from io import BytesIO

from PIL import Image, ImageSequence

from dccon2signal.models import DcconPack, ImageExt, ProcessedExt

SIGNAL_SIZE = 512
# Signal's per-sticker limit is "300 KB" — using the SI definition (300,000 bytes)
# to stay safely under the server-side check.
SIGNAL_MAX_BYTES = 300_000
WHITE_THRESHOLD = 240


def _remove_white_bg(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    pixels = rgba.load()
    assert pixels is not None
    width, height = rgba.size
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]  # type: ignore[misc]
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
        img = img.copy()  # type: ignore[assignment]

    processed = _remove_white_bg(img) if remove_bg else img.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def _process_animated(img: Image.Image, *, remove_bg: bool) -> tuple[bytes, ProcessedExt]:
    raw_frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        f = frame.convert("RGBA")
        if remove_bg:
            f = _remove_white_bg(f)
        raw_frames.append(_fit_512(f))
        durations.append(int(frame.info.get("duration", 100)))

    return _encode_apng_under_limit(raw_frames, durations)


def _encode_apng(frames: list[Image.Image], durations: list[int]) -> bytes:
    buf = BytesIO()
    frames[0].save(
        buf,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


def _encode_apng_under_limit(
    frames: list[Image.Image], durations: list[int]
) -> tuple[bytes, ProcessedExt]:
    for stride in (1, 2, 3, 4, 6, 8, 12):
        sub_frames = frames[::stride]
        if len(sub_frames) < 2:
            break
        sub_durations = [sum(durations[i : i + stride]) for i in range(0, len(durations), stride)]
        out = _encode_apng(sub_frames, sub_durations)
        if len(out) <= SIGNAL_MAX_BYTES:
            return out, "apng"

    static_out = _shrink_png_under_limit(frames[0])
    return static_out, "png"


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
