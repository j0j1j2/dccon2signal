# Animated APNG Encoder Rebuild on apngasm + oxipng

**Status:** Design approved 2026-05-23 (revised after probing)
**Author:** cloudchamb3r
**Type:** Internal refactor of `dccon2signal.image_proc`

## 1. Purpose

Replace the current Pillow-based animated APNG encoder with a pipeline built on PIL shared-palette quantization → `apngasm` (assembly with inter-frame diff at the RGBA level) → `oxipng` (post-process re-deflate). The current Pillow encoder forces stride=2 on 50-frame stickers (e.g. pack 168838 sticker 8) because it stores each frame independently, leaving too little of Signal's 300 KiB per-sticker budget. The new pipeline keeps stride=1 by trading colour depth at the source canvas size.

Empirical baseline (pack 168838, sticker 8 = 50 frames, 200×200, 967 KB source GIF):
- Current Pillow encoder: 301 KiB at stride=2 (25 frames out, choppy)
- New pipeline at 200×200, shared 8-colour palette, no dither: 282 KiB at stride=1 (50 frames out, smooth)

## 1.1 Design changes from the initial pre-probe spec

Probing with the real binaries on the dev box (`apngasm 3.1.10`, `pngquant 3.0.3`,
`oxipng` for post-deflate) surfaced two facts that invalidated the initial plan:

1. **pngquant 3.0.3 collapses animated PNGs to a single static frame** when run on an APNG. The animation chunk (`acTL`) is dropped silently. We cannot use pngquant as the final quantize step on the assembled APNG.
2. **apngasm 3.1.10 normalises palette-mode input to RGBA internally** before doing the inter-frame diff. So feeding it palette PNGs gives the same file size as feeding RGBA frames. Per-frame pngquant gains us nothing once apngasm sees the frames.

The fix is to do palette quantization in Pillow with a single **shared** palette across the whole animation (the existing `_quantize_to_shared_palette` technique, kept), assemble with apngasm (which still beats Pillow's APNG output by a clear margin on the inter-frame diff), then re-deflate with oxipng. oxipng buys 10–15 % extra without re-quantizing.

Canvas size becomes **source size** (no 256×256 upscale). The earlier upscale was buying nothing — it just inflated pixel count and made the budget tighter. With diff encoding doing the heavy lifting, smaller canvas is pure win.

## 2. Goals & Non-Goals

**Goals**
- Up to 100 source frames: **stride=1 guaranteed**; quality (palette colour count) drops as far as needed to fit. The 300 KiB budget for 50-frame 200×200 content forces ≈ 8 colours in the worst case — accept it; user-validated as "frames over colours."
- Over 100 source frames: degrade colour first, drop frames last (existing search philosophy).
- Same public surface (`process_sticker_bytes`, `process_pack`) — no caller changes.
- Single deterministic encoder path. No silent fallback to Pillow APNG.

**Non-Goals**
- "Visually lossless" target. Probing showed it is impossible for the realistic case (50 frames × 200×200 within 300 KiB). The honest goal is **all frames preserved, palette as rich as the budget allows.**
- No on-the-fly binary install. Operator installs `apngasm` and `oxipng` at deploy time.
- No multi-encoder selection logic (ffmpeg, pngquant, etc.) — apngasm + oxipng only.
- No change to the static (non-animated) PNG path.
- No retry logic for transient subprocess failures — fail the sticker, log, move on (matches existing behaviour for malformed GIFs).

## 3. Quality Ladder

Search descends colour depth before touching frame count. All steps use the same shared-palette quantize (no dither) at the current stride:

| Step | Shared palette colours | Stride |
|------|------------------------|--------|
| 1    | 256                    | 1      |
| 2    | 128                    | 1      |
| 3    | 64                     | 1      |
| 4    | 32                     | 1      |
| 5    | 16                     | 1      |
| 6    | 8                      | 1      |
| 7+   | steps 1–6 repeated     | 2, 3, 4, … |

Stride only escalates after step 6 (8 colours) at the current stride fails. For ≤100 source frames this means every step 1–6 is tried before any frame is dropped, satisfying the user-stated "stride=1 guaranteed for ≤100 frames."

Dithering is off across the board because empirically dither at lower palette counts inflates the encoded file (more high-frequency noise → worse zlib).

## 4. Module Structure

### 4.1 New file: `src/dccon2signal/apng_encoder.py`

Pure subprocess wrapper. Exposes one function:

```python
def encode_animated_apng(
    frames: list[PIL.Image.Image],
    durations: list[int],
    max_bytes: int,
) -> bytes | None:
    """Return APNG bytes ≤ max_bytes, or None if no ladder step fits.
    Caller guarantees frames are already sized to the target canvas (this
    encoder does not resize)."""
```

Internally:
1. Validates `apngasm` and `oxipng` are on `PATH` (cached after first call).
2. Stride generator yields 1, 2, 3, … and we walk the colour ladder (256→8) at each.
3. For each (stride, colours) pair:
   a. Compute `sub_frames`, `sub_durations` via `image_proc._strided_durations`.
   b. Build a single shared palette (`_quantize_to_shared_palette` from image_proc, moved into this module since it is now the only caller).
   c. Open a `tempfile.TemporaryDirectory()`.
   d. Write each quantized frame as `frame_NNNN.png` (four-digit, zero-padded) with `optimize=True`.
   e. Run `apngasm -F -o out.apng <frame> <delay> <frame> <delay> … -l 0`. Delays are integer ms; apngasm interprets a bare integer as `ms/1000` of a second.
   f. Run `oxipng -o 4 --out final.apng out.apng`. (`-o 4` is the fast-but-effective preset; `--zopfli` is too slow for per-pack batch use — `-o 4` recovers most of the savings in a fraction of the time.)
   g. Read `final.apng`; if `len(bytes) <= max_bytes`, return.
4. Stop when `frames[::stride]` would yield < 2 frames; return `None`. Caller falls back to static PNG.

### 4.2 Modified: `src/dccon2signal/image_proc.py`

- `_process_animated`: call `apng_encoder.encode_animated_apng(...)` instead of `_encode_animated_apng`.
- Delete `_encode_apng`, `_encode_animated_apng`, `_quantize_to_shared_palette`. These are dead after the migration.
- `_strided_durations` and `_fit_to_canvas_nearest` stay — still used for frame prep.
- Static PNG path is untouched.

### 4.3 Tests: `tests/test_image_proc.py`

- Existing test `test_animated_gif_becomes_apng_under_limit` keeps its assertions (ext == "apng", size ≤ limit) but runs against the new encoder.
- New test: feed a synthetic 50-frame 200×200 GIF, assert frames-out == 50 (no stride loss).
- New test for missing binaries: monkeypatch `shutil.which` to return None, assert a clear `RuntimeError`.
- `pytest` markers: tests requiring real `apngasm`/`pngquant` get `@pytest.mark.requires_binaries`; CI / fresh dev envs without the tools skip them.

## 5. Detailed Behaviour

### 5.1 Frame extraction (unchanged)

Done in `_process_animated` via `PIL.ImageSequence.Iterator`. Each frame is RGBA, white-bg removal happens here if requested.

### 5.2 Canvas sizing

Keep source dimensions, capped at 512 on the longest side (well above realistic DCcon sizes; a guard, not a target):

- Source ≤ 512 on both axes → no resize. Pad with transparent pixels to the max-dim square only if the source is non-square.
- Source > 512 on either axis (very rare) → LANCZOS downscale, preserve aspect, centre on 512×512 transparent canvas.

This is a change from the current always-256 NEAREST upscale. Probing showed the upscale was the main reason 50-frame stickers couldn't fit: at 200×200 the same encoder pipeline fits, at 256×256 it doesn't. With diff-based encoding, fewer pixels is pure win and the "Signal display size match" loss is minor — Signal scales at render time.

The NEAREST upscale path (`_fit_to_canvas_nearest`) becomes obsolete and is removed.

### 5.3 apngasm invocation

```
apngasm -o <out.apng> <tmpdir>/frame_%04d.png <tmpdir>/delays.txt
```

`delays.txt`: one line per frame, format `<numerator>/<denominator>` where the durations are in milliseconds expressed as `<ms>/1000`. apngasm interprets these as per-frame delays and computes optimal disposal/blend internally.

Loop count: `-l 0` (infinite).

### 5.4 oxipng invocation

```
oxipng -o 4 --out <final.apng> <out.apng>
```

`-o 4` is the level-4 preset (fast, recovers ~80 % of zopfli's gains in ~5 % of the time). `--zopfli` is intentionally skipped — at pack-sized batch volumes the wall-clock difference matters. oxipng leaves the `acTL` and frame chunks alone; it only re-deflates IDAT/fdAT streams.

### 5.5 Search termination

The loop stops when:
- A ladder step's output fits → return bytes.
- Stride would leave < 2 frames after `frames[::stride]` → return `None`. Caller (`_process_animated`) then falls back to a static PNG of frame 0 (current behaviour preserved).

### 5.6 Subprocess error handling

- `apngasm` or `pngquant` returns non-zero → `RuntimeError` containing stderr; bubbles up to `process_pack`'s per-sticker `try/except`, which logs `warning` and skips just that sticker. Pack continues.
- Binary missing at first call → `RuntimeError("apngasm not found on PATH; install via apt/brew")`. This is a hard configuration error: re-raised, not caught.

## 6. Deployment

- macOS dev: `brew install apngasm oxipng`
- Linux server (systemd unit): `apt install apngasm oxipng` (both packaged on Ubuntu 22.04+). No `ExecStartPre=` needed — binaries on PATH are enough.
- README: add a "System dependencies" section listing the two binaries.
- pyproject.toml: no Python dep changes.

## 7. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| apngasm not packaged for some Linux distros | Document static-binary URL in README; bot service can mount the binary in its working dir if apt is unavailable. |
| Encoding latency for very long packs (200+ stickers × 133 frames) | The current Pillow path is the slow one (large palette-montage quantize per attempt). The new pipeline does roughly the same Pillow palette work, plus a fast apngasm assembly and a fast oxipng `-o 4` pass — net should be similar or faster. Confirm with a timing measurement on pack 168838 after implementation. |
| Temporary disk usage (frames + intermediate APNGs) | `TemporaryDirectory` context guarantees cleanup; per-sticker peak is bounded by frames × pixel size (≈8 MB for a 200-frame 200² sticker). |
| Source GIFs larger than 512 on a side (rare) | LANCZOS downscale to 512×512 max canvas (§ 5.2). |

## 8. Open Questions

Resolved during probing — none left at implementation time.
