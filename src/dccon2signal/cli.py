from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click

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
    for pid in ids:
        await _run_one(
            pid,
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
    from dccon2signal.pipeline import Stage, convert_pack

    click.echo(f"→ {package_idx}")

    async def echo(stage: Stage, progress=None, detail=""):
        msg = stage.value
        if progress is not None:
            msg = f"{msg} {progress[0]}/{progress[1]}"
        if detail:
            msg = f"{msg} — {detail}"
        click.echo(f"  {msg}")

    result = await convert_pack(
        package_idx,
        auth_path=auth_path,
        out_dir=out_dir,
        upload=not no_upload,
        remove_bg=remove_bg,
        static_only=static_only,
        title_override=title_override,
        author_override=author_override,
        emoji_map=emoji_map,
        on_status=echo,
    )

    click.echo(
        f"  Fetched: {result.title!r} ({result.sticker_count} stickers) by {result.author!r}"
    )
    if result.install_url:
        click.echo(f"  Install: {result.install_url}")


if __name__ == "__main__":
    sys.exit(main())
