import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from dccon2signal.models import DcconPack, DcconSticker

logger = logging.getLogger(__name__)

HEADERS = {
    "Referer": "https://dccon.dcinside.com/",
    "User-Agent": "Mozilla/5.0 (dccon2signal)",
}


async def _fetch_one(client: httpx.AsyncClient, url: str, *, retries: int) -> bytes | None:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, headers=HEADERS, timeout=15.0)
            resp.raise_for_status()
            return resp.content
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_error = e
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    logger.warning("Failed to fetch %s after %d attempts: %s", url, retries + 1, last_error)
    return None


async def download_all(
    pack: DcconPack,
    client: httpx.AsyncClient,
    *,
    concurrency: int = 8,
    retries: int = 3,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    total = len(pack.stickers) + 1  # +1 for cover
    done = 0
    lock = asyncio.Lock()

    async def _bumped_fetch(url: str) -> bytes | None:
        nonlocal done
        async with sem:
            data = await _fetch_one(client, url, retries=retries)
        async with lock:
            done += 1
            current = done
        if on_progress is not None:
            await on_progress(current, total)
        return data

    async def _fetch_cover() -> None:
        pack.cover_bytes = await _bumped_fetch(pack.cover_url)

    async def _fetch_sticker(s: DcconSticker) -> None:
        s.image_bytes = await _bumped_fetch(s.image_url)

    await asyncio.gather(
        _fetch_cover(),
        *(_fetch_sticker(s) for s in pack.stickers),
    )
