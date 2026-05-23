import logging
from collections.abc import Awaitable, Callable
from io import BytesIO

from PIL import Image

from dccon2signal import apng_encoder
from dccon2signal.models import DcconPack, ImageExt, ProcessedExt

logger = logging.getLogger(__name__)

# Some DCcon GIFs carry garbage dimension values in a stray frame header and
# trigger Pillow's decompression-bomb safety check (verified: pixel data is
# actually 200x200 in those frames, only the metadata is bogus). Disable
# the Pillow check so we can decode them. Real-bomb defense is handled at
# the bot entry point via RLIMIT_AS — a genuine bomb hits the OS memory
# cap and raises MemoryError, which the per-sticker try/except below skips.
Image.MAX_IMAGE_PIXELS = None

SIGNAL_SIZE = 512
# Signal's per-sticker limit is documented as "300 KB" but server-side it
# means 300 KiB. Confirmed by surveying the community Gojill-Animated pack:
# production stickers up to 304,561 bytes (297.4 KiB) are accepted and
# rendered. Use 307,200 as the ceiling so we don't burn ~7KB of headroom.
SIGNAL_MAX_BYTES = 307_200
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


def _fit_to_canvas(img: Image.Image, target_size: int) -> Image.Image:
    """Resize so longest side = target_size, then center on target_size square."""
    img = img.convert("RGBA")
    scale = target_size / max(img.width, img.height)
    new_w = round(img.width * scale)
    new_h = round(img.height * scale)
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if new_w == target_size and new_h == target_size:
        return resized
    canvas = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    ox = (target_size - new_w) // 2
    oy = (target_size - new_h) // 2
    canvas.paste(resized, (ox, oy), resized)
    return canvas


def _fit_512(img: Image.Image) -> Image.Image:
    return _fit_to_canvas(img, SIGNAL_SIZE)


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
    remove_bg: bool = False,
    static_only: bool = False,
) -> tuple[bytes, ProcessedExt]:
    img = Image.open(BytesIO(data))
    is_animated = bool(getattr(img, "is_animated", False))

    if source_ext == "gif" and is_animated and not static_only:
        return _process_animated(data)

    if source_ext == "gif" and is_animated and static_only:
        img.seek(0)
        img = img.copy()  # type: ignore[assignment]

    processed = _remove_white_bg(img) if remove_bg else img.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def _process_animated(gif_bytes: bytes) -> tuple[bytes, ProcessedExt]:
    out = apng_encoder.encode_animated_apng(gif_bytes, SIGNAL_MAX_BYTES)
    if out is not None:
        return out, "apng"

    # Encoder couldn't fit any (stride, colour) combo. Fall back to a
    # static PNG of the first frame so the upload still succeeds.
    logger.warning("APNG could not fit %d bytes; falling back to static first frame", SIGNAL_MAX_BYTES)
    img = Image.open(BytesIO(gif_bytes))
    img.seek(0)
    first = img.convert("RGBA")
    return _shrink_png_under_limit(_fit_512(first)), "png"


async def process_pack(
    pack: DcconPack,
    *,
    remove_bg: bool = False,
    static_only: bool = False,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> None:
    """Process every sticker (and the cover) in place.

    Coroutine even though Pillow is sync: between stickers we directly await
    on_progress, which both fires the callback in real time AND yields control
    to the event loop so other tasks (e.g. the Telegram reporter's outgoing
    edit_message_text) can run.
    """
    total = len(pack.stickers) + (1 if pack.cover_bytes is not None else 0)
    done = 0

    if pack.cover_bytes is not None:
        pack.cover_processed, _ = process_sticker_bytes(
            pack.cover_bytes,
            source_ext="png",
            remove_bg=remove_bg,
            static_only=True,
        )
        done += 1
        if on_progress is not None:
            await on_progress(done, total)

    for s in pack.stickers:
        if s.image_bytes is None:
            done += 1
            if on_progress is not None:
                await on_progress(done, total)
            continue
        try:
            s.processed_bytes, s.processed_ext = process_sticker_bytes(
                s.image_bytes,
                source_ext=s.ext,
                remove_bg=remove_bg,
                static_only=static_only,
            )
        except Exception as e:
            # A single broken sticker (e.g. malformed GIF, encoder OOM) should
            # NOT take down the whole pack — leave it unprocessed and
            # persistence/pack_builder will skip it.
            logger.warning(
                "Skipping sticker sort=%s idx=%s: %s: %s",
                s.sort,
                s.idx,
                type(e).__name__,
                e,
            )
        done += 1
        if on_progress is not None:
            await on_progress(done, total)
