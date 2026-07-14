#!/usr/bin/env python
"""contact_sheet.py - build one big film contact sheet from the per-shot mid thumbnails.

For every shot in a cut, take its middle-frame thumbnail, crop away the black letterbox
(including the timecode / watermark burn-ins baked into the bars), and tile them edge-to-edge
(no gaps, black background) into a single large 16:9 image (8K by default).

Source of record is the tcid-named mid thumbs the pipeline already wrote -
  <output_base>/<movie_stem>/thumbs/<prefix>_<tcid>-mid.jpg
- one per shot, current after all splits/merges. Sorted by tcid = film (timecode) order.
(For a sharper render, --source clips pulls the middle frame full-res from cuts/<prefix>_<tcid>.mp4.)

The letterbox crop is auto-detected once per run (row-luma union over a sample) and cached;
override with --crop top:bottom (in source-pixel rows) or --crop none to keep full frames.

Examples
  python contact_sheet.py                         # default stem, 7680x4320, auto crop, all shots
  python contact_sheet.py --movie-stem MYSHOW_CUT_20260101
  python contact_sheet.py --cols 50 --canvas 7680x4320 --out /path/to/contact_sheet.jpg
  python contact_sheet.py --source clips          # full-res middle frame per shot (slower)

Reusable across cuts/projects: everything keys off --movie-stem + --output-base.
"""
import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

OUTPUT_BASE = Path(os.environ.get("BS_OUTPUT_BASE", "pipeline_output"))
DEFAULT_STEM = os.environ.get("BS_DEFAULT_STEM", "MYSHOW_CUT_20260101")
DEFAULT_PREFIX = os.environ.get("BS_PREFIX", "SHW")
FF = os.environ.get("BS_FFMPEG", "ffmpeg")
FP = os.environ.get("BS_FFPROBE", "ffprobe")


def tcid(s):
    m = re.search(r"(\d{8})", os.path.basename(s or ""))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- letterbox

def detect_letterbox(images, thr=12.0, sample=80, inset=1):
    """Find the active-picture top/bottom rows across a sample of frames.

    Row mean luma is robust to burn-ins: timecode / watermark text is only a few bright
    pixels on an otherwise-black bar row, so the row mean stays near zero, while a real
    picture row is bright across its width. We take the union (per-row max over frames) so
    a dark shot never shrinks the crop. Returns (top, bottom) inclusive, in source rows.
    """
    if len(images) > sample:
        images = images[:: max(1, len(images) // sample)][:sample]
    acc = None
    h = None
    for f in images:
        a = np.asarray(Image.open(f).convert("L"), dtype=np.float32)
        h = a.shape[0]
        rm = a.mean(axis=1)
        acc = rm if acc is None else np.maximum(acc, rm)
    rows = np.where(acc > thr)[0]
    if rows.size == 0:
        return 0, h - 1
    top, bot = int(rows.min()), int(rows.max())
    # inset a hair so a faint burn-in edge can never bleed into the picture
    top = min(top + inset, h - 1)
    bot = max(bot - inset, top)
    return top, bot


def crop_box(im, crop):
    if not crop:
        return im
    top, bot = crop
    # crop is expressed in the detector's reference height; scale to this image's height
    return im.crop((0, top, im.width, bot + 1))


# ---------------------------------------------------------------- sources

def thumb_sources(movie_dir, prefix=DEFAULT_PREFIX):
    files = sorted(glob.glob(str(movie_dir / "thumbs" / f"{prefix}_*-mid.jpg")),
                   key=lambda p: tcid(p))
    return files


def clip_mid_frame(clip, dst):
    """Grab the exact middle frame of a clip, full-res, as a jpg."""
    n = subprocess.run([FP, "-v", "error", "-select_streams", "v:0", "-count_frames",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(clip)],
                       capture_output=True, text=True).stdout.strip()
    try:
        mid = max(0, int(n) // 2)
    except ValueError:
        mid = 0
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clip), "-vf", f"select=eq(n\\,{mid})", "-frames:v", "1",
                    str(dst)], capture_output=True)
    return dst if dst.exists() else None


def clip_sources(movie_dir, tmpdir, prefix=DEFAULT_PREFIX):
    clips = sorted(glob.glob(str(movie_dir / "cuts" / f"{prefix}_*.mp4")), key=lambda p: tcid(p))
    out = []
    for i, c in enumerate(clips, 1):
        dst = Path(tmpdir) / f"mid_{tcid(c)}.jpg"
        if clip_mid_frame(Path(c), dst):
            out.append(str(dst))
        if i % 200 == 0:
            print(f"  extracted {i}/{len(clips)} mid frames", flush=True)
    return out


# ---------------------------------------------------------------- grid

def pick_cols(n, canvas_w, canvas_h, cell_aspect):
    """cols so each gap-free cell best matches the picture aspect on a canvas_w:canvas_h sheet."""
    cols = round(math.sqrt((canvas_w / canvas_h) * n / cell_aspect))
    return max(1, cols)


def build(images, crop, canvas, cols, out, bg=(0, 0, 0), jpeg_q=92):
    cw, ch = canvas
    n = len(images)
    # measure cropped aspect from the first readable image
    probe = crop_box(Image.open(images[0]).convert("RGB"), crop)
    cell_aspect = probe.width / probe.height
    if not cols:
        cols = pick_cols(n, cw, ch, cell_aspect)
    rows = math.ceil(n / cols)
    print(f"{n} shots -> {cols} cols x {rows} rows  "
          f"(cell ~{cw // cols}x{ch // rows}px, picture aspect {cell_aspect:.3f})", flush=True)

    sheet = Image.new("RGB", (cw, ch), bg)
    for i, f in enumerate(images):
        c, r = i % cols, i // cols
        x0, x1 = c * cw // cols, (c + 1) * cw // cols
        y0, y1 = r * ch // rows, (r + 1) * ch // rows
        try:
            im = crop_box(Image.open(f).convert("RGB"), crop)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", flush=True)
            continue
        sheet.paste(im.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0, y0))
        if (i + 1) % 400 == 0:
            print(f"  placed {i + 1}/{n}", flush=True)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in (".jpg", ".jpeg"):
        sheet.save(out, quality=jpeg_q, subsampling=0)
    else:
        sheet.save(out)
    mb = out.stat().st_size / 1e6
    print(f"[done] {out}  ({cw}x{ch}, {mb:.1f} MB)", flush=True)


# ---------------------------------------------------------------- main

def parse_canvas(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--movie-stem", default=DEFAULT_STEM)
    ap.add_argument("--output-base", default=str(OUTPUT_BASE))
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="shot-code prefix (default SHW; or BS_PREFIX env)")
    ap.add_argument("--source", choices=["thumbs", "clips"], default="thumbs")
    ap.add_argument("--canvas", default="7680x4320", type=parse_canvas)
    ap.add_argument("--cols", type=int, default=0, help="0 = auto (match 16:9 with square-ish cells)")
    ap.add_argument("--crop", default="auto",
                    help="'auto' (detect letterbox), 'none', or 'top:bottom' source rows")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="first N shots only (testing)")
    args = ap.parse_args()

    movie_dir = Path(args.output_base) / args.movie_stem
    out = args.out or str(movie_dir / f"_{args.movie_stem}_contact_sheet.jpg")

    tmp = tempfile.mkdtemp(prefix="contact_") if args.source == "clips" else None
    if args.source == "clips":
        images = clip_sources(movie_dir, tmp, args.prefix)
    else:
        images = thumb_sources(movie_dir, args.prefix)
    if not images:
        sys.exit(f"no source images found under {movie_dir} (source={args.source})")
    if args.limit:
        images = images[: args.limit]

    if args.crop == "none":
        crop = None
    elif args.crop == "auto":
        crop = detect_letterbox(images)
        h = Image.open(images[0]).height
        print(f"letterbox crop: rows {crop[0]}..{crop[1]} of {h} "
              f"(picture {crop[1] - crop[0] + 1}px tall)", flush=True)
    else:
        crop = tuple(int(x) for x in args.crop.split(":"))

    build(images, crop, args.canvas, args.cols, out)


if __name__ == "__main__":
    main()
