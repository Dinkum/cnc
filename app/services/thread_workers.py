
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class _WorkerSlots:
    semaphore: asyncio.Semaphore
    users: int = 0


async def run_to_completion(awaitable: Awaitable[T]) -> T:
    """Finish work that must not stop halfway, deferring caller cancellation.

    Recovery that restores files or host state is useless if a second
    cancellation interrupts it. The caller's cancellation is re-raised once
    the work is done.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class BoundedThreadWorker:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._slots: dict[asyncio.AbstractEventLoop, _WorkerSlots] = {}

    async def run(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        slots = self._slots.setdefault(
            loop, _WorkerSlots(asyncio.Semaphore(self.limit))
        )
        slots.users += 1
        try:
            async with slots.semaphore:
                task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not task.cancelled():
                        task.exception()
                    raise
        finally:
            slots.users -= 1
            if slots.users == 0:
                self._slots.pop(loop, None)


_host_mutation_workers = BoundedThreadWorker(4)


class HostMutationLifecycle:
    def __init__(self, worker: BoundedThreadWorker | None = None) -> None:
        self.committed = False
        self.cancel_requested = False
        self._worker = worker or _host_mutation_workers

    async def run(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await self._worker.run(function, *args, **kwargs)

    async def run_recoverable(
        self, function: Callable[..., T], *args: Any, **kwargs: Any
    ) -> T:
        # The caller needs the rollback handle even when cancellation arrives
        # while the thread is creating it.
        result = await self._finish_despite_cancellation(
            self.run(function, *args, **kwargs)
        )
        return result

    async def finish(self, awaitable: Awaitable[T]) -> T:
        return await self._finish_despite_cancellation(awaitable)

    async def commit(self, awaitable: Awaitable[T]) -> T:
        result = await self.finish(awaitable)
        self.mark_committed()
        return result

    async def _finish_despite_cancellation(self, awaitable: Awaitable[T]) -> T:
        task = asyncio.ensure_future(awaitable)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                self.cancel_requested = True
                if task.done():
                    return task.result()

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise asyncio.CancelledError

    def mark_committed(self) -> None:
        self.committed = True

    async def recover(self, work: Callable[[], Awaitable[T]]) -> T:
        if self.committed:
            raise RuntimeError("cannot roll back a committed host mutation")
        return await run_to_completion(work())
