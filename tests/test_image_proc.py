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


# After fit-to-512, the 200x200 image is centered at offset (156, 156).
# Canvas (158,158) maps to source (2,2) -> within the 20px white border.
_BORDER_PIXEL = (158, 158)
# Canvas (256,256) is center -> within the red interior.
_INTERIOR_PIXEL = (256, 256)


def test_process_static_png_returns_512_rgba(sample_static_png):
    out, ext = process_sticker_bytes(sample_static_png, source_ext="png", remove_bg=True)
    assert ext == "png"
    img = _decode(out)
    assert img.size == (SIGNAL_SIZE, SIGNAL_SIZE)
    assert img.mode == "RGBA"
    assert len(out) <= SIGNAL_MAX_BYTES


def test_process_static_removes_white_background():
    out, _ = process_sticker_bytes(
        _synth_white_border_png(), source_ext="png", remove_bg=True
    )
    img = _decode(out)
    border = img.getpixel(_BORDER_PIXEL)
    interior = img.getpixel(_INTERIOR_PIXEL)
    assert isinstance(border, tuple)
    assert isinstance(interior, tuple)
    assert border[3] == 0, f"white border should be transparent, got {border}"
    assert interior[3] == 255, f"red interior should be opaque, got {interior}"


def test_process_static_keeps_background_when_disabled():
    out, _ = process_sticker_bytes(
        _synth_white_border_png(), source_ext="png", remove_bg=False
    )
    img = _decode(out)
    border = img.getpixel(_BORDER_PIXEL)
    assert isinstance(border, tuple)
    assert border[3] == 255, f"with remove_bg=False, white border stays opaque, got {border}"
