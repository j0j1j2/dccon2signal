# Animated APNG Encoder Rebuild on apngasm + pngquant

**Status:** Design approved 2026-05-23
**Author:** cloudchamb3r
**Type:** Internal refactor of `dccon2signal.image_proc`

## 1. Purpose

Replace the current Pillow-based animated APNG encoder with a pipeline built on `apngasm` (frame-diff/disposal optimization) and `pngquant` (per-frame palette quantization). The current pipeline produces visibly choppy output on large DCcon packs (e.g. pack 168838: 92 stickers averaging 133 frames each at 200×200) because Pillow's APNG path stores full-frame palette-quantized images with a single shared palette, leaving very little room within Signal's 300 KiB per-sticker budget.

Empirical baseline (pack 168838, sticker 8 = 50 frames, 200×200, 967 KB source GIF):
the current encoder lands on roughly stride=2 / 32-colour palette. The new pipeline should land sticker 8 at stride=1 with 256 colours and remain under 300 KiB.

## 2. Goals & Non-Goals

**Goals**
- Up to 100 source frames: visually lossless (pngquant `--quality 95-100`, 256 colours, dithered) with **stride=1 guaranteed**.
- Over 100 source frames: degrade quality first, drop frames last (existing search philosophy).
- Same public surface (`process_sticker_bytes`, `process_pack`) — no caller changes.
- Single deterministic encoder path. No silent fallback to Pillow APNG.

**Non-Goals**
- No on-the-fly binary install. Operator installs `apngasm` and `pngquant` at deploy time.
- No multi-encoder selection logic (ffmpeg, etc.) — apngasm + pngquant only.
- No change to the static (non-animated) PNG path.
- No change to canvas size policy beyond what § 5.2 describes.
- No retry logic for transient subprocess failures — fail the sticker, log, move on (matches existing behaviour for malformed GIFs).

## 3. Quality Ladder

Search descends quality before touching frame count:

| Step | pngquant quality | Max colours | Dither | Stride |
|------|------------------|-------------|--------|--------|
| 1    | `--quality 95-100` | 256 | yes | 1 |
| 2    | `--quality 85-95`  | 192 | yes | 1 |
| 3    | `--quality 70-90`  | 128 | yes | 1 |
| 4    | `--quality 50-80`  | 64  | no  | 1 |
| 5    | `--quality 30-70`  | 32  | no  | 1 |
| 6+   | steps 1–5 repeated | …   | …    | 2, 3, 4, … |

Stride only escalates after every quality step at the current stride has been exhausted. The 100-frame lossless guarantee follows naturally from this ordering — for short sources, step 1 almost always fits and the loop returns immediately.

`pngquant --quality min-max` is "skip if quality would fall below min, otherwise emit at best achievable ≤ max." So step 1 either produces a 95+ result or refuses (we move on). We do **not** chain pngquant output back into pngquant — each step runs on the freshly assembled APNG.

## 4. Module Structure

### 4.1 New file: `src/dccon2signal/apng_encoder.py`

Pure subprocess wrapper. Exposes one function:

```python
def encode_animated_apng(
    frames: list[PIL.Image.Image],
    durations: list[int],
    max_bytes: int,
) -> bytes | None:
    """Return APNG bytes ≤ max_bytes, or None if no ladder step fits."""
```

Internally:
1. Validates `apngasm` and `pngquant` are on `PATH` (cached after first call).
2. For each ladder step:
   a. Compute `sub_frames`, `sub_durations` from stride.
   b. Create a `tempfile.TemporaryDirectory()`.
   c. Write each frame as `frame_NNNN.png` (four-digit, zero-padded) via Pillow.
   d. Build a delays file (`apngasm -af <delays-file>` format) listing per-frame durations.
   e. Run `apngasm -o out.apng <input dir>` to assemble.
   f. Run `pngquant --quality <range> --speed 3 [--nofs] -o final.apng out.apng`.
   g. Read `final.apng`; if `len(bytes) <= max_bytes`, return.
3. After exhausting the ladder, return `None`.

Stride values are produced lazily by an internal generator that mirrors the table in § 3.

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

Keep source dimensions, capped at 256 on the longest side:

- Source ≤ 256 on both axes → no resize.
- Source > 256 on either axis → LANCZOS downscale, preserve aspect, centre on 256×256 transparent canvas.

This is a change from the current always-256 NEAREST upscale. Rationale: with diff encoding, smaller canvas is pure win (fewer pixels per frame). For 200×200 DCcons (the dominant case) we save ≈39% pixels for free, and the loss of "Signal display size match" is minor because Signal scales at render time.

The NEAREST upscale path (`_fit_to_canvas_nearest`) becomes obsolete and is removed.

### 5.3 apngasm invocation

```
apngasm -o <out.apng> <tmpdir>/frame_%04d.png <tmpdir>/delays.txt
```

`delays.txt`: one line per frame, format `<numerator>/<denominator>` where the durations are in milliseconds expressed as `<ms>/1000`. apngasm interprets these as per-frame delays and computes optimal disposal/blend internally.

Loop count: `-l 0` (infinite).

### 5.4 pngquant invocation

```
pngquant --quality <min>-<max> --speed 3 [--nofs] --force -o <final.apng> <out.apng>
```

`--speed 3` balances quantization quality against runtime (default is 4; lower = better quality, slower).
`--nofs` (no Floyd-Steinberg dithering) is set on steps 4 and 5 in the ladder to lean on speed and on the visual "flat colour" character that already exists at low palette counts.
`--force` overwrites the output file silently.
pngquant handles animated PNGs natively since v2.18; we depend on ≥2.18.

### 5.5 Search termination

The loop stops when:
- A ladder step's output fits → return bytes.
- Stride would leave < 2 frames after `frames[::stride]` → return `None`. Caller (`_process_animated`) then falls back to a static PNG of frame 0 (current behaviour preserved).

### 5.6 Subprocess error handling

- `apngasm` or `pngquant` returns non-zero → `RuntimeError` containing stderr; bubbles up to `process_pack`'s per-sticker `try/except`, which logs `warning` and skips just that sticker. Pack continues.
- Binary missing at first call → `RuntimeError("apngasm not found on PATH; install via apt/brew")`. This is a hard configuration error: re-raised, not caught.

## 6. Deployment

- macOS dev: `brew install apngasm pngquant`
- Linux server (systemd unit): `apt install pngquant`; `apngasm` from binary release (Ubuntu apt has it on 22.04+ as `apngasm`). Update `deploy/dccon2signal-bot.service` `ExecStartPre=` doc or systemd unit `After=` chain only if needed (probably not — binaries on PATH are enough).
- README: add a "System dependencies" section listing the two binaries.
- pyproject.toml: no Python dep changes.

## 7. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| apngasm not packaged for some Linux distros | Document static-binary URL in README; bot service can mount the binary in its working dir if apt is unavailable. |
| pngquant version skew (animated PNG support added in 2.18) | Add a version probe in `apng_encoder.py` startup; raise with actionable message if too old. |
| Encoding latency for very long packs (200+ stickers × 133 frames) | apngasm + pngquant are 10-50× faster than the current Pillow palette-montage path; expected net speedup, not slowdown. Confirm with a timing measurement on pack 168838 after implementation. |
| Temporary disk usage (frames + intermediate APNGs) | `TemporaryDirectory` context guarantees cleanup; per-sticker peak is bounded by frames × pixel size (≈8 MB for a 200-frame 256² sticker). |

## 8. Open Questions

None at design time. Open during implementation:
- Exact apngasm CLI for delay file (verify against installed binary).
- Whether pngquant on already-quantized APNG is idempotent enough that we can avoid re-assembly when only the quality dial changes (likely yes; if so, the search loop can cache the assembled APNG per stride and only re-run pngquant).
