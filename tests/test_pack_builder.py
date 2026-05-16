import pytest

from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal.pack_builder import PackBuilderError, build


def _pack_with_two_stickers() -> DcconPack:
    pack = DcconPack(
        package_idx="1",
        title="t",
        author="a",
        description="d",
        cover_url="u",
        cover_processed=b"COVER",
    )
    pack.stickers.extend(
        [
            DcconSticker(
                idx="1",
                sort=1,
                title="x",
                ext="png",
                image_url="u",
                processed_bytes=b"S1",
                processed_ext="png",
                emoji="😀",
            ),
            DcconSticker(
                idx="2",
                sort=2,
                title="y",
                ext="gif",
                image_url="u",
                processed_bytes=b"S2",
                processed_ext="apng",
                emoji="😀",
            ),
        ]
    )
    return pack


def test_build_sets_metadata():
    pack = _pack_with_two_stickers()
    signal_pack = build(pack)
    assert signal_pack.title == "t"
    assert signal_pack.author == "a"


def test_build_applies_emoji_map():
    pack = _pack_with_two_stickers()
    signal_pack = build(pack, emoji_map={"1": "🐱", "2": "🐶"})
    emojis = sorted(s.emoji for s in signal_pack.stickers)
    assert emojis == ["🐱", "🐶"]


def test_build_rejects_missing_cover():
    pack = _pack_with_two_stickers()
    pack.cover_processed = None
    with pytest.raises(PackBuilderError, match="cover"):
        build(pack)


def test_build_rejects_no_stickers():
    pack = DcconPack(
        package_idx="1",
        title="t",
        author="a",
        description="d",
        cover_url="u",
        cover_processed=b"COVER",
    )
    with pytest.raises(PackBuilderError, match="empty"):
        build(pack)


def test_build_cover_uses_dedicated_slot():
    """Regression: cover must NOT share id=0 with stickers[0], or the cover
    upload overwrites the first sticker on Signal's CDN."""
    pack = _pack_with_two_stickers()
    signal_pack = build(pack)
    sticker_ids = [s.id for s in signal_pack.stickers]
    assert sticker_ids == [0, 1]
    assert signal_pack.cover.id == 2, (
        f"cover.id must equal len(stickers) (=2), got {signal_pack.cover.id}"
    )


def test_build_rejects_over_200_stickers():
    pack = _pack_with_two_stickers()
    pack.cover_processed = b"COVER"
    for i in range(3, 250):
        pack.stickers.append(
            DcconSticker(
                idx=str(i),
                sort=i,
                title=str(i),
                ext="png",
                image_url="u",
                processed_bytes=b"X",
                processed_ext="png",
            )
        )
    with pytest.raises(PackBuilderError, match="200"):
        build(pack)
