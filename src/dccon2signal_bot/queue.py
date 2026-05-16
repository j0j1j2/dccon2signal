import asyncio
from dataclasses import dataclass


@dataclass
class Job:
    package_idx: str
    chat_id: int
    message_id: int
    submitted_at: float


class JobQueue:
    """asyncio.Queue wrapper exposing current size and 1-based submit position."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[Job] = asyncio.Queue()

    async def submit(self, job: Job) -> int:
        await self._q.put(job)
        return self._q.qsize()

    async def get(self) -> Job:
        return await self._q.get()

    def task_done(self) -> None:
        self._q.task_done()

    @property
    def size(self) -> int:
        return self._q.qsize()
