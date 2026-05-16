import json
from pathlib import Path

from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal.persistence import save_pack


def test_save_pack_writes_manifest_and_images(tmp_path: Path):
    pack = DcconPack(
        package_idx="170660",
        title="구구가가",
        author="도끼도끼",
        description="설명",
        cover_url="u",
        cover_processed=b"\x89PNG\r\n\x1a\nFAKE_COVER",
    )
    pack.stickers.append(
        DcconSticker(
            idx="1",
            sort=1,
            title="1",
            ext="png",
            image_url="u",
            processed_bytes=b"\x89PNG\r\n\x1a\nFAKE_STICKER",
            processed_ext="png",
            emoji="😀",
        )
    )
    pack.stickers.append(
        DcconSticker(
            idx="2",
            sort=2,
            title="2",
            ext="gif",
            image_url="u",
            processed_bytes=b"\x89PNG\r\n\x1a\nFAKE_APNG",
            processed_ext="apng",
            emoji="😂",
        )
    )

    out_dir = tmp_path / "170660"
    save_pack(pack, out_dir)

    assert (out_dir / "cover.png").read_bytes() == b"\x89PNG\r\n\x1a\nFAKE_COVER"
    assert (out_dir / "stickers" / "1.png").exists()
    assert (out_dir / "stickers" / "2.apng").exists()

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_idx"] == "170660"
    assert manifest["title"] == "구구가가"
    assert manifest["author"] == "도끼도끼"
    assert manifest["stickers"][1]["emoji"] == "😂"
    assert manifest["stickers"][1]["file"] == "stickers/2.apng"


def test_save_pack_skips_unprocessed(tmp_path: Path):
    pack = DcconPack(package_idx="1", title="t", author="a", description="d", cover_url="u")
    pack.stickers.append(
        DcconSticker(
            idx="1",
            sort=1,
            title="t",
            ext="png",
            image_url="u",
            processed_bytes=None,
        )
    )
    out_dir = tmp_path / "1"
    save_pack(pack, out_dir)
    assert not (out_dir / "stickers" / "1.png").exists()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stickers"] == []
