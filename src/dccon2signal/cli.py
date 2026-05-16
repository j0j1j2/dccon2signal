from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
import httpx

from dccon2signal import (
    auth as auth_mod,
)
from dccon2signal import (
    downloader,
    image_proc,
    pack_builder,
    persistence,
    scraper,
    uploader,
)
from dccon2signal.models import DcconPack

DEFAULT_AUTH_PATH = Path.home() / ".config" / "dccon2signal" / "auth.json"


@click.command()
@click.argument("package_idx", nargs=-1, required=True)
@click.option("--title", default=None, help="Override pack title")
@click.option("--author", default=None, help="Override author name")
@click.option(
    "--out-dir",
    default="./out",
    type=click.Path(path_type=Path),
    help="Where to save processed images + manifest",
)
@click.option("--no-upload", is_flag=True, default=False, help="Skip Signal upload")
@click.option(
    "--remove-bg",
    is_flag=True,
    default=False,
    help="Auto-remove near-white background to alpha. Off by default — many DCcons "
    "have intentional white backgrounds or character fills that would get eaten.",
)
@click.option("--static-only", is_flag=True, default=False, help="Convert GIFs to static PNG only")
@click.option(
    "--emoji-map",
    "emoji_map_path",
    type=click.Path(path_type=Path),
    default=None,
    help="JSON {sort: emoji} for per-sticker emoji",
)
@click.option(
    "--auth",
    "auth_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_AUTH_PATH,
    help="Signal credentials file",
)
@click.option("-v", "--verbose", is_flag=True, default=False)
def main(
    package_idx: tuple[str, ...],
    title: str | None,
    author: str | None,
    out_dir: Path,
    no_upload: bool,
    remove_bg: bool,
    static_only: bool,
    emoji_map_path: Path | None,
    auth_path: Path,
    verbose: bool,
) -> None:
    """Convert DCInside DCcon package(s) to Signal sticker pack(s)."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")

    emoji_map: dict[str, str] | None = None
    if emoji_map_path is not None:
        emoji_map = json.loads(emoji_map_path.read_text(encoding="utf-8"))

    asyncio.run(
        _run_all(
            package_idx,
            title,
            author,
            out_dir,
            no_upload=no_upload,
            remove_bg=remove_bg,
            static_only=static_only,
            emoji_map=emoji_map,
            auth_path=auth_path,
        )
    )


async def _run_all(
    ids: tuple[str, ...],
    title_override: str | None,
    author_override: str | None,
    out_dir: Path,
    *,
    no_upload: bool,
    remove_bg: bool,
    static_only: bool,
    emoji_map: dict[str, str] | None,
    auth_path: Path,
) -> None:
    async with httpx.AsyncClient() as client:
        for pid in ids:
            await _run_one(
                pid,
                client,
                title_override,
                author_override,
                out_dir,
                no_upload=no_upload,
                remove_bg=remove_bg,
                static_only=static_only,
                emoji_map=emoji_map,
                auth_path=auth_path,
            )


async def _run_one(
    package_idx: str,
    client: httpx.AsyncClient,
    title_override: str | None,
    author_override: str | None,
    out_dir: Path,
    *,
    no_upload: bool,
    remove_bg: bool,
    static_only: bool,
    emoji_map: dict[str, str] | None,
    auth_path: Path,
) -> None:
    click.echo(f"→ {package_idx}")
    pack: DcconPack = await scraper.fetch_pack(package_idx, client)
    if title_override:
        pack.title = title_override
    if author_override:
        pack.author = author_override
    click.echo(f"  Fetched: {pack.title!r} ({len(pack.stickers)} stickers) by {pack.author!r}")

    await downloader.download_all(pack, client)
    successful = sum(1 for s in pack.stickers if s.image_bytes is not None)
    click.echo(f"  Downloaded: {successful}/{len(pack.stickers)} images")

    image_proc.process_pack(pack, remove_bg=remove_bg, static_only=static_only)
    processed_count = sum(1 for s in pack.stickers if s.processed_bytes is not None)
    click.echo(f"  Processed: {processed_count}/{len(pack.stickers)}")

    pack_dir = out_dir / pack.package_idx
    persistence.save_pack(pack, pack_dir)
    click.echo(f"  Saved to {pack_dir}")

    if no_upload:
        return

    auth = auth_mod.load(auth_path)
    signal_pack = pack_builder.build(pack, emoji_map=emoji_map)
    pack_id, pack_key = await uploader.upload(signal_pack, auth)
    click.echo(f"  Install: {uploader.install_url(pack_id, pack_key)}")


if __name__ == "__main__":
    sys.exit(main())
