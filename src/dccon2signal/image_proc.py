import logging
from collections.abc import Awaitable, Callable
from io import BytesIO

from PIL import Image, ImageSequence

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
        return _process_animated(img, remove_bg=remove_bg)

    if source_ext == "gif" and is_animated and static_only:
        img.seek(0)
        img = img.copy()  # type: ignore[assignment]

    processed = _remove_white_bg(img) if remove_bg else img.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def _process_animated(img: Image.Image, *, remove_bg: bool) -> tuple[bytes, ProcessedExt]:
    # Animated stickers go out as animated WebP, which compresses ~5× better
    # than APNG for cartoon-like content. This lets us keep the full 512×512
    # canvas (matching static stickers) AND all frames within Signal's 300KB
    # per-sticker budget.
    raw_frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        f = frame.convert("RGBA")
        if remove_bg:
            f = _remove_white_bg(f)
        raw_frames.append(_fit_to_canvas(f, SIGNAL_SIZE))
        durations.append(int(frame.info.get("duration", 100)))

    return _encode_webp_under_limit(raw_frames, durations)


# WebP encoder method: 0 = fastest, 6 = best compression. method=2 cuts
# encoding time ~50% vs method=4 while keeping files comfortably under 300KB
# for typical DCcon animated frames.
WEBP_METHOD = 2

# Per-frame display duration in the output animation. ~60fps target makes
# stickers look smooth on Signal even after stride-based frame dropping —
# accepting that the animation plays faster than the original GIF timing.
WEBP_FRAME_DURATION_MS = 17


def _encode_webp(frames: list[Image.Image], durations: list[int], quality: int) -> bytes:
    buf = BytesIO()
    frames[0].save(
        buf,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        quality=quality,
        method=WEBP_METHOD,
        # Force every frame to be a key frame (kmin=kmax=1). Without this
        # libwebp emits sub-frame diffs (variable-sized rectangles with
        # mixed alpha/blend modes), which Glide on Android sometimes
        # refuses to animate, rendering as static instead. With key-frames
        # only, each frame is a full 512×512 RGBA image — bigger files
        # but rock-solid compatibility across Signal clients.
        kmin=1,
        kmax=1,
        minimize_size=False,
        allow_mixed=False,
        lossless=False,
        background=(0, 0, 0, 0),
    )
    return buf.getvalue()


def _encode_webp_under_limit(
    frames: list[Image.Image], durations: list[int]
) -> tuple[bytes, ProcessedExt]:
    # With kmin=1 each frame is a full key frame, so file size scales linearly
    # with frame count. Empirically ~15 frames @ 512×512 fits 300KB at q=80.
    # Pre-compute an initial stride so we don't waste 3-4 encode passes that
    # are mathematically doomed.
    initial_stride = max(1, (len(frames) + 17) // 18)

    for stride in (initial_stride, initial_stride * 2, initial_stride * 3, initial_stride * 4):
        sub_frames = frames[::stride]
        if not sub_frames:
            continue
        # Fixed 60fps-target duration per output frame (the `durations`
        # passed in from the source GIF are discarded — see
        # WEBP_FRAME_DURATION_MS comment for rationale).
        sub_durations = [WEBP_FRAME_DURATION_MS] * len(sub_frames)
        for quality in (80, 70, 60, 50):
            if len(sub_frames) == 1:
                buf = BytesIO()
                sub_frames[0].save(buf, format="WEBP", quality=quality, method=WEBP_METHOD)
                out = buf.getvalue()
            else:
                out = _encode_webp(sub_frames, sub_durations, quality)
            if len(out) <= SIGNAL_MAX_BYTES:
                return out, "webp"

    # Last resort: static PNG of frame 0 (uses PNG quantize fallback for size).
    return _shrink_png_under_limit(frames[0]), "png"


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
            pack.cover_bytes, source_ext="png", remove_bg=remove_bg, static_only=True
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
            # A single broken sticker (e.g. malformed GIF, decompression-bomb
            # tripped) should NOT take down the whole pack — leave it
            # unprocessed and persistence/pack_builder will skip it.
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
