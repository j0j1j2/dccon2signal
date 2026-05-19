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


# WebP encoder method: 0 = fastest, 6 = best compression. method=2 keeps
# per-encode time under a second for typical DCcon GIFs; method=4 would
# trim ~5-10% off file sizes but multiplies pack-conversion time by ~2x
# given we encode multiple times per sticker for quality/stride search.
WEBP_METHOD = 2

# Minimum per-frame display duration. 33ms ≈ 30fps cap. The fps perceived
# in playback is `1000 / max_kept_frame_duration` — so to feel like 30fps
# we need both this minimum AND enough kept frames that no stride forces
# us to consolidate multiple source frames into one held output frame.
# See _encode_webp_under_limit for the stride-1-first strategy that makes
# 30fps actually achievable on the source frame counts we can fit.
MIN_FRAME_DURATION_MS = 33


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
    # Strategy: keep every source frame when feasible (= max perceived fps,
    # ~30fps if MIN_FRAME_DURATION_MS=33). Long source GIFs can't fit ~50+
    # frames in 300KB even at q=40, so skip directly to a stride that yields
    # a frame count we have any hope of encoding. Per-frame duration is
    # consolidated across stride so total duration matches source.
    _MAX_FRAMES_TARGET = 40
    initial_stride = max(1, len(frames) // _MAX_FRAMES_TARGET)

    candidate_strides = sorted(
        {
            initial_stride,
            initial_stride * 2,
            initial_stride * 3,
            initial_stride * 4,
            initial_stride * 6,
            initial_stride * 9,
        }
    )
    for stride in candidate_strides:
        sub_frames = frames[::stride]
        if len(sub_frames) < 2:
            # Either we ran out of frames, or input was a single frame.
            break
        # Per kept frame: sum the source-frame durations it replaces, with
        # each source frame clamped to at least MIN_FRAME_DURATION_MS. This
        # both enforces the fps ceiling and preserves total duration across
        # stride frame-dropping.
        sub_durations = [
            sum(max(d, MIN_FRAME_DURATION_MS) for d in durations[i : i + stride])
            for i in range(0, len(durations), stride)
        ]

        # Single optimistic attempt at a balanced quality. Tight pass-or-skip
        # logic — we don't try to find the BEST fitting quality because
        # encoding a high-frame WebP costs ~1s and accumulates fast across
        # the pack. q=70 is the sweet spot: visually clean, usually fits
        # once the stride heuristic has tightened frame count.
        out = _encode_webp(sub_frames, sub_durations, 70)
        if len(out) <= SIGNAL_MAX_BYTES:
            return out, "webp"

        # Fast fallback at lower quality.
        out = _encode_webp(sub_frames, sub_durations, 50)
        if len(out) <= SIGNAL_MAX_BYTES:
            return out, "webp"

        # Still over budget — skip to the next stride. Last-resort q=40
        # is tried only when stride escalation has reached its end.
        if stride < candidate_strides[-1]:
            continue
        out = _encode_webp(sub_frames, sub_durations, 40)
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
