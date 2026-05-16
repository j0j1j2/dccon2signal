import json
from pathlib import Path

from dccon2signal.models import DcconPack


def save_pack(pack: DcconPack, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stickers_dir = out_dir / "stickers"
    stickers_dir.mkdir(exist_ok=True)

    if pack.cover_processed is not None:
        (out_dir / "cover.png").write_bytes(pack.cover_processed)

    manifest_stickers = []
    for s in pack.stickers:
        if s.processed_bytes is None or s.processed_ext is None:
            continue
        filename = f"{s.sort}.{s.processed_ext}"
        (stickers_dir / filename).write_bytes(s.processed_bytes)
        manifest_stickers.append(
            {
                "sort": s.sort,
                "idx": s.idx,
                "title": s.title,
                "emoji": s.emoji,
                "file": f"stickers/{filename}",
            }
        )

    manifest = {
        "package_idx": pack.package_idx,
        "title": pack.title,
        "author": pack.author,
        "description": pack.description,
        "tags": pack.tags,
        "stickers": manifest_stickers,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
