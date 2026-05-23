from io import BytesIO

from PIL import Image

from dccon2signal.image_proc import (
    SIGNAL_MAX_BYTES,
    SIGNAL_SIZE,
    process_pack,
    process_sticker_bytes,
)
from dccon2signal.models import DcconPack, DcconSticker


def _decode(b: bytes) -> Image.Image:
    return Image.open(BytesIO(b))


def _synth_white_border_png() -> bytes:
    """200x200 PNG: 20px white border around a solid red center."""
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    for y in range(20, 180):
        for x in range(20, 180):
            img.putpixel((x, y), (200, 50, 50))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# 200x200 source upscaled to 512x512: scale ≈ 2.56.
# Source (2,2) (white border, src has 20px border) maps to canvas (~5, ~5).
# Source (100,100) (red interior) maps to canvas (~256, ~256).
_BORDER_PIXEL = (5, 5)
_INTERIOR_PIXEL = (256, 256)


def test_process_static_png_returns_512_rgba(sample_static_png):
    out, ext = process_sticker_bytes(sample_static_png, source_ext="png", remove_bg=True)
    assert ext == "png"
    img = _decode(out)
    assert img.size == (SIGNAL_SIZE, SIGNAL_SIZE)
    assert img.mode == "RGBA"
    assert len(out) <= SIGNAL_MAX_BYTES


def test_process_static_removes_white_background():
    out, _ = process_sticker_bytes(_synth_white_border_png(), source_ext="png", remove_bg=True)
    img = _decode(out)
    border = img.getpixel(_BORDER_PIXEL)
    interior = img.getpixel(_INTERIOR_PIXEL)
    assert isinstance(border, tuple)
    assert isinstance(interior, tuple)
    assert border[3] == 0, f"white border should be transparent, got {border}"
    assert interior[3] == 255, f"red interior should be opaque, got {interior}"


def test_process_static_keeps_background_when_disabled():
    out, _ = process_sticker_bytes(_synth_white_border_png(), source_ext="png", remove_bg=False)
    img = _decode(out)
    border = img.getpixel(_BORDER_PIXEL)
    assert isinstance(border, tuple)
    assert border[3] == 255, f"with remove_bg=False, white border stays opaque, got {border}"


def test_animated_gif_becomes_apng_under_limit(sample_animated_gif):
    out, ext = process_sticker_bytes(sample_animated_gif, source_ext="gif", remove_bg=True)
    assert ext == "apng"
    img = _decode(out)
    assert img.format == "PNG"
    assert getattr(img, "is_animated", False) is True
    # Encoder keeps source size when it fits, so the fixture (200×200) stays 200×200.
    src = Image.open(BytesIO(sample_animated_gif))
    assert img.size == src.size
    assert len(out) <= SIGNAL_MAX_BYTES


def test_animated_gif_preserves_all_frames(sample_animated_gif):
    """Visual regression: with the apngasm encoder, short animations should
    not lose frames to stride. The fixture is short enough that stride=1
    must fit on the colour ladder."""
    out, ext = process_sticker_bytes(sample_animated_gif, source_ext="gif", remove_bg=False)
    assert ext == "apng"
    out_img = _decode(out)
    src = Image.open(BytesIO(sample_animated_gif))
    assert getattr(out_img, "n_frames", 1) == getattr(src, "n_frames", 1)


def test_animated_gif_static_only_returns_png(sample_animated_gif):
    out, ext = process_sticker_bytes(
        sample_animated_gif, source_ext="gif", remove_bg=True, static_only=True
    )
    assert ext == "png"
    img = _decode(out)
    assert img.size == (SIGNAL_SIZE, SIGNAL_SIZE)
    assert not getattr(img, "is_animated", False)


async def test_process_pack_populates_processed_fields(sample_static_png, sample_animated_gif):
    pack = DcconPack(
        package_idx="1",
        title="t",
        author="a",
        description="d",
        cover_url="u",
        cover_bytes=sample_static_png,
    )
    pack.stickers.append(
        DcconSticker(
            idx="1",
            sort=1,
            title="s",
            ext="gif",
            image_url="u",
            image_bytes=sample_animated_gif,
        )
    )
    await process_pack(pack)
    assert pack.cover_processed is not None
    assert pack.stickers[0].processed_bytes is not None
    assert pack.stickers[0].processed_ext == "apng"
