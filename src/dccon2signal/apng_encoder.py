"""Animated APNG encoder using `apngasm` + `oxipng`.

Replaces Pillow's APNG encoder, which stores each frame independently and
forces frame drops on dense animations to stay under Signal's 300 KiB limit.
This module pre-quantizes frames to a single shared palette in Pillow, hands
them to apngasm (which computes inter-frame diffs at the RGBA level), and
runs oxipng to re-deflate. On 50-frame 200×200 DCcon stickers this preserves
every frame where the old encoder lost half.

The encoder does not resize: the caller delivers frames at the target canvas.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

from PIL import Image

logger = logging.getLogger(__name__)

# Colour ladder for the shared palette. Step 1 (256) is rich; step 6 (8) is
# flat-but-smooth — picked empirically because 50-frame 200×200 DCcons fit at
# 8 colours and stop fitting around 16.
_COLOR_LADDER: tuple[int, ...] = (256, 128, 64, 32, 16, 8)

_BIN_APNGASM = "apngasm"
_BIN_OXIPNG = "oxipng"


class EncoderUnavailable(RuntimeError):
    """Raised at startup when a required binary is missing on PATH."""


def _check_binaries() -> None:
    missing = [b for b in (_BIN_APNGASM, _BIN_OXIPNG) if shutil.which(b) is None]
    if missing:
        raise EncoderUnavailable(
            f"required binaries not on PATH: {', '.join(missing)} — "
            f"install via 'brew install {' '.join(missing)}' (macOS) "
            f"or 'apt install {' '.join(missing)}' (Ubuntu 22.04+)"
        )


def _quantize_to_shared_palette(
    frames: list[Image.Image], colors: int
) -> list[Image.Image]:
    """Quantize every frame to a single shared N-color palette and reserve
    palette index `colors` for transparency. The shared palette is computed
    by stacking all frames into a montage and running MEDIANCUT once."""
    size = frames[0].size
    montage = Image.new("RGB", (size[0], size[1] * len(frames)))
    for i, f in enumerate(frames):
        montage.paste(f.convert("RGB"), (0, i * size[1]))
    pal_img = montage.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)

    out: list[Image.Image] = []
    for f in frames:
        alpha = f.split()[-1]
        p = f.convert("RGB").quantize(palette=pal_img, dither=Image.Dither.NONE)
        mask = alpha.point(lambda x: 0 if x < 128 else 255)
        p.paste(colors, mask=Image.eval(mask, lambda x: 255 - x))
        p.info["transparency"] = colors
        out.append(p)
    return out


def _strided(values: list, stride: int) -> list:
    return values[::stride]


def _coalesced_durations(durations: list[int], stride: int, min_ms: int) -> list[int]:
    """Per kept frame, sum the durations of source frames it replaces, with
    each source duration clamped to `min_ms` (so playback can't run faster
    than 1000/min_ms fps perceived). Total playback duration is preserved."""
    return [
        sum(max(d, min_ms) for d in durations[i : i + stride])
        for i in range(0, len(durations), stride)
    ]


def _run_apngasm(frame_paths: list[str], delays_ms: list[int], out_path: str) -> None:
    cmd: list[str] = [_BIN_APNGASM, "-F", "-o", out_path]
    for path, delay in zip(frame_paths, delays_ms, strict=True):
        cmd.append(path)
        cmd.append(str(delay))
    cmd.extend(["-l", "0"])  # infinite loop
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"apngasm failed: {res.stderr.decode(errors='replace')[:500]}")


def _run_oxipng(in_path: str, out_path: str) -> None:
    cmd = [_BIN_OXIPNG, "-o", "4", "--out", out_path, in_path]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"oxipng failed: {res.stderr.decode(errors='replace')[:500]}")


# oxipng saves ~10-15 % over apngasm's raw output. So a raw candidate that
# is more than this factor over budget can never fit even after oxipng —
# skip the expensive optimize step and move to the next ladder rung.
_OXIPNG_SAVINGS_HEADROOM = 0.85


def _encode_one(
    frames: list[Image.Image],
    delays_ms: list[int],
    tmpdir: str,
    max_bytes: int,
) -> bytes | None:
    """Return finalized APNG bytes ≤ max_bytes, or None if even after the
    oxipng pass this configuration won't fit. Skips the oxipng step entirely
    when the raw apngasm output is already too big to recover."""
    frame_paths: list[str] = []
    for i, f in enumerate(frames):
        path = os.path.join(tmpdir, f"frame_{i:04d}.png")
        f.save(path, optimize=True)
        frame_paths.append(path)
    raw = os.path.join(tmpdir, "raw.apng")
    _run_apngasm(frame_paths, delays_ms, raw)
    raw_size = os.path.getsize(raw)
    # Already small enough — don't pay for oxipng.
    if raw_size <= max_bytes:
        with open(raw, "rb") as fh:
            return fh.read()
    # Even with oxipng's 10-15 % savings we wouldn't fit — bail.
    if raw_size * _OXIPNG_SAVINGS_HEADROOM > max_bytes:
        return None
    final = os.path.join(tmpdir, "final.apng")
    _run_oxipng(raw, final)
    final_size = os.path.getsize(final)
    if final_size > max_bytes:
        return None
    with open(final, "rb") as fh:
        return fh.read()


def _try(
    frames: list[Image.Image], durations: list[int], colors: int, max_bytes: int
) -> bytes | None:
    quantized = _quantize_to_shared_palette(frames, colors)
    with tempfile.TemporaryDirectory(prefix="dccon-apng-") as td:
        return _encode_one(quantized, durations, td, max_bytes)


def encode_animated_apng(
    frames: list[Image.Image],
    durations: list[int],
    max_bytes: int,
    *,
    min_frame_duration_ms: int,
) -> bytes | None:
    """Encode `frames` to an APNG ≤ `max_bytes`.

    At each stride: probes the smallest palette (8 colours) first as a cheap
    fit-or-bust check. If even 8 colours overflows, escalates stride
    immediately. Once a fit is found, walks the ladder UP to recover the
    richest palette that still fits. Returns `None` if `frames[::stride]`
    would drop below 2 frames before fitting — caller falls back to a static
    PNG.
    """
    _check_binaries()
    if len(frames) < 2:
        return None
    if len(frames) != len(durations):
        raise ValueError("frames and durations must be the same length")

    # Ladder walked top-down once a probe fits: skip the first entry (8) since
    # the probe already produced it, then try richer palettes in descending
    # priority and keep the latest that fits.
    upgrade_ladder = tuple(c for c in _COLOR_LADDER if c != 8)  # (256, 128, 64, 32, 16)

    stride = 1
    while True:
        sub_frames = _strided(frames, stride)
        if len(sub_frames) < 2:
            return None
        sub_durations = _coalesced_durations(durations, stride, min_frame_duration_ms)

        probe = _try(sub_frames, sub_durations, 8, max_bytes)
        if probe is None:
            stride += 1
            continue
        best = probe
        best_colors = 8
        # Walk down from 256: as soon as a richer palette fits, keep it.
        for colors in upgrade_ladder:
            cand = _try(sub_frames, sub_durations, colors, max_bytes)
            if cand is not None:
                best = cand
                best_colors = colors
                break
        logger.debug(
            "apng fit: stride=%d colors=%d size=%d", stride, best_colors, len(best)
        )
        return best


__all__ = (
    "EncoderUnavailable",
    "encode_animated_apng",
)
