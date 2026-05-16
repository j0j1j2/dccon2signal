import asyncio
import logging

import httpx

from dccon2signal.models import DcconPack, DcconSticker

logger = logging.getLogger(__name__)

HEADERS = {
    "Referer": "https://dccon.dcinside.com/",
    "User-Agent": "Mozilla/5.0 (dccon2signal)",
}


async def _fetch_one(
    client: httpx.AsyncClient, url: str, *, retries: int
) -> bytes | None:
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
) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def _bounded_fetch(url: str) -> bytes | None:
        async with sem:
            return await _fetch_one(client, url, retries=retries)

    async def _fetch_cover() -> None:
        pack.cover_bytes = await _bounded_fetch(pack.cover_url)

    async def _fetch_sticker(s: DcconSticker) -> None:
        s.image_bytes = await _bounded_fetch(s.image_url)

    await asyncio.gather(
        _fetch_cover(),
        *(_fetch_sticker(s) for s in pack.stickers),
    )
