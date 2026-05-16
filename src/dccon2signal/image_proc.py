from io import BytesIO

from PIL import Image, ImageSequence

from dccon2signal.models import DcconPack, ImageExt, ProcessedExt

SIGNAL_SIZE = 512
SIGNAL_MAX_BYTES = 300 * 1024
WHITE_THRESHOLD = 240


def _remove_white_bg(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    pixels = rgba.load()
    assert pixels is not None
    width, height = rgba.size
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
                pixels[x, y] = (r, g, b, 0)
    return rgba


def _fit_512(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    img.thumbnail((SIGNAL_SIZE, SIGNAL_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (SIGNAL_SIZE, SIGNAL_SIZE), (0, 0, 0, 0))
    ox = (SIGNAL_SIZE - img.width) // 2
    oy = (SIGNAL_SIZE - img.height) // 2
    canvas.paste(img, (ox, oy), img)
    return canvas


def _encode_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _shrink_png_under_limit(img: Image.Image) -> bytes:
    out = _encode_png(img)
    if len(out) <= SIGNAL_MAX_BYTES:
        return out
    for colors in (256, 128, 64, 32):
        quant = img.quantize(colors=colors).convert("RGBA")
        buf = BytesIO()
        quant.save(buf, format="PNG", optimize=True)
        candidate = buf.getvalue()
        if len(candidate) <= SIGNAL_MAX_BYTES:
            return candidate
    return out


def process_sticker_bytes(
    data: bytes,
    *,
    source_ext: ImageExt,
    remove_bg: bool,
    static_only: bool = False,
) -> tuple[bytes, ProcessedExt]:
    img = Image.open(BytesIO(data))
    is_animated = bool(getattr(img, "is_animated", False))

    if source_ext == "gif" and is_animated and not static_only:
        return _process_animated(img, remove_bg=remove_bg)

    if source_ext == "gif" and is_animated and static_only:
        img.seek(0)
        img = img.copy()

    processed = _remove_white_bg(img) if remove_bg else img.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def _process_animated(img: Image.Image, *, remove_bg: bool) -> tuple[bytes, ProcessedExt]:
    img.seek(0)
    first = img.copy()
    processed = _remove_white_bg(first) if remove_bg else first.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def process_pack(pack: DcconPack, *, remove_bg: bool = True, static_only: bool = False) -> None:
    if pack.cover_bytes is not None:
        pack.cover_processed, _ = process_sticker_bytes(
            pack.cover_bytes, source_ext="png", remove_bg=remove_bg, static_only=True
        )
    for s in pack.stickers:
        if s.image_bytes is None:
            continue
        s.processed_bytes, s.processed_ext = process_sticker_bytes(
            s.image_bytes,
            source_ext=s.ext,
            remove_bg=remove_bg,
            static_only=static_only,
        )
