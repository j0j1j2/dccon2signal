//! Convert a GIF to an optimized animated PNG (APNG) under a size budget.
//!
//! Usage: `dccon-apng <input.gif> <max_bytes>`
//!
//! Pipeline:
//!   1. Decode all GIF frames into RGBA + per-frame durations.
//!   2. Normalize to a single canvas size (handles DCcons that grow/shrink
//!      per-frame, and bogus image-descriptor sizes).
//!   3. Walk a convert-to-apng-style compression ladder: quality bands first,
//!      then resolution scale-down, then frame stride as the last resort.
//!   4. Quantize all frames to a single shared palette via libimagequant
//!      and write the result as a palette-mode APNG (indexed pixels +
//!      tRNS for transparency).
//!
//! Stdout receives the chosen APNG bytes. Stderr receives progress logs.

use std::env;
use std::fs::File;
use std::io::{Cursor, Read, Write};
use std::process::ExitCode;

use anyhow::{Context, Result, anyhow, bail};
use png::{BitDepth, BlendOp, ColorType, DisposeOp};

const MIN_FRAME_DURATION_MS: u32 = 33;
const ZERO_DELAY_FALLBACK_MS: u32 = 100;
const ANIM_MAX_SIDE: u32 = 512;
// Mirrors the useful part of convert-to-apng's pngquant ladder:
// try progressively lower quality bands before sacrificing resolution
// or frame count.
const QUALITY_LADDER: &[QualityBand] = &[
    QualityBand { min: 80, max: 100 },
    QualityBand { min: 70, max: 90 },
    QualityBand { min: 60, max: 80 },
    QualityBand { min: 50, max: 70 },
    QualityBand { min: 0, max: 50 },
];

#[derive(Clone)]
struct RawFrame {
    /// Decoded RGBA pixels for the sub-rectangle (w*h*4 bytes).
    pixels: Vec<u8>,
    width: u32,
    height: u32,
    left: u32,
    top: u32,
    delay_ms: u32,
    dispose: gif::DisposalMethod,
}

#[derive(Clone)]
struct Frame {
    /// Composited canvas RGBA (canvas_side * canvas_side * 4 bytes).
    rgba: Vec<u8>,
    width: u32,
    height: u32,
    delay_ms: u32,
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
struct QualityBand {
    min: u8,
    max: u8,
}

fn gif_delay_ms(delay_cs: u16) -> u32 {
    if delay_cs == 0 {
        ZERO_DELAY_FALLBACK_MS
    } else {
        ((delay_cs as u32) * 10).max(MIN_FRAME_DURATION_MS)
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("dccon-apng: {e:#}");
            ExitCode::from(1)
        }
    }
}

fn run() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    if args.len() != 3 {
        bail!("usage: dccon-apng <input.gif> <max_bytes>");
    }
    let input_path = &args[1];
    let max_bytes: usize = args[2]
        .parse()
        .with_context(|| format!("max_bytes must be a positive integer, got {:?}", args[2]))?;

    let mut buf = Vec::new();
    File::open(input_path)
        .with_context(|| format!("opening {input_path}"))?
        .read_to_end(&mut buf)?;
    let frames = decode_gif(&buf).context("decoding GIF")?;
    if frames.len() < 2 {
        bail!("input has fewer than 2 frames; not animated");
    }
    eprintln!(
        "decoded {} frames at canvas {}x{}",
        frames.len(),
        frames[0].width,
        frames[0].height,
    );

    let apng = encode_ladder(&frames, max_bytes)
        .ok_or_else(|| anyhow!("no ladder step fit under {max_bytes} bytes"))?;
    std::io::stdout().write_all(&apng)?;
    Ok(())
}

fn decode_gif(buf: &[u8]) -> Result<Vec<Frame>> {
    let mut opts = gif::DecodeOptions::new();
    opts.set_color_output(gif::ColorOutput::RGBA);
    let mut decoder = opts.read_info(Cursor::new(buf))?;

    let canvas_w = decoder.width() as u32;
    let canvas_h = decoder.height() as u32;
    // The GIF header's declared canvas is the source of truth. Per-frame
    // image descriptors can claim wild dimensions/positions in DCcon files —
    // we treat anything that extends beyond the declared canvas as bogus
    // and discard the extra extent. Cap at ANIM_MAX_SIDE just for sanity.
    let canvas_side = canvas_w.max(canvas_h).min(ANIM_MAX_SIDE);
    // A frame is "sane" if its declared rectangle is fully within or within
    // 2× of the canvas — anything larger is metadata corruption.
    let sane_max = canvas_side.saturating_mul(2);

    let mut raws: Vec<RawFrame> = Vec::new();
    while let Some(frame) = decoder.read_next_frame()? {
        let w = frame.width as u32;
        let h = frame.height as u32;
        let left = frame.left as u32;
        let top = frame.top as u32;
        let delay_ms = gif_delay_ms(frame.delay);
        raws.push(RawFrame {
            pixels: frame.buffer.to_vec(),
            width: w,
            height: h,
            left,
            top,
            delay_ms,
            dispose: frame.dispose,
        });
        let _ = sane_max; // touched below in the compositing pass
    }

    let canvas_px = (canvas_side * canvas_side * 4) as usize;
    let mut compose = vec![0u8; canvas_px];
    let mut frames: Vec<Frame> = Vec::with_capacity(raws.len());
    let mut last_safe: Option<Frame> = None;

    for raw in raws {
        // Discard frames whose declared rectangle is wildly off (some
        // DCcons claim 27264×62464 in a stray descriptor) — substitute the
        // previous good frame to preserve playback timing.
        if raw.width > sane_max
            || raw.height > sane_max
            || raw.left.saturating_add(raw.width) > sane_max
            || raw.top.saturating_add(raw.height) > sane_max
        {
            if let Some(prev) = last_safe.as_ref() {
                let mut copy = prev.clone();
                copy.delay_ms = raw.delay_ms;
                frames.push(copy);
            }
            continue;
        }

        for y in 0..raw.height {
            for x in 0..raw.width {
                let cx = raw.left + x;
                let cy = raw.top + y;
                if cx >= canvas_side || cy >= canvas_side {
                    continue;
                }
                let sidx = ((y * raw.width + x) * 4) as usize;
                let didx = ((cy * canvas_side + cx) * 4) as usize;
                if sidx + 3 >= raw.pixels.len() {
                    continue;
                }
                let a = raw.pixels[sidx + 3];
                if a > 0 {
                    compose[didx] = raw.pixels[sidx];
                    compose[didx + 1] = raw.pixels[sidx + 1];
                    compose[didx + 2] = raw.pixels[sidx + 2];
                    compose[didx + 3] = a;
                }
            }
        }

        let snapshot = Frame {
            rgba: compose.clone(),
            width: canvas_side,
            height: canvas_side,
            delay_ms: raw.delay_ms,
        };
        last_safe = Some(snapshot.clone());
        frames.push(snapshot);

        match raw.dispose {
            gif::DisposalMethod::Background => {
                for y in 0..raw.height {
                    for x in 0..raw.width {
                        let cx = raw.left + x;
                        let cy = raw.top + y;
                        if cx >= canvas_side || cy >= canvas_side {
                            continue;
                        }
                        let didx = ((cy * canvas_side + cx) * 4) as usize;
                        compose[didx] = 0;
                        compose[didx + 1] = 0;
                        compose[didx + 2] = 0;
                        compose[didx + 3] = 0;
                    }
                }
            }
            gif::DisposalMethod::Previous => {
                if let Some(prev) = last_safe.as_ref() {
                    compose.copy_from_slice(&prev.rgba);
                }
            }
            _ => {}
        }
    }

    Ok(frames)
}

#[cfg(test)]
fn quality_ladder() -> Vec<QualityBand> {
    QUALITY_LADDER.to_vec()
}

fn canvas_ladder(source_side: u32) -> Vec<u32> {
    // convert-to-apng falls back through 90%, 80%, 70%, 60%, 50% size steps
    // after quality reduction. Keep the source size first so short animations
    // can preserve their original dimensions.
    let mut out = vec![source_side];
    for percent in [90u32, 80, 70, 60, 50] {
        let scaled = ((source_side * percent) + 50) / 100;
        if scaled >= 1 && out.last() != Some(&scaled) {
            out.push(scaled);
        }
    }
    out
}

fn resize_frames(frames: &[Frame], target_side: u32) -> Vec<Frame> {
    if frames[0].width == target_side && frames[0].height == target_side {
        return frames.to_vec();
    }
    let src_side = frames[0].width as usize;
    let dst_side = target_side as usize;
    frames
        .iter()
        .map(|f| {
            let mut dst = vec![0u8; dst_side * dst_side * 4];
            // Box-average downsample (cheap and stable for palette quantize).
            for dy in 0..dst_side {
                let sy0 = dy * src_side / dst_side;
                let sy1 = ((dy + 1) * src_side / dst_side).max(sy0 + 1);
                for dx in 0..dst_side {
                    let sx0 = dx * src_side / dst_side;
                    let sx1 = ((dx + 1) * src_side / dst_side).max(sx0 + 1);
                    let (mut r, mut g, mut b, mut a, mut n) = (0u32, 0u32, 0u32, 0u32, 0u32);
                    for sy in sy0..sy1 {
                        for sx in sx0..sx1 {
                            let i = (sy * src_side + sx) * 4;
                            r += f.rgba[i] as u32;
                            g += f.rgba[i + 1] as u32;
                            b += f.rgba[i + 2] as u32;
                            a += f.rgba[i + 3] as u32;
                            n += 1;
                        }
                    }
                    let i = (dy * dst_side + dx) * 4;
                    dst[i] = (r / n) as u8;
                    dst[i + 1] = (g / n) as u8;
                    dst[i + 2] = (b / n) as u8;
                    dst[i + 3] = (a / n) as u8;
                }
            }
            Frame {
                rgba: dst,
                width: target_side,
                height: target_side,
                delay_ms: f.delay_ms,
            }
        })
        .collect()
}

fn try_at(
    frames: &[Frame],
    delays: &[u32],
    max_bytes: usize,
    label_canvas: u32,
    label_stride: usize,
) -> Option<Vec<u8>> {
    let zopfli_rescue_cutoff = max_bytes + max_bytes / 6;
    let lowest_quality = *QUALITY_LADDER.last().expect("non-empty quality ladder");

    let mut min_fast: Option<Vec<u8>> = None;
    for &quality in QUALITY_LADDER {
        match encode_at_quality(frames, delays, quality, DeflateStrength::Fast) {
            Ok(out) => {
                if out.len() <= max_bytes {
                    eprintln!(
                        "fit: canvas={} stride={} quality={}-{} size={}",
                        label_canvas,
                        label_stride,
                        quality.min,
                        quality.max,
                        out.len()
                    );
                    return Some(out);
                }
                if quality == lowest_quality {
                    min_fast = Some(out);
                }
            }
            Err(e) => eprintln!(
                "encode error canvas={label_canvas} stride={label_stride} quality={}-{}: {e:#}",
                quality.min, quality.max
            ),
        }
    }
    if let Some(min_out) = min_fast {
        if min_out.len() <= zopfli_rescue_cutoff {
            if let Ok(rescued) =
                encode_at_quality(frames, delays, lowest_quality, DeflateStrength::Slow)
            {
                if rescued.len() <= max_bytes {
                    for &quality in &QUALITY_LADDER[..QUALITY_LADDER.len() - 1] {
                        if let Ok(out) =
                            encode_at_quality(frames, delays, quality, DeflateStrength::Slow)
                        {
                            if out.len() <= max_bytes {
                                eprintln!(
                                    "fit (zopfli): canvas={} stride={} quality={}-{} size={}",
                                    label_canvas,
                                    label_stride,
                                    quality.min,
                                    quality.max,
                                    out.len()
                                );
                                return Some(out);
                            }
                        }
                    }
                    eprintln!(
                        "fit (zopfli min): canvas={} stride={} quality={}-{} size={}",
                        label_canvas,
                        label_stride,
                        lowest_quality.min,
                        lowest_quality.max,
                        rescued.len()
                    );
                    return Some(rescued);
                }
            }
        }
    }
    None
}

fn encode_ladder(frames: &[Frame], max_bytes: usize) -> Option<Vec<u8>> {
    let source_side = frames[0].width;
    let canvases = canvas_ladder(source_side);

    // Pass 1: stride=1, walk canvases big→small. Both frames and colour
    // stay intact; only resolution shrinks. This is the preferred branch
    // for short-to-medium animations where the user wants smooth motion
    // AND decent colour fidelity (≥ 32 palette).
    let delays_stride_1: Vec<u32> = frames
        .iter()
        .map(|f| f.delay_ms.max(MIN_FRAME_DURATION_MS))
        .collect();
    for &canvas in &canvases {
        let resized = resize_frames(frames, canvas);
        if let Some(out) = try_at(&resized, &delays_stride_1, max_bytes, canvas, 1) {
            return Some(out);
        }
    }

    // Pass 2: at the smallest canvas, walk stride 2, 3, ... — last resort
    // for very long sources where even the small canvas can't hold every
    // frame within budget.
    let smallest = *canvases.last().unwrap();
    let resized = resize_frames(frames, smallest);
    let mut stride: usize = 2;
    loop {
        let sub: Vec<Frame> = resized.iter().step_by(stride).cloned().collect();
        if sub.len() < 2 {
            return None;
        }
        let sub_delays: Vec<u32> = (0..frames.len())
            .step_by(stride)
            .map(|i| {
                let end = (i + stride).min(frames.len());
                frames[i..end]
                    .iter()
                    .map(|f| f.delay_ms.max(MIN_FRAME_DURATION_MS))
                    .sum::<u32>()
            })
            .collect();
        if let Some(out) = try_at(&sub, &sub_delays, max_bytes, smallest, stride) {
            return Some(out);
        }
        stride += 1;
    }
}

#[derive(Copy, Clone)]
enum DeflateStrength {
    Fast,
    Slow,
}

/// Find the tightest axis-aligned bounding box of pixel indices that differ
/// between `prev` and `curr` (palette-mode buffers of size width*height).
/// Returns `(x, y, w, h)` or `None` if the frames are identical.
fn diff_bbox(prev: &[u8], curr: &[u8], width: u32, height: u32) -> Option<(u32, u32, u32, u32)> {
    let w = width as usize;
    let h = height as usize;
    let mut min_x = w;
    let mut max_x = 0usize;
    let mut min_y = h;
    let mut max_y = 0usize;
    for y in 0..h {
        let row = y * w;
        for x in 0..w {
            if prev[row + x] != curr[row + x] {
                if x < min_x {
                    min_x = x;
                }
                if x > max_x {
                    max_x = x;
                }
                if y < min_y {
                    min_y = y;
                }
                if y > max_y {
                    max_y = y;
                }
            }
        }
    }
    if min_x > max_x {
        return None;
    }
    Some((
        min_x as u32,
        min_y as u32,
        (max_x - min_x + 1) as u32,
        (max_y - min_y + 1) as u32,
    ))
}

fn encode_at_quality(
    frames: &[Frame],
    delays_ms: &[u32],
    quality: QualityBand,
    strength: DeflateStrength,
) -> Result<Vec<u8>> {
    let width = frames[0].width;
    let height = frames[0].height;

    let mut liq = imagequant::Attributes::new();
    liq.set_max_colors(256)
        .map_err(|e| anyhow!("imagequant set_max_colors(256): {e:?}"))?;
    liq.set_quality(quality.min, quality.max)
        .map_err(|e| anyhow!("imagequant set_quality({quality:?}): {e:?}"))?;
    liq.set_speed(5)
        .map_err(|e| anyhow!("imagequant set_speed: {e:?}"))?;

    let mut hist = imagequant::Histogram::new(&liq);
    for f in frames {
        let rgba: &[imagequant::RGBA] =
            unsafe { std::slice::from_raw_parts(f.rgba.as_ptr() as *const _, f.rgba.len() / 4) };
        let mut img = liq
            .new_image(rgba, width as usize, height as usize, 0.0)
            .map_err(|e| anyhow!("imagequant new_image: {e:?}"))?;
        hist.add_image(&liq, &mut img)
            .map_err(|e| anyhow!("imagequant hist.add_image: {e:?}"))?;
    }
    let mut result = hist
        .quantize(&liq)
        .map_err(|e| anyhow!("imagequant histogram quantize: {e:?}"))?;
    result
        .set_dithering_level(0.0)
        .map_err(|e| anyhow!("imagequant dither: {e:?}"))?;

    let palette = result.palette();
    let mut palette_rgb = Vec::with_capacity(palette.len() * 3);
    let mut palette_a = Vec::with_capacity(palette.len());
    for p in palette {
        palette_rgb.extend_from_slice(&[p.r, p.g, p.b]);
        palette_a.push(p.a);
    }

    let mut indexed_frames: Vec<Vec<u8>> = Vec::with_capacity(frames.len());
    for f in frames {
        let rgba: &[imagequant::RGBA] =
            unsafe { std::slice::from_raw_parts(f.rgba.as_ptr() as *const _, f.rgba.len() / 4) };
        let mut img = liq
            .new_image(rgba, width as usize, height as usize, 0.0)
            .map_err(|e| anyhow!("imagequant new_image (remap): {e:?}"))?;
        let (_, pixels) = result
            .remapped(&mut img)
            .map_err(|e| anyhow!("imagequant remap: {e:?}"))?;
        indexed_frames.push(pixels);
    }

    // Compute per-frame diff rectangle vs previous frame. The first frame is
    // written full-canvas; subsequent frames are written as the smallest
    // bounding box of pixels whose palette index differs from the previous
    // composite. With disposal=None + blend=Source this lays each diff on
    // top of the prior canvas, matching apngasm's space-saving behaviour.
    let mut raw = Vec::with_capacity(64 * 1024);
    {
        let mut encoder = png::Encoder::new(&mut raw, width, height);
        encoder.set_color(ColorType::Indexed);
        encoder.set_depth(BitDepth::Eight);
        encoder.set_palette(palette_rgb.as_slice());
        encoder.set_trns(palette_a.as_slice());
        encoder.set_animated(frames.len() as u32, 0)?;
        let mut writer = encoder.write_header()?;

        // First frame: full canvas.
        writer.set_frame_delay(delays_ms[0] as u16, 1000)?;
        writer.set_dispose_op(DisposeOp::None)?;
        writer.set_blend_op(BlendOp::Source)?;
        writer.write_image_data(&indexed_frames[0])?;

        for idx in 1..indexed_frames.len() {
            let prev = &indexed_frames[idx - 1];
            let curr = &indexed_frames[idx];
            let bbox = diff_bbox(prev, curr, width, height);
            let (x, y, w, h) = match bbox {
                Some(b) => b,
                // No pixels changed — still must emit a frame to keep timing.
                // Encode a 1×1 sub-rectangle reusing the corner pixel.
                None => (0u32, 0u32, 1u32, 1u32),
            };
            debug_assert!(
                x + w <= width && y + h <= height,
                "bbox {}+{}={} > {} or {}+{}={} > {}",
                x,
                w,
                x + w,
                width,
                y,
                h,
                y + h,
                height,
            );
            let mut sub = Vec::with_capacity((w * h) as usize);
            for row in 0..h {
                let start = (((y + row) * width) + x) as usize;
                sub.extend_from_slice(&curr[start..start + w as usize]);
            }
            writer.set_frame_delay(delays_ms[idx] as u16, 1000)?;
            writer.set_dispose_op(DisposeOp::None)?;
            writer.set_blend_op(BlendOp::Source)?;
            // Reset position to (0,0) before changing dimension — the png
            // crate validates dimension against the previously-set position
            // (which may carry over from the previous frame), so a small
            // dimension at a large position can fail the bounds check.
            writer.set_frame_position(0, 0)?;
            writer.set_frame_dimension(w, h).map_err(|e| {
                anyhow!(
                    "set_frame_dimension({w},{h}) at pos ({x},{y}) canvas {width}x{height}: {e}"
                )
            })?;
            writer.set_frame_position(x, y)?;
            writer.write_image_data(&sub)?;
        }
        writer.finish()?;
    }
    let opts = match strength {
        DeflateStrength::Fast => oxipng::Options {
            deflate: oxipng::Deflaters::Libdeflater { compression: 12 },
            optimize_alpha: true,
            ..oxipng::Options::from_preset(4)
        },
        DeflateStrength::Slow => oxipng::Options {
            deflate: oxipng::Deflaters::Zopfli {
                iterations: std::num::NonZeroU8::new(15).unwrap(),
            },
            optimize_alpha: true,
            ..oxipng::Options::from_preset(4)
        },
    };
    let optimized =
        oxipng::optimize_from_memory(&raw, &opts).map_err(|e| anyhow!("oxipng: {e}"))?;
    Ok(if optimized.len() < raw.len() {
        optimized
    } else {
        raw
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_gif_delay_uses_browser_style_fallback() {
        assert_eq!(gif_delay_ms(0), 100);
    }

    #[test]
    fn explicit_gif_delay_keeps_existing_minimum() {
        assert_eq!(gif_delay_ms(1), 33);
        assert_eq!(gif_delay_ms(4), 40);
        assert_eq!(gif_delay_ms(10), 100);
    }

    #[test]
    fn quality_ladder_matches_pngquant_style_bands() {
        let bands = quality_ladder();

        assert_eq!(
            bands,
            vec![
                QualityBand { min: 80, max: 100 },
                QualityBand { min: 70, max: 90 },
                QualityBand { min: 60, max: 80 },
                QualityBand { min: 50, max: 70 },
                QualityBand { min: 0, max: 50 },
            ]
        );
    }

    #[test]
    fn canvas_ladder_uses_codeberg_scale_percentages() {
        assert_eq!(canvas_ladder(512), vec![512, 461, 410, 358, 307, 256]);
        assert_eq!(canvas_ladder(200), vec![200, 180, 160, 140, 120, 100]);
    }
}
