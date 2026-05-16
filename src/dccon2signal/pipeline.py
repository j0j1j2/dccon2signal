from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx

from dccon2signal import auth as auth_mod
from dccon2signal import downloader, image_proc, pack_builder, persistence, scraper, uploader
from dccon2signal.uploader import install_url as _install_url


class Stage(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    SAVING = "saving"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"


class StatusCallback(Protocol):
    async def __call__(
        self,
        stage: Stage,
        progress: tuple[int, int] | None = None,
        detail: str = "",
    ) -> None: ...


@dataclass
class ConvertResult:
    pack_id: str
    pack_key: str
    title: str
    author: str
    sticker_count: int
    install_url: str


async def _noop(
    stage: Stage,
    progress: tuple[int, int] | None = None,
    detail: str = "",
) -> None:
    return None


async def convert_pack(
    package_idx: str,
    *,
    auth_path: Path,
    out_dir: Path,
    upload: bool = True,
    remove_bg: bool = False,
    static_only: bool = False,
    title_override: str | None = None,
    author_override: str | None = None,
    emoji_map: dict[str, str] | None = None,
    on_status: StatusCallback | None = None,
) -> ConvertResult:
    cb: StatusCallback = on_status if on_status is not None else _noop

    async with httpx.AsyncClient() as client:
        await cb(Stage.FETCHING)
        pack = await scraper.fetch_pack(package_idx, client)
        if title_override:
            pack.title = title_override
        if author_override:
            pack.author = author_override

        total = len(pack.stickers)

        async def dl_progress(done: int, total_: int) -> None:
            await cb(Stage.DOWNLOADING, (done, total_))

        await cb(Stage.DOWNLOADING, (0, total))
        await downloader.download_all(pack, client, on_progress=dl_progress)

        async def proc_progress(done: int, total_: int) -> None:
            await cb(Stage.PROCESSING, (done, total_))

        await cb(Stage.PROCESSING, (0, total))
        image_proc.process_pack(
            pack,
            remove_bg=remove_bg,
            static_only=static_only,
            on_progress=proc_progress,
        )

        await cb(Stage.SAVING)
        pack_dir = out_dir / pack.package_idx
        persistence.save_pack(pack, pack_dir)

        if not upload:
            await cb(Stage.DONE)
            return ConvertResult(
                pack_id="",
                pack_key="",
                title=pack.title,
                author=pack.author,
                sticker_count=total,
                install_url="",
            )

        await cb(Stage.UPLOADING)
        auth = auth_mod.load(auth_path)
        signal_pack = pack_builder.build(pack, emoji_map=emoji_map)
        pack_id, pack_key = await uploader.upload(signal_pack, auth)

        await cb(Stage.DONE)
        return ConvertResult(
            pack_id=pack_id,
            pack_key=pack_key,
            title=pack.title,
            author=pack.author,
            sticker_count=total,
            install_url=_install_url(pack_id, pack_key),
        )
