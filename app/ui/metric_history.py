
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field

from fastapi.responses import JSONResponse

from app.services.error_reporting import ErrorCode
from app.ui.errors import operator_error_json

HistoryResponse = dict[str, object] | JSONResponse


@dataclass
class _HistoryRequests:
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    tasks: dict[Hashable, asyncio.Task[HistoryResponse]] = field(default_factory=dict)


_MAX_PENDING = 8
_REQUESTS: dict[asyncio.AbstractEventLoop, _HistoryRequests] = {}


async def run_metric_history_request(
    key: Hashable, build: Callable[[], Awaitable[HistoryResponse]]
) -> HistoryResponse:
    loop = asyncio.get_running_loop()
    state = _REQUESTS.setdefault(loop, _HistoryRequests())
    task = state.tasks.get(key)
    if task is None:
        if len(state.tasks) >= _MAX_PENDING:
            response = operator_error_json(
                "Chart history is busy. Please retry shortly.",
                ErrorCode.UI_ACTION_UNAVAILABLE,
                status_code=503,
            )
            response.headers["Retry-After"] = "1"
            return response

        async def run() -> HistoryResponse:
            async with state.slots:
                return await build()

        task = asyncio.create_task(run())
        state.tasks[key] = task

        def finished(done: asyncio.Task[HistoryResponse]) -> None:
            state.tasks.pop(key, None)
            if not state.tasks and _REQUESTS.get(loop) is state:
                _REQUESTS.pop(loop, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
    return await asyncio.shield(task)


async def cancel_metric_history_requests() -> None:
    state = _REQUESTS.get(asyncio.get_running_loop())
    if state is None:
        return
    tasks = list(state.tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
