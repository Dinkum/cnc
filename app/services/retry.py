from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar
from urllib import error as urllib_error


T = TypeVar("T")

RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def is_retryable_http_exception(exc: Exception) -> bool:
    if isinstance(exc, urllib_error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS_CODES
    if isinstance(exc, urllib_error.URLError):
        return True
    return isinstance(exc, (TimeoutError, OSError))


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int,
    backoff_sec: float,
    should_retry: Callable[[Exception], bool],
    sleep_func: Callable[[float], None] = time.sleep,
    on_retry: Callable[[Exception, int, int, float], None] | None = None,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts or not should_retry(exc):
                raise
            delay_sec = max(0.0, backoff_sec) * attempt
            if on_retry is not None:
                on_retry(exc, attempt, attempts, delay_sec)
            if delay_sec > 0:
                sleep_func(delay_sec)
    raise AssertionError("retry_call exhausted without returning or raising")


async def retry_async_call(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff_sec: float,
    should_retry: Callable[[Exception], bool],
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[Exception, int, int, float], None] | None = None,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= attempts or not should_retry(exc):
                raise
            delay_sec = max(0.0, backoff_sec) * attempt
            if on_retry is not None:
                on_retry(exc, attempt, attempts, delay_sec)
            if delay_sec > 0:
                await sleep_func(delay_sec)
    raise AssertionError("retry_async_call exhausted without returning or raising")
