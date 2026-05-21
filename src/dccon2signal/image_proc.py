import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from io import BytesIO

from PIL import Image, ImageSequence

from dccon2signal.models import DcconPack, ImageExt, ProcessedExt

logger = logging.getLogger(__name__)

# Google's reference GIF→animated-WebP encoder. Its output sets the VP8X
# alpha flag and uses canonical sub-frame structure, which Signal Android's
# decoder animates correctly — Pillow's encoder drops the alpha flag for
# opaque content and Android then renders the sticker as a static first
# frame. Prefer gif2webp when present; fall back to Pillow otherwise.
GIF2WEBP_BIN = shutil.which("gif2webp")

# Some DCcon GIFs carry garbage dimension values in a stray frame header and
# trigger Pillow's decompression-bomb safety check (verified: pixel data is
# actually 200x200 in those frames, only the metadata is bogus). Disable
# the Pillow check so we can decode them. Real-bomb defense is handled at
# the bot entry point via RLIMIT_AS — a genuine bomb hits the OS memory
# cap and raises MemoryError, which the per-sticker try/except below skips.
Image.MAX_IMAGE_PIXELS = None

SIGNAL_SIZE = 512
# Animated stickers render at full 512×512 — gif2webp's sub-frame
# compression keeps even high frame counts well under the 300KB budget,
# so the smaller canvas the Pillow path needed is no longer required.
ANIM_SIZE = 512
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
    # Decode every source frame to a fitted RGBA image once.
    raw_frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        f = frame.convert("RGBA")
        if remove_bg:
            f = _remove_white_bg(f)
        raw_frames.append(_fit_to_canvas(f, ANIM_SIZE))
        durations.append(int(frame.info.get("duration", 100)))

    if GIF2WEBP_BIN is not None:
        out = _encode_animated_gif2webp(raw_frames, durations)
        if out is not None:
            return out, "webp"
        logger.warning("gif2webp encoding failed; falling back to Pillow WebP")

    return _encode_webp_under_limit(raw_frames, durations)


def _strided_durations(durations: list[int], stride: int) -> list[int]:
    # Each kept frame's duration = sum of source-frame durations it replaces,
    # with each source frame clamped to MIN_FRAME_DURATION_MS. Preserves total
    # playback duration under stride and caps fps at ~30.
    return [
        sum(max(d, MIN_FRAME_DURATION_MS) for d in durations[i : i + stride])
        for i in range(0, len(durations), stride)
    ]


def _frames_to_gif_bytes(frames: list[Image.Image], durations: list[int]) -> bytes:
    buf = BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


def _run_gif2webp(gif_bytes: bytes, quality: int) -> bytes | None:
    assert GIF2WEBP_BIN is not None
    gif_path = webp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as gf:
            gf.write(gif_bytes)
            gif_path = gf.name
        webp_path = gif_path + ".webp"
        result = subprocess.run(
            [GIF2WEBP_BIN, "-lossy", "-q", str(quality), "-m", "4", gif_path, "-o", webp_path],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0 or not os.path.exists(webp_path):
            logger.warning("gif2webp returned %s: %s", result.returncode, result.stderr[:200])
            return None
        with open(webp_path, "rb") as f:
            return f.read()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("gif2webp invocation failed: %s", e)
        return None
    finally:
        for p in (gif_path, webp_path):
            if p and os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.unlink(p)


def _encode_animated_gif2webp(frames: list[Image.Image], durations: list[int]) -> bytes | None:
    # gif2webp reads a GIF, so we build a strided intermediate GIF (the
    # DCcon source is already ≤256-color GIF, so re-palettizing costs little)
    # and encode it. Same stride/quality search as the Pillow path, but
    # gif2webp's sub-frame compression fits 512×512 at high quality.
    _MAX_FRAMES_TARGET = 60
    initial_stride = max(1, len(frames) // _MAX_FRAMES_TARGET)
    candidate_strides = sorted(
        {initial_stride, initial_stride * 2, initial_stride * 3, initial_stride * 4}
    )
    for stride in candidate_strides:
        sub_frames = frames[::stride]
        if len(sub_frames) < 2:
            break
        gif_bytes = _frames_to_gif_bytes(sub_frames, _strided_durations(durations, stride))
        for quality in (85, 70, 55, 40):
            out = _run_gif2webp(gif_bytes, quality)
            if out is None:
                break  # gif2webp broken — bail to Pillow fallback
            if len(out) <= SIGNAL_MAX_BYTES:
                return out
    return None


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
    # Strategy with ANIM_SIZE=256: each frame at q=70 is ~3KB, so a 300KB
    # budget holds ~80 frames. _MAX_FRAMES_TARGET=70 keeps the initial
    # stride small enough to try stride=1 (= every source frame, max fps)
    # whenever the source is ≤70 frames. Quality search per stride probes
    # at q=40, then climbs to higher qualities only when there's headroom.
    _MAX_FRAMES_TARGET = 70
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

    def _sub(stride: int) -> tuple[list[Image.Image], list[int]] | None:
        sub_frames = frames[::stride]
        if len(sub_frames) < 2:
            return None
        sub_durations = [
            sum(max(d, MIN_FRAME_DURATION_MS) for d in durations[i : i + stride])
            for i in range(0, len(durations), stride)
        ]
        return sub_frames, sub_durations

    # Outer: stride (lowest first → more frames → smoother). For each
    # stride, probe at the lowest quality first; if even that doesn't fit
    # skip ahead. Otherwise climb the quality ladder, returning the
    # highest that fits. Headroom checks short-circuit obviously-doomed
    # upgrades so we don't waste encode passes.
    for stride in candidate_strides:
        sub = _sub(stride)
        if sub is None:
            break
        sub_frames, sub_durations = sub

        probe = _encode_webp(sub_frames, sub_durations, 40)
        if len(probe) > SIGNAL_MAX_BYTES:
            continue

        # Each quality step is roughly +15-25% file size. Try the highest
        # quality whose ratio to probe still fits.
        for quality, headroom_threshold in ((90, 0.35), (75, 0.5), (60, 0.7)):
            if len(probe) > SIGNAL_MAX_BYTES * headroom_threshold:
                continue
            out = _encode_webp(sub_frames, sub_durations, quality)
            if len(out) <= SIGNAL_MAX_BYTES:
                return out, "webp"
        return probe, "webp"

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
