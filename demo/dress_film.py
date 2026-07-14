#!/usr/bin/env python
"""dress_film.py - "dresses" ANY source movie like an editorial VFX handoff, for Breakdown Studio's
cinematic demo.

Where make_demo_cut.py *generates* fully procedural footage, this script takes real (properly
licensed) footage and burns a synthetic editorial-slate layer on top of it, exactly like a real cut
would arrive: a slate/take burn-in top-left, a show-TC counter top-right, a VFX editorial note on
some shots bottom-left, all inside a letterbox band (never over picture), plus a small attribution
watermark. Nothing about the SOURCE footage is altered creatively; this is a pure overlay pass.

Pipeline:
  1. detect     - shot-boundary detection via TransNetV2 (transnet_detect.py), cached to a Scenes.csv
                  next to the output so re-runs skip re-detecting.
  2. assign     - deterministic (seeded) synthetic slate/take/note/show-TC assignment per shot,
                  following the SAME grammar bs_ocr.py's parse_slate() expects (see make_demo_cut.py
                  for the grammar rationale). Written to a truth CSV (the OCR answer key).
  3. burn       - one PIL overlay PNG per shot (RGBA, transparent except the letterbox bars +
                  text), composited over that shot's frame range with ffmpeg's overlay filter,
                  encoded as an individual segment, then all segments concatenated (stream copy) into
                  the final dressed movie. Doing it per-shot-segment (instead of one giant drawtext/
                  overlay filter graph for the whole film) keeps the ffmpeg command size and memory
                  bounded regardless of shot count, and lets failed segments retry independently.

Usage:
  python dress_film.py --source ~/blender_demo/tears_of_steel_1080p.mov \\
      --out ~/blender_demo/tos_dressed.mp4 --truth-out demo/tos_truth.csv \\
      --transnet-python <path> --ffmpeg <path> --ffprobe <path>

  # Faster iteration: dress only an excerpt (seconds from the top of the source)
  python dress_film.py --source ... --clip-seconds 180 ...

Requires: Pillow (burn-ins), an ffmpeg/ffprobe binary, and (for the detect stage) the TransNet
interpreter from config.json's transnet_python key -- passed explicitly via --transnet-python so
this script never hardcodes a local path. Does NOT read config.json directly, matching
make_demo_cut.py's "runnable standalone" convention; the caller (a wrapper, or you) passes paths in.
"""
import argparse
import csv
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# ------------------------------------------------------------------------------------- burn-in grammar
# Mirrors make_demo_cut.py's grammar exactly (see that file's header comment for the full rationale):
# top-left slate "<scene>/<slate>/<take-roman>", top-right show-TC, bottom-left VFX note on ~40% of
# shots (held for the whole shot so the OCR 3-frame consistency gate can read it), centered
# attribution watermark, small corner film-title tag.
ROMANS = ["I", "II", "III", "IV", "V", "VI"]
NOTE_VOCAB = [
    "CLEAN UP", "SET EXTENSION", "MONITOR INSERT", "ADD SNOW", "CAM SHAKE",
    "SKY REPLACE", "WIRE REMOVAL", "DUST BUST", "SCREEN COMP", "STABILIZE",
]
ATTRIBUTION = "(CC) Blender Foundation | mango.blender.org"
FILM_TITLE = "TEARS OF STEEL"

LETTERBOX = 90  # px, top+bottom bars -- burn-ins live here, never over picture (matches contact_sheet.py's
                # letterbox-detect convention so the real pipeline crops it identically)


def _font(size, bold=True):
    cands = [
        r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\arialbd.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in cands:
        try:
            if Path(c).exists():
                return ImageFont.truetype(c, size)
        except Exception:
            pass
    return ImageFont.load_default()


def tc_string(frame_idx, fps, start_h=1):
    total = start_h * 3600 * fps + frame_idx
    ff = total % fps
    s_total = total // fps
    ss = s_total % 60
    mm = (s_total // 60) % 60
    hh = s_total // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


# =============================================================================================
# Stage 1: shot detection (TransNetV2, cached)
# =============================================================================================

def run_detect(source, transnet_python, ffmpeg, ffprobe, threshold, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    scenes_csv = cache_dir / f"{Path(source).stem}-Scenes.csv"
    if scenes_csv.exists():
        print(f"[detect] cached -> {scenes_csv}")
        return scenes_csv
    print(f"[detect] running TransNetV2 (threshold={threshold}) ...")
    env_ffmpeg = ffmpeg
    cmd = [
        transnet_python, str(REPO / "transnet_detect.py"),
        "--movie", str(source),
        "--out", str(scenes_csv),
        "--threshold", str(threshold),
    ]
    import os
    env = dict(**{**__import__("os").environ})
    env["BS_FFMPEG"] = ffmpeg
    env["BS_FFPROBE"] = ffprobe
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0 or not scenes_csv.exists():
        sys.exit(f"ERROR: TransNetV2 detect failed (exit {r.returncode})")
    return scenes_csv


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
                "start_frame": int(d["Start Frame"]),
                "end_frame": int(d["End Frame"]),
                "len_frames": int(d["Length (frames)"]),
            })
    return scenes


# =============================================================================================
# Stage 2: synthetic slate/note/TC assignment
# =============================================================================================

def assign_shots(scenes, seed, fps):
    rng = random.Random(seed)
    scenes_used = list(range(101, 101 + len(scenes) // 2 + 3))
    rng.shuffle(scenes_used)

    special_every = max(7, len(scenes) // 6)  # sprinkle a pt2/join/stock variant every N-ish shots
    shots = []
    slate_ctr = {}
    scene_cursor = 0
    for i, sc in enumerate(scenes):
        kind = "normal"
        if i > 0 and i % special_every == 3:
            kind = "pt2"
        elif i > 0 and i % special_every == 5:
            kind = "join"
        elif i > 0 and i % special_every == 1 and i % (special_every * 3) < special_every:
            kind = "stock"

        if kind == "stock":
            scene, slate, take = "", None, ""
            slate_disp = f"Snowfield-{rng.randint(100, 999):03d}"
        else:
            scene_num = scenes_used[scene_cursor % len(scenes_used)]
            scene_cursor += 1
            if kind == "pt2":
                scene_field = f"{scene_num}pt2"
            elif kind == "join":
                scene_field = f"{scene_num}+{scene_num + 1}pt1"
            else:
                scene_field = str(scene_num)
            slate_num = 100 + i * 5 + rng.randint(0, 3)
            slate_ctr[slate_num] = slate_ctr.get(slate_num, 0) + 1
            take = ROMANS[(slate_ctr[slate_num] - 1) % len(ROMANS)]
            scene, slate = scene_field, str(slate_num)
            slate_disp = f"{scene_field}/{slate_num}/{take}"

        has_note = (i % 5 in (0, 2)) and kind != "stock"  # ~40% of shots
        note = rng.choice(NOTE_VOCAB) if has_note else ""

        shots.append({
            "shot": i + 1,
            "start_frame": sc["start_frame"],
            "end_frame": sc["end_frame"],
            "len_frames": sc["len_frames"],
            "scene": scene,
            "slate": slate or "",
            "take": take,
            "slate_display": slate_disp,
            "note": note,
            "kind": kind,
        })
    return shots


def write_truth_csv(shots, fps, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["shot", "start_frame", "end_frame", "length_frames", "duration_s",
              "scene", "slate", "take", "slate_display", "note", "kind"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in shots:
            w.writerow({
                "shot": s["shot"], "start_frame": s["start_frame"], "end_frame": s["end_frame"],
                "length_frames": s["len_frames"], "duration_s": round(s["len_frames"] / fps, 3),
                "scene": s["scene"], "slate": s["slate"], "take": s["take"],
                "slate_display": s["slate_display"], "note": s["note"], "kind": s["kind"],
            })
    return out_path


# =============================================================================================
# Stage 3: burn-in overlay + per-shot segment encode + concat
# =============================================================================================

def build_overlay_png(shot, w, h, fps, out_path):
    """One transparent RGBA PNG per shot: letterbox bars + slate/note/TC/watermark/title text.
    The show-TC baked into this static overlay is the shot's START tc (held for the whole shot,
    same convention as a real slate burn-in that doesn't re-render every frame)."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, w, LETTERBOX], fill=(0, 0, 0, 255))
    d.rectangle([0, h - LETTERBOX, w, h], fill=(0, 0, 0, 255))

    font_burn = _font(int(h * 0.0315))
    font_wm = _font(int(h * 0.02))
    font_title = _font(int(h * 0.016))
    pad = int(w * 0.0094)

    d.text((pad, 28), shot["slate_display"], font=font_burn, fill=(255, 255, 255, 255))
    # show-TC burned as the shot's START timecode (held for the whole shot, same convention as the
    # slate/note fields -- a real burn-in usually free-runs per-frame, but a per-shot static overlay
    # keeps this a single PNG per shot; still gives bs_ocr's tcoffset stage a valid, distinct read
    # per shot).
    tc = tc_string(shot["start_frame"] - 1, int(round(fps)))
    tw = d.textlength(tc, font=font_burn)
    d.text((w - pad - tw, 28), tc, font=font_burn, fill=(255, 255, 255, 255))
    if shot["note"]:
        d.text((pad, h - LETTERBOX + 28), shot["note"], font=font_burn, fill=(80, 220, 255, 255))

    ww = d.textlength(ATTRIBUTION, font=font_wm)
    d.text(((w - ww) / 2, h - LETTERBOX - font_wm.size - 6), ATTRIBUTION, font=font_wm,
           fill=(255, 255, 255, 130))

    tit_w = d.textlength(FILM_TITLE, font=font_title)
    d.text((w - pad - tit_w, h - LETTERBOX + 28 + font_burn.size + 6), FILM_TITLE, font=font_title,
           fill=(255, 200, 60, 200))

    img.save(out_path)
    return out_path


def probe_video_size(ffprobe, source):
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate",
                        "-of", "csv=p=0", str(source)], capture_output=True, text=True)
    w, h, rate = r.stdout.strip().split(",")
    num, den = rate.split("/")
    fps = float(num) / float(den)
    return int(w), int(h), fps


def burn_segments(source, shots, fps, w, h, ffmpeg, out_path, workers=1, offset_frames=0):
    """For each shot: build its overlay PNG, cut [start,end] from source, overlay-composite,
    encode as an individual H.264 segment (crf 18, audio copied through). Then concat (stream
    copy) all segments into out_path. offset_frames lets a --clip-seconds trim still address the
    correct source frame range (scenes were detected on the trimmed clip, frames are relative to it,
    so offset_frames is normally 0; kept for completeness / future re-use)."""
    tmp = Path(tempfile.mkdtemp(prefix="dress_film_"))
    seg_dir = tmp / "segments"
    png_dir = tmp / "overlays"
    seg_dir.mkdir(parents=True)
    png_dir.mkdir(parents=True)
    concat_list = tmp / "concat.txt"

    seg_paths = []
    try:
        for s in shots:
            png = png_dir / f"ov_{s['shot']:04d}.png"
            build_overlay_png(s, w, h, fps, png)

            start_s = (s["start_frame"] - 1 + offset_frames) / fps
            end_s = (s["end_frame"] + offset_frames) / fps
            dur = max(1.0 / fps, end_s - start_s)
            seg = seg_dir / f"seg_{s['shot']:04d}.mp4"

            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start_s:.4f}", "-i", str(source),
                "-i", str(png),
                "-t", f"{dur:.4f}",
                "-filter_complex", "[0:v][1:v]overlay=0:0[v]",
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                "-video_track_timescale", "24000",
                str(seg),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not seg.exists():
                print(f"  [burn] FAIL shot {s['shot']}: {r.stderr[-800:]}", file=sys.stderr)
                continue
            seg_paths.append(seg)
            if s["shot"] % 10 == 0 or s["shot"] == len(shots):
                print(f"PROGRESS burn {s['shot']}/{len(shots)}", flush=True)

        if not seg_paths:
            sys.exit("ERROR: no segments encoded successfully")

        with open(concat_list, "w", encoding="utf-8") as f:
            for p in seg_paths:
                # ffmpeg concat demuxer wants forward slashes / escaped paths to be safe cross-platform
                f.write(f"file '{p.as_posix()}'\n")

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(concat_list),
               "-c", "copy", str(out_path)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            # stream-copy concat can fail on minor param drift between segments; re-encode concat as fallback
            print("  [concat] stream-copy concat failed, re-encoding concat instead", file=sys.stderr)
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-safe", "0", "-i", str(concat_list),
                   "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", str(out_path)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"ERROR: concat failed:\n{r.stderr[-3000:]}")
        print(f"[burn] {len(seg_paths)}/{len(shots)} segments -> {out_path}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return Path(out_path)


def maybe_trim_source(source, ffmpeg, clip_seconds, tmp_holder):
    if not clip_seconds:
        return source
    trimmed = tmp_holder / f"{Path(source).stem}_clip{clip_seconds}s.mp4"
    if trimmed.exists():
        print(f"[trim] cached -> {trimmed}")
        return trimmed
    print(f"[trim] cutting first {clip_seconds}s from source (re-encode for frame-accurate cut) ...")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(source), "-t", str(clip_seconds),
           "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", str(trimmed)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ERROR: trim failed:\n{r.stderr[-3000:]}")
    return trimmed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="path to the source movie (e.g. tears_of_steel_1080p.mov)")
    ap.add_argument("--out", required=True, help="output dressed movie path")
    ap.add_argument("--truth-out", default=str(HERE / "tos_truth.csv"))
    ap.add_argument("--cache-dir", default="", help="dir for cached Scenes.csv / trimmed source (default: alongside --out)")
    ap.add_argument("--transnet-python", required=True)
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--ffprobe", required=True)
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--clip-seconds", type=float, default=0, help="if set, only dress the first N seconds of source (faster iteration)")
    ap.add_argument("--min-shots", type=int, default=0, help="sanity floor; exits nonzero if detection finds fewer")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.exists():
        sys.exit(f"ERROR: source not found: {source}")
    out = Path(args.out)
    cache_dir = Path(args.cache_dir) if args.cache_dir else out.parent / "_dress_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    work_source = maybe_trim_source(source, args.ffmpeg, args.clip_seconds, cache_dir)

    w, h, fps = probe_video_size(args.ffprobe, work_source)
    print(f"[dress_film] source={work_source.name} {w}x{h} @ {fps:.3f}fps")

    scenes_csv = run_detect(work_source, args.transnet_python, args.ffmpeg, args.ffprobe,
                             args.threshold, cache_dir)
    scenes = parse_scenes_csv(scenes_csv)
    print(f"[dress_film] {len(scenes)} shots detected")
    if args.min_shots and len(scenes) < args.min_shots:
        sys.exit(f"ERROR: only {len(scenes)} shots detected, expected >= {args.min_shots}")

    shots = assign_shots(scenes, args.seed, fps)
    truth = write_truth_csv(shots, fps, args.truth_out)
    print(f"[dress_film] wrote truth CSV -> {truth}  ({len(shots)} rows, "
          f"{sum(1 for s in shots if s['note'])} with notes, "
          f"{sum(1 for s in shots if s['kind'] != 'normal')} special-variant slates)")

    burn_segments(work_source, shots, fps, w, h, args.ffmpeg, out)

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"[dress_film] DONE -> {out} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
