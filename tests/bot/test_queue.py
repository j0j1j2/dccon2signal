import time

import pytest

from dccon2signal_bot.queue import Job, JobQueue


def _job(pkg: str) -> Job:
    return Job(package_idx=pkg, chat_id=1, message_id=2, submitted_at=time.time())


@pytest.mark.asyncio
async def test_submit_returns_1_based_position():
    q = JobQueue()
    pos1 = await q.submit(_job("1"))
    pos2 = await q.submit(_job("2"))
    pos3 = await q.submit(_job("3"))
    assert (pos1, pos2, pos3) == (1, 2, 3)
    assert q.size == 3


@pytest.mark.asyncio
async def test_fifo_order():
    q = JobQueue()
    await q.submit(_job("a"))
    await q.submit(_job("b"))
    first = await q.get()
    second = await q.get()
    assert first.package_idx == "a"
    assert second.package_idx == "b"


@pytest.mark.asyncio
async def test_task_done_after_get():
    q = JobQueue()
    await q.submit(_job("a"))
    await q.get()
    q.task_done()
    assert q.size == 0
