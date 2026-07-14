#!/usr/bin/env python
"""make_launch_reel.py - builds the public launch reel for Breakdown Studio.

Cuts a ~30-34s vertical (9:16) and horizontal (16:9) trailer from the dressed Tears of Steel demo
(see dress_film.py), the app screenshots, and the 8K contact sheet, with PIL-rendered title cards
and captions composited over the footage. Ships no client/project data: every path is passed in via
CLI args or read from a config file you point at explicitly, never hardcoded.

Why PIL instead of ffmpeg drawtext: drawtext segfaults on the ffmpeg build this pipeline uses.
Every piece of text in this reel is rendered to a transparent RGBA PNG (or a short PNG sequence,
for text that needs to fade) with Pillow, then composited onto the footage with ffmpeg's `overlay`
filter. No drawtext/ass/subtitles filter is used anywhere in this script.

Usage:
  python make_launch_reel.py \\
      --source /path/to/tos_dressed.mp4 \\
      --contact-sheet /path/to/tos_contact_sheet_8k.jpg \\
      --screenshot-running site/assets/screenshot_running.png \\
      --ffmpeg <path from config.json: ffmpeg> \\
      --ffprobe <path from config.json: ffprobe> \\
      --out-9x16 site/assets/launch_reel_9x16.mp4 \\
      --out-16x9 site/assets/launch_reel_16x9.mp4

  (tos_dressed.mp4 and tos_contact_sheet_8k.jpg are produced by dress_film.py -- see that
  script's docstring; they're large re-encodes of third-party footage and stay on local disk,
  not in the repo)

Requires: Pillow, an ffmpeg/ffprobe binary (paths via --ffmpeg/--ffprobe, never hardcoded here).
Does NOT read config.json directly (same "runnable standalone" convention as dress_film.py /
make_demo_cut.py) - pass binary paths explicitly, or wrap this call in a small script that reads
your local config.json for them.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = Path(__file__).resolve().parent

ATTRIBUTION = "Footage: (CC) Blender Foundation | mango.blender.org"

FONT_CANDIDATES_BOLD = [
    "arialbd.ttf",
    "Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "arial.ttf",
    "Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _font(size, bold=True):
    for c in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default()


def run(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(
            "command failed: %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (" ".join(str(c) for c in cmd), r.stdout[-4000:], r.stderr[-4000:])
        )
    return r


def ffprobe_json(ffprobe, path):
    r = run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    return json.loads(r.stdout)


# =============================================================================================
# Text rendering: every text element is drawn with PIL onto a transparent RGBA canvas at the
# TARGET resolution, then written out as one or more PNGs. Fades are done by generating N PNG
# frames with ramped alpha (image2 sequence), never with an ffmpeg text/alpha filter.
# =============================================================================================

def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_text_block(canvas_size, lines, font, fill=(255, 255, 255, 255), align="center",
                     anchor_y="center", margin_frac=0.08, line_spacing=1.25,
                     shadow=True, y_override=None, scrim=False):
    """Draws a multi-line text block onto a transparent RGBA image, safe-margined.
    anchor_y: 'top' | 'center' | 'bottom' -- vertical placement within the safe area.
    scrim: draws a soft dark rounded panel behind the text first -- for text sitting over busy
    photographic/thumbnail imagery where a drop shadow alone isn't enough contrast.
    Returns the RGBA Image.
    """
    w, h = canvas_size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    margin_x = int(w * margin_frac)
    margin_y = int(h * margin_frac)
    safe_top, safe_bottom = margin_y, h - margin_y

    line_h = int(font.size * line_spacing)
    block_h = line_h * len(lines)

    if y_override is not None:
        top = y_override
    elif anchor_y == "top":
        top = safe_top
    elif anchor_y == "bottom":
        top = safe_bottom - block_h
    else:
        top = (h - block_h) // 2

    if scrim:
        widest = max((d.textlength(line, font=font) for line in lines), default=0)
        pad_x, pad_y = int(font.size * 0.9), int(font.size * 0.55)
        sx0 = (w - widest) / 2 - pad_x
        sx1 = (w + widest) / 2 + pad_x
        sy0 = top - pad_y
        sy1 = top + block_h - (line_h - font.size) + pad_y
        radius = int(font.size * 0.4)
        d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=radius, fill=(8, 8, 12, 165))

    for i, line in enumerate(lines):
        tw = d.textlength(line, font=font)
        if align == "center":
            x = (w - tw) / 2
        elif align == "left":
            x = margin_x
        else:
            x = w - margin_x - tw
        y = top + i * line_h

        if shadow:
            off = max(2, font.size // 22)
            d.text((x + off, y + off), line, font=font, fill=(0, 0, 0, 160))
            d.text((x + off * 2, y + off * 2), line, font=font, fill=(0, 0, 0, 90))
        d.text((x, y), line, font=font, fill=fill)

    return img


def render_fade_sequence(seq_dir, canvas_size, text_states, fps, seg_frames, font_size_frac=0.062,
                          align="center", anchor_y="center", y_override_frac=None, bold=True,
                          fill=(255, 255, 255, 255), max_width_frac=0.82, fade_frames=6,
                          hold_pad_frames=0, scrim=False):
    """text_states: list of (start_frame, end_frame, text_or_None) within the segment's local
    frame range [0, seg_frames). Each state's text fades in over `fade_frames`, holds, fades out
    over `fade_frames` before the next state (or stays if end_frame == seg_frames). Writes one
    PNG per output frame into seq_dir (0-padded, frame_%06d.png) and returns seq_dir.
    scrim: see draw_text_block -- adds a dark panel behind the text for legibility over busy imagery.
    """
    seq_dir.mkdir(parents=True, exist_ok=True)
    w, h = canvas_size
    font = _font(int(h * font_size_frac), bold=bold)
    max_w = int(w * max_width_frac)
    y_override = int(h * y_override_frac) if y_override_frac is not None else None

    # Pre-render each distinct text block once.
    blocks = {}
    for _, _, text in text_states:
        if text is None or text in blocks:
            continue
        probe = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(probe)
        lines = wrap_text(d, text, font, max_w)
        blocks[text] = draw_text_block(canvas_size, lines, font, fill=fill, align=align,
                                        anchor_y=anchor_y, y_override=y_override, scrim=scrim)

    for f in range(seg_frames):
        alpha_mult = 0.0
        active_text = None
        for (s, e, text) in text_states:
            if s <= f < e:
                active_text = text
                local = f - s
                dur = e - s
                fin = min(fade_frames, dur // 2 if dur > 0 else 0)
                fout = min(fade_frames, dur // 2 if dur > 0 else 0)
                if local < fin:
                    alpha_mult = local / max(1, fin)
                elif local >= dur - fout:
                    alpha_mult = (dur - local) / max(1, fout)
                else:
                    alpha_mult = 1.0
                break
        out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        if active_text is not None and alpha_mult > 0.001:
            block = blocks[active_text]
            if alpha_mult < 0.999:
                a = block.split()[3].point(lambda p, m=alpha_mult: int(p * m))
                block = block.copy()
                block.putalpha(a)
            out.alpha_composite(block)
        out.save(seq_dir / ("frame_%06d.png" % f))
    return seq_dir


# =============================================================================================
# Segment builders. Each returns a path to a rendered H.264 mp4 segment at (w,h)@fps, silent
# (audio is muxed once at the very end from the score bed), with text already composited in.
# =============================================================================================

def band_geometry(w, h, source_aspect, band_frac):
    """For 'letterbox'/banded compositions: the footage is scaled to occupy `band_frac` of the
    canvas's shorter dimension of travel (its width, always == w), producing a centered band.
    Returns (band_w, band_h, band_top, band_bottom) in canvas pixels -- callers use band_top/
    band_bottom to place caption text just outside the band instead of guessing a y-fraction
    blind to the actual band size.
    """
    band_w = w
    band_h = int(round(band_w / source_aspect))
    max_band_h = int(h * band_frac)
    if band_h > max_band_h:
        band_h = max_band_h
        band_w = int(round(band_h * source_aspect))
    band_top = (h - band_h) // 2
    band_bottom = band_top + band_h
    return band_w, band_h, band_top, band_bottom


def build_video_base(ffmpeg, source, start, dur, w, h, fps, out_path, mode="letterbox",
                      zoom=None, crop_focus="center", strip_burnin_bars=None, band_frac=0.62):
    """Extracts `dur` seconds from `source` starting at `start`, scaled/cropped to (w,h).
    mode: 'letterbox' (centered band, black above/below, sized via band_frac -- see
    band_geometry()) or 'fill' (center-crop to fill the whole frame).
    zoom: optional (start_scale, end_scale) for a slow linear push-in over the clip (fill mode only).
    strip_burnin_bars: pixel height of the source's own top/bottom letterbox+burn-in bars to crop
    away before scaling (used for 'fill' hook/montage shots, so the editorial slate/note/TC/
    attribution burn-in baked into the demo footage never bleeds into a pure-footage crop; the
    'letterbox' mode shots keep the bars, since showing the burn-in there is the point).
    band_frac: for 'letterbox' mode, the max fraction of canvas HEIGHT the band may occupy
    (only matters when the canvas is much taller than the source, e.g. 9x16 vertical -- keeps
    the band a deliberate, substantial size instead of a thin strip with dead space around it).
    """
    pre = ""
    if strip_burnin_bars:
        pre = f"crop=iw:ih-{2*strip_burnin_bars}:0:{strip_burnin_bars},"

    if mode == "fill" and zoom:
        z0, z1 = zoom
        n = int(round(dur * fps))
        # zoompan needs an oversized source; scale up first so cropping never runs out of pixels.
        vf = (
            f"{pre}scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
            f"crop={w*2}:{h*2},"
            f"zoompan=z='{z0}+({z1}-{z0})*on/{max(1,n-1)}':d=1:x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}"
        )
    elif mode == "fill":
        vf = f"{pre}scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps}"
    else:  # letterbox: scale to a deliberately-sized centered band, not just "fit inside canvas"
        src_aspect = 1920.0 / (800.0 - (2 * strip_burnin_bars if strip_burnin_bars else 0))
        band_w, band_h, band_top, _ = band_geometry(w, h, src_aspect, band_frac)
        vf = (f"{pre}scale={band_w}:{band_h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:{band_top}:black,fps={fps}")

    run([ffmpeg, "-y", "-ss", str(start), "-t", str(dur), "-i", source,
         "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", str(out_path), "-loglevel", "error"])
    return out_path


def build_image_base(ffmpeg, image_path, dur, w, h, fps, out_path, mode="fill", zoom=None,
                      focus="center", pre_crop=None, fit_frac=0.7):
    """Ken-Burns an image over `dur` seconds. zoom=(z0,z1) linear push. focus picks the crop
    anchor when the source aspect doesn't match target. pre_crop=(x,y,cw,ch) crops the source
    image (in source pixels) before the zoompan, e.g. to start tight on a contact-sheet region.
    mode: 'fill' (center-crop to fill the whole frame, for photographic footage) or 'fit'
    (the WHOLE image stays visible, letterboxed onto a dark backdrop sized via fit_frac of the
    canvas's shorter side -- for UI screenshots, where cropping into the picture makes on-screen
    text illegible; leaves real margin for a caption to sit on the dark backdrop, off the UI).
    """
    src = image_path
    tmp_pre = None
    if pre_crop:
        img = Image.open(image_path).convert("RGB")
        x, y, cw, ch = pre_crop
        img = img.crop((x, y, x + cw, y + ch))
        tmp_pre = out_path.with_suffix(".precrop.jpg")
        img.save(tmp_pre, quality=95)
        src = tmp_pre

    n = int(round(dur * fps))
    z0, z1 = zoom if zoom else (1.0, 1.0)

    if mode == "fit":
        # Pad the (never-cropped) image onto a dark canvas sized to fit_frac of the shorter
        # canvas dimension, then apply the zoom as a gentle push on the WHOLE padded canvas
        # (so the screenshot itself is never cropped, only the surrounding dark space shrinks).
        short_dim = min(w, h)
        target = int(short_dim * fit_frac)
        vf = (
            f"scale={target}:{target}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:0x12121A,"
            f"scale={int(w*1.3)}:{int(h*1.3)},"
            f"zoompan=z='{z0}+({z1}-{z0})*on/{max(1,n-1)}':d=1:x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}"
        )
    else:
        anchor = {
            "center": ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            "top": ("iw/2-(iw/zoom/2)", "0"),
            "bottom": ("iw/2-(iw/zoom/2)", "ih-ih/zoom"),
        }.get(focus, ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"))
        vf = (
            f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
            f"crop={w*2}:{h*2},"
            f"zoompan=z='{z0}+({z1}-{z0})*on/{max(1,n-1)}':d=1:x='{anchor[0]}':y='{anchor[1]}':"
            f"s={w}x{h}:fps={fps}"
        )
    run([ffmpeg, "-y", "-loop", "1", "-i", src, "-t", str(dur),
         "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", str(out_path), "-loglevel", "error"])
    if tmp_pre and tmp_pre.exists():
        tmp_pre.unlink()
    return out_path


def build_solid_base(ffmpeg, color, dur, w, h, fps, out_path):
    run([ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}:d={dur}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
         str(out_path), "-loglevel", "error"])
    return out_path


def build_blurred_still_base(ffmpeg, source, at, dur, w, h, fps, out_path, darken=0.32, blur=18):
    """A single frame from `source`, blurred + darkened, held for `dur` -- used as the PROOF
    section background so stat lines read clearly over it."""
    frame_path = out_path.with_suffix(".src.jpg")
    run([ffmpeg, "-y", "-ss", str(at), "-i", source, "-frames:v", "1", "-q:v", "2",
         str(frame_path), "-loglevel", "error"])
    img = Image.open(frame_path).convert("RGB")
    img = img.filter(ImageFilter.GaussianBlur(blur))
    # darken
    dark = Image.new("RGB", img.size, (0, 0, 0))
    img = Image.blend(img, dark, 1.0 - darken)
    still_path = out_path.with_suffix(".still.jpg")
    img.save(still_path, quality=92)
    run([ffmpeg, "-y", "-loop", "1", "-i", still_path, "-t", str(dur),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps}",
         "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
         str(out_path), "-loglevel", "error"])
    frame_path.unlink(missing_ok=True)
    still_path.unlink(missing_ok=True)
    return out_path


def composite_overlay(ffmpeg, base_path, seq_dir, fps, out_path):
    run([ffmpeg, "-y", "-i", base_path, "-framerate", str(fps), "-i",
         str(seq_dir / "frame_%06d.png"),
         "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]",
         "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", str(out_path), "-loglevel", "error"])
    return out_path


def flash_frames_overlay(ffmpeg, ffprobe, clip_path, w, h, fps, out_path, flash_frames=2):
    """Overlays a brief white flash on the LAST `flash_frames` frames of clip_path (in place)."""
    n = flash_frames
    flash_dir = out_path.with_suffix("")
    flash_dir.mkdir(parents=True, exist_ok=True)
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    clear = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    info = ffprobe_frame_count(ffprobe, clip_path)
    for f in range(info):
        img = white if f >= info - n else clear
        img.save(flash_dir / ("frame_%06d.png" % f))
    composite_overlay(ffmpeg, clip_path, flash_dir, fps, out_path)
    shutil.rmtree(flash_dir, ignore_errors=True)
    return out_path


def ffprobe_frame_count(ffprobe, clip_path):
    """Reliable frame count via ffprobe (counting packets is fast and exact for a local mp4;
    avoids depending on ffmpeg's stderr progress format, which varies by build/loglevel)."""
    r = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(clip_path)])
    return int(r.stdout.strip())


# =============================================================================================
# Timeline definition
# =============================================================================================

def seconds_to_frames(s, fps):
    return int(round(s * fps))


def build_timeline(orientation, w, h, fps, args, work):
    """Returns an ordered list of segment mp4 paths (already text-composited, silent)."""
    segs = []
    vertical = orientation == "9x16"

    def seg_path(name):
        return work / f"{orientation}_{name}.mp4"

    # --- 0.0-4.0 HOOK -------------------------------------------------------------------
    hook_dur = 4.0
    p = seg_path("01_hook")
    if vertical:
        build_video_base(args.ffmpeg, args.source, args.hook_a_start, hook_dur, w, h, fps, p,
                          mode="fill", zoom=(1.0, 1.06), strip_burnin_bars=90)
    else:
        build_video_base(args.ffmpeg, args.source, args.hook_a_start, hook_dur, w, h, fps, p,
                          mode="fill", zoom=(1.0, 1.08), strip_burnin_bars=90)
    seg_frames = seconds_to_frames(hook_dur, fps)
    swap = seconds_to_frames(2.5, fps)
    states = [
        (0, swap, "A production hands you the cut."),
        (swap, seg_frames, "They need a number."),
    ]
    seqdir = work / f"{orientation}_01_hook_txt"
    render_fade_sequence(seqdir, (w, h), states, fps, seg_frames,
                          font_size_frac=0.042 if vertical else 0.05,
                          anchor_y="bottom", y_override_frac=0.62 if vertical else 0.72,
                          fade_frames=6, max_width_frac=0.7)
    out = seg_path("01_hook_txt")
    composite_overlay(args.ffmpeg, p, seqdir, fps, out)
    segs.append(out)

    # --- 4.0-8.5 PROBLEM -----------------------------------------------------------------
    prob_dur = 4.5
    p = seg_path("02_problem")
    band_frac = 0.42 if vertical else 0.85
    build_video_base(args.ffmpeg, args.source, args.problem_start, prob_dur, w, h, fps, p,
                      mode="letterbox", band_frac=band_frac)
    _, _, band_top, band_bottom = band_geometry(w, h, 1920.0 / 800.0, band_frac)
    seg_frames = seconds_to_frames(prob_dur, fps)
    swap = seconds_to_frames(2.3, fps)
    states = [
        (0, swap, "The VFX breakdown: weeks of hand-logging."),
        (swap, seg_frames, "Thrown away on every re-cut."),
    ]
    seqdir = work / f"{orientation}_02_problem_txt"
    if vertical:
        # first line lives in the gap above the band, second in the gap below -- both states
        # share the SAME y so there's no jump-cut in text position when they swap.
        y_frac = (band_top * 0.42) / h
    else:
        y_frac = 0.84
    render_fade_sequence(seqdir, (w, h), states, fps, seg_frames,
                          font_size_frac=0.044 if vertical else 0.04,
                          anchor_y="top" if vertical else "bottom",
                          y_override_frac=y_frac,
                          fade_frames=6, max_width_frac=0.82)
    out = seg_path("02_problem_txt")
    composite_overlay(args.ffmpeg, p, seqdir, fps, out)
    segs.append(out)

    # --- 8.5-13.5 TURN: fast montage --------------------------------------------------
    turn_total = 5.0
    turn_starts = args.turn_starts
    n_shots = len(turn_starts)
    per_shot = turn_total / n_shots
    flash_f = 2
    turn_clip_paths = []
    for i, st in enumerate(turn_starts):
        cp = seg_path(f"03_turn_{i}")
        build_video_base(args.ffmpeg, args.source, st, per_shot, w, h, fps, cp,
                          mode="fill" if vertical else "letterbox",
                          zoom=(1.0, 1.1) if vertical else None,
                          strip_burnin_bars=90 if vertical else None)
        if i < n_shots - 1:
            cp2 = seg_path(f"03_turn_{i}_flash")
            flash_frames_overlay(args.ffmpeg, args.ffprobe, cp, w, h, fps, cp2, flash_frames=flash_f)
            turn_clip_paths.append(cp2)
        else:
            turn_clip_paths.append(cp)
    # concat the montage clips into one base, then lay the held caption on top once
    montage_base = seg_path("03_turn_concat")
    concat_clips(args.ffmpeg, turn_clip_paths, montage_base)
    seg_frames = ffprobe_frame_count(args.ffprobe, montage_base)
    states = [(0, seg_frames, "Breakdown Studio does it in one pass.")]
    seqdir = work / f"{orientation}_03_turn_txt"
    render_fade_sequence(seqdir, (w, h), states, fps, seg_frames,
                          font_size_frac=0.04 if vertical else 0.04,
                          anchor_y="bottom", y_override_frac=0.74 if vertical else 0.82,
                          fade_frames=8, max_width_frac=0.86)
    out = seg_path("03_turn_txt")
    composite_overlay(args.ffmpeg, montage_base, seqdir, fps, out)
    segs.append(out)

    # --- 13.5-18.5 CAPABILITY 1: contact sheet zoom-out -----------------------------------
    cap1_dur = 5.0
    p = seg_path("04_cap1")
    # Start tight on a cluster of cells (upper-middle band, busy with imagery), pull out to the
    # full grid. pre_crop picks a sub-region of the 8K sheet as the "start close" state; the
    # zoompan then pulls from that crop's effective frame back out across the whole image by
    # scaling the *crop window* implicitly via zoom range on the full image instead (simpler &
    # more reliable than chaining two crops) -- see build_image_base: we zoom from tight (z0 high)
    # to wide (z1=1.0) directly on the full sheet, anchored center-ish where detail lives.
    build_image_base(args.ffmpeg, args.contact_sheet, cap1_dur, w, h, fps, p, mode="fill",
                      zoom=(2.6, 1.0), focus="center")
    seg_frames = seconds_to_frames(cap1_dur, fps)
    states = [(0, seg_frames, "Every shot detected. Every slate and note read.")]
    seqdir = work / f"{orientation}_04_cap1_txt"
    render_fade_sequence(seqdir, (w, h), states, fps, seg_frames,
                          font_size_frac=0.04 if vertical else 0.038,
                          anchor_y="bottom", y_override_frac=0.86 if vertical else 0.86,
                          fade_frames=8, max_width_frac=0.86, scrim=True)
    out = seg_path("04_cap1_txt")
    composite_overlay(args.ffmpeg, p, seqdir, fps, out)
    segs.append(out)

    # --- 18.5-23.0 CAPABILITY 2: app screenshot -------------------------------------------
    # 'fit' mode: the whole screenshot stays intact and readable on a dark backdrop (never
    # cropped/filled), so on-screen UI text isn't blown up into illegibility and the caption
    # gets real dark space to sit in instead of overlapping the app's own text.
    cap2_dur = 4.5
    p = seg_path("05_cap2")
    fit_frac = 0.62 if vertical else 0.66
    build_image_base(args.ffmpeg, args.screenshot_running, cap2_dur, w, h, fps, p,
                      mode="fit", zoom=(1.0, 1.05), fit_frac=fit_frac)
    seg_frames = seconds_to_frames(cap2_dur, fps)
    states = [(0, seg_frames, "The breakdown builds itself. The re-cut updates it.")]
    seqdir = work / f"{orientation}_05_cap2_txt"
    render_fade_sequence(seqdir, (w, h), states, fps, seg_frames,
                          font_size_frac=0.042 if vertical else 0.038,
                          anchor_y="top" if vertical else "bottom",
                          y_override_frac=0.07 if vertical else 0.86,
                          fade_frames=8, max_width_frac=0.84)
    out = seg_path("05_cap2_txt")
    composite_overlay(args.ffmpeg, p, seqdir, fps, out)
    segs.append(out)

    # --- 23.0-28.0 PROOF: dark bg, 4 lines appearing one by one --------------------------
    proof_dur = 5.0
    p = seg_path("06_proof")
    build_blurred_still_base(args.ffmpeg, args.source, args.proof_bg_at, proof_dur, w, h, fps, p)
    seg_frames = seconds_to_frames(proof_dur, fps)
    lines = [
        "149 shots in one pass",
        "96% slates read off the burn-in",
        "4 cuts re-matched, 1:1",
        "Validated on a real feature",
    ]
    # stagger: each line appears and stays (accumulating), evenly spaced across the segment
    seqdir = work / f"{orientation}_06_proof_txt"
    render_stagger_sequence(seqdir, (w, h), lines, fps, seg_frames,
                             font_size_frac=0.042 if vertical else 0.036, fade_frames=6)
    out = seg_path("06_proof_txt")
    composite_overlay(args.ffmpeg, p, seqdir, fps, out)
    segs.append(out)

    # --- 28.0-33.0 CTA CARD ---------------------------------------------------------------
    cta_dur = 5.0
    p = seg_path("07_cta")
    build_solid_base(args.ffmpeg, "black", cta_dur, w, h, fps, p)
    seg_frames = seconds_to_frames(cta_dur, fps)
    seqdir = work / f"{orientation}_07_cta_txt"
    render_cta_sequence(seqdir, (w, h), fps, seg_frames,
                         title_frac=0.072 if vertical else 0.062,
                         sub_frac=0.034 if vertical else 0.03,
                         mono_frac=0.024 if vertical else 0.021,
                         fade_frames=10)
    out = seg_path("07_cta_txt")
    composite_overlay(args.ffmpeg, p, seqdir, fps, out)
    segs.append(out)

    return segs


def render_stagger_sequence(seq_dir, canvas_size, lines, fps, seg_frames, font_size_frac, fade_frames,
                             max_width_frac=0.84):
    seq_dir.mkdir(parents=True, exist_ok=True)
    w, h = canvas_size
    max_w = int(w * max_width_frac)
    font = _font(int(h * font_size_frac), bold=True)
    # Shrink-to-fit: these are fixed known strings, so pick one font size (applied to all lines,
    # for a consistent look) that keeps every line inside the safe width -- never truncate/wrap
    # a stat line, just scale it down.
    probe = Image.new("RGBA", (1, 1))
    d0 = ImageDraw.Draw(probe)
    while font.size > 10 and max(d0.textlength(t, font=font) for t in lines) > max_w:
        font = _font(int(font.size * 0.92), bold=True)

    line_h = int(font.size * 1.7)
    n = len(lines)
    block_h = line_h * n
    top = (h - block_h) // 2

    appear_at = [seconds_to_frames(0.3 + i * ((seg_frames / fps - 0.6) / n), fps) for i in range(n)]

    rendered = []
    for i, text in enumerate(lines):
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img, "RGBA")
        tw = d.textlength(text, font=font)
        x = (w - tw) / 2
        y = top + i * line_h
        off = max(2, font.size // 22)
        d.text((x + off, y + off), text, font=font, fill=(0, 0, 0, 160))
        d.text((x, y), text, font=font, fill=(255, 255, 255, 255))
        rendered.append(img)

    for f in range(seg_frames):
        out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        for i, block in enumerate(rendered):
            a0 = appear_at[i]
            if f < a0:
                continue
            local = f - a0
            mult = min(1.0, local / max(1, fade_frames))
            if mult >= 0.999:
                out.alpha_composite(block)
            else:
                a = block.split()[3].point(lambda p, m=mult: int(p * m))
                b2 = block.copy()
                b2.putalpha(a)
                out.alpha_composite(b2)
        out.save(seq_dir / ("frame_%06d.png" % f))
    return seq_dir


def render_cta_sequence(seq_dir, canvas_size, fps, seg_frames, title_frac, sub_frac, mono_frac,
                         fade_frames):
    seq_dir.mkdir(parents=True, exist_ok=True)
    w, h = canvas_size
    title_font = _font(int(h * title_frac), bold=True)
    sub_font = _font(int(h * sub_frac), bold=True)
    mono_font = _font(int(h * mono_frac), bold=False)
    small_font = _font(int(h * mono_frac * 0.78), bold=False)

    probe = Image.new("RGBA", (1, 1))
    d0 = ImageDraw.Draw(probe)

    title_text = "BREAKDOWN STUDIO"
    title_max_w = int(w * 0.88)
    while title_font.size > 10 and d0.textlength(title_text, font=title_font) > title_max_w:
        title_font = _font(int(title_font.size * 0.94), bold=True)
    title_lines = [title_text]
    sub_lines = wrap_text(d0, "Breakdown and budget a whole film. Solo.", sub_font, int(w * 0.82))
    mono_text = "Open source. MIT. Built by a working VFX producer."
    mono_lines = wrap_text(d0, mono_text, mono_font, int(w * 0.82))

    def block_img(lines, font, fill=(255, 255, 255, 255), spacing=1.3):
        line_h = int(font.size * spacing)
        bh = line_h * len(lines)
        img = Image.new("RGBA", (w, bh + font.size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img, "RGBA")
        for i, ln in enumerate(lines):
            tw = d.textlength(ln, font=font)
            x = (w - tw) / 2
            y = i * line_h
            off = max(2, font.size // 20)
            d.text((x + off, y + off), ln, font=font, fill=(0, 0, 0, 160))
            d.text((x, y), ln, font=font, fill=fill)
        return img, bh

    title_img, title_h = block_img(title_lines, title_font, fill=(255, 255, 255, 255), spacing=1.1)
    sub_img, sub_h = block_img(sub_lines, sub_font, fill=(230, 230, 230, 255), spacing=1.25)
    mono_img, mono_h = block_img(mono_lines, mono_font, fill=(170, 170, 170, 255), spacing=1.3)

    gap1, gap2 = int(h * 0.035), int(h * 0.03)
    total_h = title_h + gap1 + sub_h + gap2 + mono_h
    top = (h - total_h) // 2 - int(h * 0.02)

    footer_lines = wrap_text(d0, ATTRIBUTION, small_font, int(w * 0.86))
    footer_img, footer_h = block_img(footer_lines, small_font, fill=(150, 150, 150, 230), spacing=1.2)
    footer_y = h - int(h * 0.07) - footer_h

    appear = {
        "title": seconds_to_frames(0.15, fps),
        "sub": seconds_to_frames(0.9, fps),
        "mono": seconds_to_frames(1.6, fps),
        "footer": seconds_to_frames(2.3, fps),
    }
    fade_out_start = seg_frames - seconds_to_frames(0.9, fps)

    def alpha_for(f, appear_frame):
        if f < appear_frame:
            return 0.0
        local = f - appear_frame
        a_in = min(1.0, local / max(1, fade_frames))
        if f >= fade_out_start:
            local_out = f - fade_out_start
            a_out = max(0.0, 1.0 - local_out / max(1, seconds_to_frames(0.9, fps)))
            return min(a_in, a_out)
        return a_in

    def paste_with_alpha(canvas, block, x, y, mult):
        if mult <= 0.001:
            return
        if mult >= 0.999:
            canvas.alpha_composite(block, (x, y))
        else:
            a = block.split()[3].point(lambda p, m=mult: int(p * m))
            b2 = block.copy()
            b2.putalpha(a)
            canvas.alpha_composite(b2, (x, y))

    for f in range(seg_frames):
        out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        y = top
        paste_with_alpha(out, title_img, 0, y, alpha_for(f, appear["title"]))
        y += title_h + gap1
        paste_with_alpha(out, sub_img, 0, y, alpha_for(f, appear["sub"]))
        y += sub_h + gap2
        paste_with_alpha(out, mono_img, 0, y, alpha_for(f, appear["mono"]))
        paste_with_alpha(out, footer_img, 0, footer_y, alpha_for(f, appear["footer"]))
        out.save(seq_dir / ("frame_%06d.png" % f))
    return seq_dir


def concat_clips(ffmpeg, clip_paths, out_path):
    listfile = out_path.with_suffix(".txt")
    with open(listfile, "w", encoding="utf-8") as fh:
        for c in clip_paths:
            fh.write(f"file '{Path(c).resolve().as_posix()}'\n")
    run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
         str(out_path), "-loglevel", "error"])
    listfile.unlink(missing_ok=True)
    return out_path


# =============================================================================================
# Audio bed
# =============================================================================================

def build_audio_bed(ffmpeg, source, start, dur, out_path, fade_in=1.0, fade_out=1.5):
    fo_start = max(0.0, dur - fade_out)
    af = f"afade=t=in:st=0:d={fade_in},afade=t=out:st={fo_start}:d={fade_out}"
    run([ffmpeg, "-y", "-ss", str(start), "-t", str(dur), "-i", source,
         "-vn", "-af", af, "-c:a", "aac", "-b:a", "192k", str(out_path), "-loglevel", "error"])
    return out_path


def mux_final(ffmpeg, video_path, audio_path, out_path):
    run([ffmpeg, "-y", "-i", video_path, "-i", audio_path,
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         "-shortest", str(out_path), "-loglevel", "error"])
    return out_path


# =============================================================================================
# Main
# =============================================================================================

def render_orientation(orientation, args, work):
    w, h = (1080, 1920) if orientation == "9x16" else (1920, 1080)
    fps = 24
    segs = build_timeline(orientation, w, h, fps, args, work)
    concat_path = work / f"{orientation}_concat.mp4"
    concat_clips(args.ffmpeg, segs, concat_path)

    total_dur = sum(get_dur(args.ffprobe, s) for s in segs)
    audio_path = work / f"{orientation}_audio.m4a"
    build_audio_bed(args.ffmpeg, args.source, args.music_start, total_dur, audio_path)

    out_path = Path(args.out_9x16 if orientation == "9x16" else args.out_16x9)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mux_final(args.ffmpeg, str(concat_path), str(audio_path), str(out_path))
    return out_path, total_dur


def get_dur(ffprobe, path):
    r = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="dressed Tears of Steel demo movie")
    ap.add_argument("--contact-sheet", required=True, help="full-res contact sheet jpg")
    ap.add_argument("--screenshot-running", required=True, help="app mid-run screenshot png")
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--ffprobe", required=True)
    ap.add_argument("--out-9x16", required=True)
    ap.add_argument("--out-16x9", required=True)
    ap.add_argument("--work-dir", default=None, help="scratch dir for intermediate renders (default: temp)")
    ap.add_argument("--keep-work", action="store_true", help="don't delete the work dir on success")

    # Shot picks (seconds into --source). Chosen by scouting frames across the film; see repo
    # history / commit message for the montage contact sheets used to pick these.
    ap.add_argument("--hook-a-start", type=float, default=104.0, help="HOOK shot: airship over rooftops, wide sky")
    ap.add_argument("--problem-start", type=float, default=246.0, help="PROBLEM shot: two figures walking, letterboxed")
    ap.add_argument("--turn-starts", type=float, nargs="+",
                     default=[460.0, 314.0, 354.0, 480.0, 555.0],
                     help="TURN montage shot starts (~1s each)")
    ap.add_argument("--proof-bg-at", type=float, default=340.0, help="dark frame used for PROOF background")
    ap.add_argument("--music-start", type=float, default=393.0, help="score passage start (builds through the climax)")

    args = ap.parse_args()

    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="bs_launch_reel_"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"[work dir] {work}")

    try:
        for orientation in ("9x16", "16x9"):
            print(f"[{orientation}] rendering timeline...")
            out_path, dur = render_orientation(orientation, args, work)
            size_mb = out_path.stat().st_size / (1024 * 1024)
            print(f"[{orientation}] done: {out_path}  duration={dur:.2f}s  size={size_mb:.2f}MB")
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
            print(f"[cleanup] removed {work}")
        else:
            print(f"[keep-work] left intermediates at {work}")


if __name__ == "__main__":
    sys.exit(main())
