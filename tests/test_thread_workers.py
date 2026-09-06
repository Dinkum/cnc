import asyncio
import threading

import pytest

from app.services.thread_workers import BoundedThreadWorker


@pytest.mark.asyncio
async def test_cancelled_worker_holds_slot_until_thread_finishes():
    worker = BoundedThreadWorker(1)
    started, release, second_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def first():
        started.set()
        assert release.wait(3)

    first_task = asyncio.create_task(worker.run(first))
    second_task = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first_task.cancel()
        await asyncio.sleep(0)
        first_task.cancel()
        second_task = asyncio.create_task(worker.run(second_started.set))
        await asyncio.sleep(0.02)
        assert not first_task.done()
        assert not second_started.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await second_task
    assert second_started.is_set()
    assert not worker._slots
