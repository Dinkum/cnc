"""Bound blocking work without abandoning an active thread on cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class _WorkerSlots:
    semaphore: asyncio.Semaphore
    users: int = 0


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
                    # A thread cannot be cancelled. Keep its slot and owner until
                    # its file locks are released, even after repeated cancellation.
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
