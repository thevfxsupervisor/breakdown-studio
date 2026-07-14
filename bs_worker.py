#!/usr/bin/env python
"""bs_worker.py - portable local pipeline worker for Breakdown Studio.

Self-contained: depends only on Pillow + numpy + an ffmpeg/ffprobe binary. Does NOT import the
Google/ShotGrid-coupled pipeline, so it installs and runs on any machine. Every stage keys each
shot off its tcid (HHMMSSFF of the shot's start timecode), matching the studio convention, so the
artifacts interoperate with contact_sheet.py and the rest of the toolchain.

Stages (run one per invocation; the GUI chains them):
  frames    movie + Scenes.csv -> frames/SHW_<tcid>-{start,mid,end}.jpg   (full-res)
  thumbs    frames/*           -> thumbs/SHW_<tcid>-{start,mid,end}.jpg   (downscaled)
  cuts      movie + Scenes.csv -> cuts/SHW_<tcid>.mp4                      (one clip per shot)
  refclips  cuts/*             -> refclips/SHW_<tcid>.mov + _timeline.mov  (burned-in code+dur)
  qc        Scenes.csv (+thumbs/frames) -> qc_flags.csv                   (flag suspect shots)

Scenes.csv is the PySceneDetect / TransNetV2 format (see transnet_detect.py). Detection itself is a
separate stage (TransNetV2) the GUI runs with the torch interpreter.

  python bs_worker.py frames  --movie M --output-base B [--scenes CSV] [--prefix SHW] [--workers 6]
  python bs_worker.py thumbs  --output-base B --movie M [--prefix SHW]
  python bs_worker.py cuts    --movie M --output-base B [--encoder nvenc] [--prefix SHW]
  python bs_worker.py refclips --output-base B --movie M [--fps 24] [--prefix SHW]
  python bs_worker.py qc      --output-base B --movie M [--prefix SHW]

Progress is printed as 'PROGRESS stage done/total' lines so the GUI can drive a bar.
"""
import argparse
import csv
import concurrent.futures as cf
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image, ImageDraw, ImageFont

THUMB_W, THUMB_H = 480, 270
PRORES = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]

FFMPEG = os.environ.get("BS_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("BS_FFPROBE", "ffprobe")

NULL_OUT = "NUL" if os.name == "nt" else "/dev/null"


# ----------------------------------------------------------------- helpers

def progress(stage, done, total):
    print(f"PROGRESS {stage} {done}/{total}", flush=True)


def _tail(b, n=500):
    """Tail of a captured stderr (bytes or str) for surfacing ffmpeg failures."""
    if b is None:
        return ""
    if isinstance(b, bytes):
        b = b.decode("utf-8", "replace")
    b = b.strip().replace("\r", "")
    return b[-n:]


def get_fps(movie):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(movie)],
                         capture_output=True, text=True).stdout.strip()
    try:
        num, den = out.split("/")
        return float(num) / float(den)
    except Exception:
        return 24.0


def tc_to_id(tc_str, fps=24.0):
    """HH:MM:SS.mmm -> HHMMSSFF (frame-accurate, unique per cut)."""
    parts = tc_str.split(":")
    h, m = int(parts[0]), int(parts[1])
    sec_frac = float(parts[2])
    s = int(sec_frac)
    ff = int(round((sec_frac - s) * fps))
    if ff >= round(fps):
        ff = int(round(fps)) - 1
    return f"{h:02d}{m:02d}{s:02d}{ff:02d}"


def tc_to_seconds(tc_str):
    parts = tc_str.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def find_scenes_csv(output_dir, stem):
    """Prefer a reconciled live scenes/ CSV, else scenes_transnet/, else any *Scenes*.csv."""
    for sub in ("scenes", "scenes_transnet"):
        p = output_dir / sub / f"{stem}-Scenes.csv"
        if p.exists():
            return p
    for sub in ("scenes", "scenes_transnet", "."):
        cands = sorted((output_dir / sub).glob("*Scenes*.csv")) if (output_dir / sub).exists() else []
        if cands:
            return cands[0]
    return None


def parse_scenes_csv(csv_file):
    scenes = []
    with open(csv_file, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if row and row[0].strip() == "Scene Number":
                header = [c.strip() for c in row]
                break
        if header is None:
            raise ValueError(f"No header row in {csv_file}")
        for row in reader:
            if not row or not row[0].strip().isdigit():
                continue
            d = dict(zip(header, [c.strip() for c in row]))
            scenes.append({
                "scene": int(d["Scene Number"]),
                "start_tc": d["Start Timecode"],
                "end_tc": d["End Timecode"],
                "start_frame": int(d["Start Frame"]),
                "end_frame": int(d["End Frame"]),
                "len_frames": int(d["Length (frames)"]),
                "duration_s": float(d["Length (seconds)"]),
            })
    return scenes


def load_scenes(output_dir, stem, scenes_arg, fps):
    csv_file = Path(scenes_arg) if scenes_arg else find_scenes_csv(output_dir, stem)
    if not csv_file or not Path(csv_file).exists():
        sys.exit(f"ERROR: no Scenes.csv found (looked in {output_dir}/scenes[_transnet]). "
                 f"Run detection first.")
    scenes = parse_scenes_csv(csv_file)
    for s in scenes:
        s["tcid"] = tc_to_id(s["start_tc"], fps)
    print(f"  loaded {len(scenes)} shots from {Path(csv_file).name}", flush=True)
    return scenes


def run_pool(fn, items, workers, stage):
    total = len(items)
    if total == 0:
        print(f"[{stage}] no shots to process", flush=True)
        progress(stage, 0, 0)
        return []
    done = 0
    fails = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for fut in cf.as_completed(futs):
            ok = fut.result()
            done += 1
            if not ok:
                fails.append(futs[fut])
            if done % 25 == 0 or done == total:
                progress(stage, done, total)
    return fails


# ----------------------------------------------------------------- frame math

def _seek_args(k, total, fps):
    """Frame index k -> ffmpeg seek args for an accurate single-frame grab.

    Uses an input pre-seek (fast, keyframe-snapping) to ~2s before the target, then a precise
    output seek for the remaining offset. Target time is (k-0.5)/fps: output-seek returns the
    first frame with PTS >= target, so (k+0.5)/fps would overshoot into frame k+1.
    """
    if total and total > 0:
        k = max(0, min(int(k), total - 1))
    else:
        k = max(0, int(k))
    t = max(0.0, (k - 0.5) / fps)
    pre = max(0.0, t - 2.0)
    post = t - pre  # in [0, 2]
    pre_args = ["-ss", f"{pre:.4f}"] if pre > 0 else []
    post_args = ["-ss", f"{post:.4f}"] if post > 0 else []
    return pre_args, post_args


# ----------------------------------------------------------------- stages

def shot_frame_indices(start_frame, end_frame):
    """Scenes-CSV frames (1-BASED, End INCLUSIVE) -> 0-based (start, mid, end) indices to extract.

    Subtract 1 to convert 1-based CSV numbers to the 0-based index the (k-0.5)/fps seek expects.
    Without the -1, 'start' lands one frame late and 'end' grabs the NEXT shot's first frame (the
    off-by-one that broke 3-frame VFX-note detection, split detection and end thumbnails).
    """
    first, last = start_frame - 1, end_frame - 1
    return first, (first + last) // 2, max(first, last)


def stage_frames(movie, output_dir, scenes, workers, fps, total_frames=None, prefix="SHW"):
    fdir = output_dir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    movie = str(movie)

    def one(s):
        first, mid, last = shot_frame_indices(s["start_frame"], s["end_frame"])
        ok = True
        for pos, fr in (("start", first), ("mid", mid), ("end", last)):
            out = fdir / f"{prefix}_{s['tcid']}-{pos}.jpg"
            if out.exists():
                continue
            pre, post = _seek_args(fr, total_frames, fps)
            r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
                               + pre + ["-i", movie] + post
                               + ["-frames:v", "1", "-q:v", "2", str(out)],
                               capture_output=True)
            if r.returncode != 0:
                ok = False
                print(f"  [frames] FAIL {s['tcid']}-{pos}: {_tail(r.stderr)}", flush=True)
        return ok

    fails = run_pool(one, scenes, workers, "frames")
    print(f"[frames] {len(scenes) - len(fails)} ok, {len(fails)} failed", flush=True)
    return fails


def stage_thumbs(output_dir, scenes, workers, prefix="SHW"):
    fdir, tdir = output_dir / "frames", output_dir / "thumbs"
    tdir.mkdir(parents=True, exist_ok=True)

    def one(s):
        ok = True
        for pos in ("start", "mid", "end"):
            src = fdir / f"{prefix}_{s['tcid']}-{pos}.jpg"
            dst = tdir / f"{prefix}_{s['tcid']}-{pos}.jpg"
            if dst.exists():
                continue
            if not src.exists():
                ok = False
                continue
            try:
                im = Image.open(src).convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS)
                im.save(dst, quality=88)
            except Exception as e:
                ok = False
                print(f"  [thumbs] FAIL {s['tcid']}-{pos}: {e}", flush=True)
        return ok

    fails = run_pool(one, scenes, workers, "thumbs")
    print(f"[thumbs] {len(scenes) - len(fails)} ok, {len(fails)} failed", flush=True)
    return fails


def _nvenc_works(movie, fps):
    """Encode ~10 frames FROM THE REAL MOVIE with h264_nvenc to confirm the GPU path works.

    Synthetic lavfi sources give false -22 'Invalid argument' errors even when NVENC is fine, so
    we exercise the real input. Returns True if the test encode succeeds.
    """
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(movie), "-frames:v", "10",
                        "-c:v", "h264_nvenc", "-preset", "p5", "-an",
                        "-f", "null", NULL_OUT], capture_output=True)
    if r.returncode != 0:
        print(f"  [cuts] nvenc probe failed: {_tail(r.stderr)}", flush=True)
    return r.returncode == 0


def stage_cuts(movie, output_dir, scenes, workers, encoder, fps, prefix="SHW"):
    cdir = output_dir / "cuts"
    cdir.mkdir(parents=True, exist_ok=True)
    movie = str(movie)
    if encoder == "nvenc":
        if _nvenc_works(movie, fps):
            print("  [cuts] h264_nvenc available", flush=True)
        else:
            print("  WARNING: h264_nvenc unavailable on this machine; falling back to libx264",
                  flush=True)
            encoder = "libx264"
    venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20"] if encoder == "nvenc" \
        else ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]

    def one(s):
        out = cdir / f"{prefix}_{s['tcid']}.mp4"
        if out.exists():
            return True
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", s["start_tc"].replace(",", "."), "-i", movie,
                            "-t", f"{s['duration_s']:.4f}"] + venc +
                           ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(out)],
                           capture_output=True)
        if r.returncode != 0:
            print(f"  [cuts] FAIL {s['tcid']}: {_tail(r.stderr)}", flush=True)
        return r.returncode == 0

    fails = run_pool(one, scenes, workers, "cuts")
    print(f"[cuts] {len(scenes) - len(fails)} ok, {len(fails)} failed", flush=True)
    return fails


def _font(size):
    for cand in (os.environ.get("BS_FONT"), r"C:\Windows\Fonts\arialbd.ttf",
                 "/Library/Fonts/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            if cand and Path(cand).exists():
                return ImageFont.truetype(cand, size)
        except Exception:
            pass
    return ImageFont.load_default()


def make_overlay(code, dur, path, w, h, s1=48):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _font(s1)
    # shot code, top-left
    x, y, pad = 28, 18, 10
    tw = d.textlength(code, font=f)
    d.rectangle([x - 12, y - pad, x + tw + 12, y + f.size + pad], fill=(0, 0, 0, 200))
    d.text((x, y), code, font=f, fill=(255, 255, 255, 255))
    # duration (frames), bottom-right
    dt = f"{dur}f"
    dw = d.textlength(dt, font=f)
    dx, dy = w - 28 - dw, h - 28 - f.size
    d.rectangle([dx - 12, dy - pad, dx + dw + 12, dy + f.size + pad], fill=(0, 0, 0, 200))
    d.text((dx, dy), dt, font=f, fill=(255, 255, 255, 255))
    img.save(path)


def stage_refclips(movie, output_dir, scenes, workers, fps, prefix="SHW"):
    cdir, rdir = output_dir / "cuts", output_dir / "refclips"
    rdir.mkdir(parents=True, exist_ok=True)
    tmp = rdir / "_tmp"
    tmp.mkdir(exist_ok=True)
    # probe size from first existing cut
    W, H = 1920, 1080
    for s in scenes:
        c = cdir / f"{prefix}_{s['tcid']}.mp4"
        if c.exists():
            out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=width,height", "-of", "csv=p=0", str(c)],
                                 capture_output=True, text=True).stdout.strip()
            try:
                W, H = (int(x) for x in out.split(","))
            except Exception:
                pass
            break

    def one(s):
        clip = cdir / f"{prefix}_{s['tcid']}.mp4"
        if not clip.exists():
            return False
        out = rdir / f"{prefix}_{s['tcid']}.mov"
        if out.exists():
            return True
        png = tmp / f"ov_{s['tcid']}.png"
        make_overlay(f"{prefix}_{s['tcid']}", s["len_frames"], png, W, H)
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(clip), "-i", str(png),
                            "-filter_complex", "[0:v][1:v]overlay=0:0"] + PRORES +
                           ["-c:a", "aac", "-b:a", "192k", str(out)], capture_output=True)
        try:
            png.unlink()
        except OSError:
            pass
        if r.returncode != 0:
            print(f"  [refclips] FAIL {s['tcid']}: {_tail(r.stderr)}", flush=True)
        return r.returncode == 0

    fails = run_pool(one, scenes, workers, "refclips")
    made = [s for s in scenes if (rdir / f"{prefix}_{s['tcid']}.mov").exists()]
    made.sort(key=lambda s: s["tcid"])
    listf = rdir / "_concat.txt"
    listf.write_text("".join(f"file '{(rdir / (prefix + '_' + s['tcid'] + '.mov')).as_posix()}'\n"
                             for s in made), encoding="utf-8")
    timeline = rdir / f"_{Path(movie).stem}_refclips_timeline.mov"
    rc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                         "-safe", "0", "-i", str(listf), "-c", "copy", str(timeline)],
                        capture_output=True)
    if rc.returncode != 0:
        print(f"  [refclips] timeline FAIL: {_tail(rc.stderr)}", flush=True)
    print(f"[refclips] {len(made)} clips ok, {len(fails)} failed; timeline rc={rc.returncode}",
          flush=True)
    return fails


# ----------------------------------------------------------------- qc stage

def _ahash(path, hash_size=8):
    """Simple average hash -> uint64. Returns None if the image can't be read."""
    try:
        im = Image.open(path).convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    except Exception:
        return None
    a = np.asarray(im, dtype=np.float64)
    bits = (a > a.mean()).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _hamming(a, b):
    if a is None or b is None:
        return None
    return bin(a ^ b).count("1")


def _find_thumb(output_dir, tcid, pos, prefix="SHW"):
    """Locate a thumbnail (preferred) or full-res frame for a shot/position."""
    for sub in ("thumbs", "frames"):
        p = output_dir / sub / f"{prefix}_{tcid}-{pos}.jpg"
        if p.exists():
            return p
    return None


def stage_qc(output_dir, scenes, fps, prefix="SHW"):
    """Flag suspect shots before building. ALWAYS QC scene detection before building."""
    out_csv = output_dir / "qc_flags.csv"
    if not scenes:
        print("[qc] no shots to QC", flush=True)
        out_csv.write_text(
            "tcid,temp_code,start_tc,len_frames,duration_s,dur_flag,"
            "phash_startmid,phash_midend,phash_flag,short_flag,note\n", encoding="utf-8")
        progress("qc", 0, 0)
        return out_csv

    durs = [s["duration_s"] for s in scenes]
    med = median(durs)
    dur_threshold = max(10.0, 6.0 * med)
    short_frames = 4         # < ~4 frames -> possible false split
    phash_threshold = 22     # Hamming distance (of 64) above which start/mid/end look like a cut

    rows = []
    dflag = pflag = sflag = 0
    total = len(scenes)
    for i, s in enumerate(scenes, 1):
        tcid = s["tcid"]
        temp_code = f"{prefix}_{tcid}"
        notes = []

        dur_flag = s["duration_s"] > dur_threshold
        if dur_flag:
            dflag += 1
            notes.append(f"dur>{dur_threshold:.1f}s(med={med:.1f})")

        short_flag = s["len_frames"] < short_frames
        if short_flag:
            sflag += 1
            notes.append(f"len<{short_frames}f")

        # perceptual hash across the shot: a high start<->mid or mid<->end distance suggests the
        # "shot" spans a real cut (two shots merged into one).
        ps = _find_thumb(output_dir, tcid, "start", prefix)
        pm = _find_thumb(output_dir, tcid, "mid", prefix)
        pe = _find_thumb(output_dir, tcid, "end", prefix)
        hs = _ahash(ps) if ps else None
        hm = _ahash(pm) if pm else None
        he = _ahash(pe) if pe else None
        d_sm = _hamming(hs, hm)
        d_me = _hamming(hm, he)
        phash_flag = ((d_sm is not None and d_sm > phash_threshold) or
                      (d_me is not None and d_me > phash_threshold))
        if phash_flag:
            pflag += 1
            notes.append("phash-jump")

        rows.append({
            "tcid": tcid,
            "temp_code": temp_code,
            "start_tc": s["start_tc"],
            "len_frames": s["len_frames"],
            "duration_s": f"{s['duration_s']:.3f}",
            "dur_flag": int(dur_flag),
            "phash_startmid": "" if d_sm is None else d_sm,
            "phash_midend": "" if d_me is None else d_me,
            "phash_flag": int(phash_flag),
            "short_flag": int(short_flag),
            "note": "; ".join(notes),
        })
        if i % 25 == 0 or i == total:
            progress("qc", i, total)

    cols = ["tcid", "temp_code", "start_tc", "len_frames", "duration_s", "dur_flag",
            "phash_startmid", "phash_midend", "phash_flag", "short_flag", "note"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"[qc] {total} shots, {dflag} duration-flagged, {pflag} phash-flagged, {sflag} short "
          f"-> {out_csv.name}", flush=True)
    return out_csv


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["frames", "thumbs", "cuts", "refclips", "qc"])
    ap.add_argument("--movie", required=True)
    ap.add_argument("--output-base", required=True)
    ap.add_argument("--scenes", default="")
    ap.add_argument("--prefix", default="SHW")
    ap.add_argument("--encoder", choices=["libx264", "nvenc"], default="libx264")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--fps", type=float, default=0.0)
    args = ap.parse_args()

    movie = Path(args.movie)
    stem = movie.stem
    output_dir = Path(args.output_base) / stem
    output_dir.mkdir(parents=True, exist_ok=True)
    fps = args.fps or (get_fps(movie) if movie.exists() else 24.0)

    print(f"=== bs_worker {args.stage} | {stem} @ {fps:.3f}fps | workers={args.workers} ===",
          flush=True)
    scenes = load_scenes(output_dir, stem, args.scenes, fps)

    # Max 1-based inclusive End Frame == the cut's total frame count; 0-based frames are 0..count-1,
    # so this is the clamp ceiling (total-1 = last valid 0-based index) used by _seek_args.
    total_frames = max(s["end_frame"] for s in scenes) if scenes else None

    if args.stage == "frames":
        stage_frames(movie, output_dir, scenes, args.workers, fps, total_frames, args.prefix)
    elif args.stage == "thumbs":
        stage_thumbs(output_dir, scenes, args.workers, args.prefix)
    elif args.stage == "cuts":
        stage_cuts(movie, output_dir, scenes, args.workers, args.encoder, fps, args.prefix)
    elif args.stage == "refclips":
        stage_refclips(movie, output_dir, scenes, args.workers, fps, args.prefix)
    elif args.stage == "qc":
        stage_qc(output_dir, scenes, fps, args.prefix)
    print(f"DONE {args.stage}", flush=True)


if __name__ == "__main__":
    main()
