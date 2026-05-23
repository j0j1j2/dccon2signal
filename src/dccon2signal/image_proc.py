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
# Animated stickers go out as palette APNG (Signal animates APNG, not
# animated WebP — confirmed empirically against signalstickers.org
# community packs). APNG palette compression is dominated by colour count,
# not pixel area, so the canvas size barely affects file size; 320×320 is
# a comfortable middle that matches the official Signal animated packs.
ANIM_SIZE = 320
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

    # Signal animates APNG, not animated WebP — empirically confirmed against
    # a random sample of community "animated" packs from signalstickers.org:
    # 100% APNG, 0 animated WebP. Animated WebP renders as static on Signal
    # Android. So encode animated stickers as palette APNG (smaller files,
    # the same format the official animated packs use).
    out = _encode_animated_apng(raw_frames, durations)
    if out is not None:
        return out, "apng"

    # APNG didn't fit even at minimum settings — fall back to a static PNG
    # of the first frame so the upload still succeeds.
    logger.warning("APNG could not fit 300KB; falling back to static first frame")
    return _shrink_png_under_limit(raw_frames[0]), "png"


def _strided_durations(durations: list[int], stride: int) -> list[int]:
    # Each kept frame's duration = sum of source-frame durations it replaces,
    # with each source frame clamped to MIN_FRAME_DURATION_MS. Preserves total
    # playback duration under stride and caps fps at ~30.
    return [
        sum(max(d, MIN_FRAME_DURATION_MS) for d in durations[i : i + stride])
        for i in range(0, len(durations), stride)
    ]


def _quantize_to_shared_palette(frames: list[Image.Image], colors: int) -> list[Image.Image]:
    """Quantize every frame to a single shared N-color palette and bake
    transparency at palette index `colors`. Pillow's APNG encoder requires
    consistent mode/palette across frames; per-frame palettes break the
    inter-frame disposal step ("images do not match")."""
    size = frames[0].size
    montage = Image.new("RGB", (size[0], size[1] * len(frames)))
    for i, f in enumerate(frames):
        montage.paste(f.convert("RGB"), (0, i * size[1]))
    pal_img = montage.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)

    out: list[Image.Image] = []
    for f in frames:
        alpha = f.split()[-1]
        p = f.convert("RGB").quantize(palette=pal_img, dither=Image.Dither.NONE)
        # Reserve index `colors` for transparent pixels.
        mask = alpha.point(lambda x: 0 if x < 128 else 255)
        p.paste(colors, mask=Image.eval(mask, lambda x: 255 - x))
        p.info["transparency"] = colors
        out.append(p)
    return out


def _encode_apng(frames: list[Image.Image], durations: list[int], colors: int) -> bytes:
    quantized = _quantize_to_shared_palette(frames, colors)
    buf = BytesIO()
    # Note: no `disposal` kwarg — Pillow's APNG disposal step crashes with
    # "images do not match" on palette frames; omitting it produces the
    # working stream that Signal Android animates the same way the
    # community reference packs do.
    quantized[0].save(
        buf,
        format="PNG",
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
        transparency=colors,
        optimize=True,
    )
    return buf.getvalue()


def _encode_animated_apng(frames: list[Image.Image], durations: list[int]) -> bytes | None:
    # APNG palette compression: ~7-10KB per frame at 64 colors → 300KB ≈ 35
    # frames. Stride aggressively for long source GIFs; degrade palette
    # depth only when stride alone can't fit (preserve frame smoothness
    # before sacrificing colors).
    _MAX_FRAMES_TARGET = 35
    initial_stride = max(1, len(frames) // _MAX_FRAMES_TARGET)
    candidate_strides = sorted(
        {initial_stride, initial_stride * 2, initial_stride * 3, initial_stride * 4}
    )
    for stride in candidate_strides:
        sub_frames = frames[::stride]
        if len(sub_frames) < 2:
            break
        sub_durations = _strided_durations(durations, stride)
        for colors in (128, 64, 32):
            out = _encode_apng(sub_frames, sub_durations, colors)
            if len(out) <= SIGNAL_MAX_BYTES:
                return out
    return None


# Minimum per-frame display duration. 33ms ≈ 30fps cap. The actual playback
# fps in the output APNG is 1000 / per_frame_duration. Source frames whose
# declared duration is shorter than this (e.g. 30ms DCcon GIFs) get clamped
# so playback doesn't run faster than 30fps perceived.
MIN_FRAME_DURATION_MS = 33


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
