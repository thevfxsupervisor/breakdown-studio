#!/usr/bin/env python
"""make_demo_cut.py - generates a fully synthetic "locked cut" for Breakdown Studio demos/tests.

Everything in this file is procedural graphics: gradients, drifting shapes, noise fields, and a
generated tone bed. There is no client footage, no client names, and no real project data anywhere
in this script or its output. It exists so the repo ships a working end-to-end demo/test fixture
that anyone can run without access to a real editorial cut.

Output:
  demo/demo_cut.mp4              ~90s, 1920x1080 @ 24fps, ~28 shots, burned-in slate/TC/notes/watermark
  demo/demo_scenes_truth.csv     ground-truth cut list (start/end frame, slate, take, note) - the
                                  same file doubles as the "answer key" the pipeline's OCR/detect
                                  stages get scored against.

Burn-in grammar mirrors bs_ocr.py's parse_slate()/note_consistency_tier() expectations:
  - top-left slate:  "<scene>/<slate>/<take-roman>"  (plus a few pt2 / "5+6" join / stock variants)
  - top-right:       show-TC counter, HH:MM:SS:FF, starting 01:00:00:00
  - bottom-left:      VFX editorial note on ~40% of shots, held on EVERY frame of the shot (the OCR
                      3-frame consistency gate requires the note on mid+end at minimum)
  - center watermark: faint "PROPERTY OF DEMO PICTURES"

Usage:
  python make_demo_cut.py [--ffmpeg ffmpeg] [--out demo_cut.mp4] [--fps 24] [--seed 7]

Requires: Pillow, numpy, and an ffmpeg binary (path via --ffmpeg, $BS_FFMPEG, or PATH). Does NOT
read config.json and does NOT need any project-specific interpreter - keep it runnable standalone.
"""
import argparse
import csv
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FPS = 24
HERE = Path(__file__).resolve().parent

# ------------------------------------------------------------------------------------- burn-in
ROMANS = ["I", "II", "III", "IV", "V", "VI"]
NOTE_VOCAB = [
    "CLEAN UP", "SET EXTENSION", "MONITOR INSERT", "CAM SHAKE", "ADD SNOW",
    "SKY REPLACE", "WIRE REMOVAL", "DUST BUST", "SCREEN COMP", "STABILIZE",
]


def _font(size, bold=True):
    cands = [r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              "/Library/Fonts/Arial Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for c in cands:
        try:
            if Path(c).exists():
                return ImageFont.truetype(c, size)
        except Exception:
            pass
    return ImageFont.load_default()


FONT_BURN = _font(34)
FONT_WATERMARK = _font(30)


def tc_string(frame_idx, fps=FPS, start_h=1):
    total = start_h * 3600 * fps + frame_idx
    ff = total % fps
    s_total = total // fps
    ss = s_total % 60
    mm = (s_total // 60) % 60
    hh = s_total // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def draw_burnins(img, slate_text, note_text, tc_text, letterbox=90):
    """Draws burn-ins into a black letterbox band top+bottom (matches the real pipeline's
    letterbox-crop convention in contact_sheet.py: burn-ins live in bars, not over picture)."""
    d = ImageDraw.Draw(img, "RGBA")
    # top + bottom letterbox bars
    d.rectangle([0, 0, W, letterbox], fill=(0, 0, 0, 255))
    d.rectangle([0, H - letterbox, W, H], fill=(0, 0, 0, 255))

    pad = 18
    # top-left: slate
    d.text((pad, 28), slate_text, font=FONT_BURN, fill=(255, 255, 255, 255))
    # top-right: show TC
    tw = d.textlength(tc_text, font=FONT_BURN)
    d.text((W - pad - tw, 28), tc_text, font=FONT_BURN, fill=(255, 255, 255, 255))
    # bottom-left: VFX note (only if present)
    if note_text:
        d.text((pad, H - letterbox + 28), note_text, font=FONT_BURN, fill=(80, 220, 255, 255))
    # centered faint watermark
    wm = "PROPERTY OF DEMO PICTURES"
    ww = d.textlength(wm, font=FONT_WATERMARK)
    d.text(((W - ww) / 2, H / 2 - 15), wm, font=FONT_WATERMARK, fill=(255, 255, 255, 60))
    return img


# ------------------------------------------------------------------------------------- shot content
#
# IMPORTANT: TransNetV2 is a real shot-boundary detector, so any discontinuity in a content function
# (a modulo position wrap, a color-band snap, a full-frame re-randomize) reads as a false cut in the
# middle of what's supposed to be one continuous shot. TransNet is ALSO sensitive to perfectly
# clean, noise-free full-frame changes at a moderate rate: real footage has grain/motion blur that
# masks gradual change, so a mathematically smooth (but noiseless) global color sweep can still
# register as a soft-cut storm.
#
# Every content_* function below follows a strict two-phase contract so this can't happen by
# accident:
#   prepare_*(seed_rng, w, h)  -> a params dict, called EXACTLY ONCE per shot (all the shot's
#                                 randomness -- colors, textures, motion amplitude/phase -- is
#                                 drawn here and fixed for the whole shot).
#   render_*(t, params, w, h)  -> the frame at local time t, a PURE function of (t, params) that
#                                 must NEVER touch the RNG. This was learned the hard way: an
#                                 earlier version called seed_rng.randint(...) for "fixed" colors
#                                 INSIDE the per-frame render function, so the whole palette was
#                                 silently re-randomized every single frame -- invisible on
#                                 high-frequency noise content, but a full flat-color flip every
#                                 frame on the gradient content, which TransNetV2 (correctly) read
#                                 as a hard cut on every frame.
# Verified empirically by rendering each content_* type in isolation and running it through
# TransNetV2 directly: all four now score zero false-positive cuts (see the isolated-content check
# this file's tests exercise indirectly via the full demo_cut.mp4 + demo_scenes_truth.csv pair).

def _make_field(rs, w, h, cell, lo=0, hi=255):
    """Low-res random field resized up with a smooth filter -> a fixed, blurry texture. Shared
    building block for gradient/noise so both are constructed the same proven-safe way."""
    small_w, small_h = w // cell + 2, h // cell + 2
    base = rs.randint(lo, hi + 1, size=(small_h, small_w, 3)).astype(np.uint8)
    im = Image.fromarray(base, "RGB").resize((small_w * cell, small_h * cell), Image.BILINEAR)
    return np.asarray(im.crop((0, 0, w, h)), dtype=np.float32)


def prepare_gradient(seed_rng, w=W, h=H, cell=28):
    c1 = np.array([seed_rng.randint(20, 235) for _ in range(3)], dtype=np.float32)
    c2 = np.array([seed_rng.randint(20, 235) for _ in range(3)], dtype=np.float32)
    rs = np.random.RandomState(seed_rng.randint(0, 2**31 - 1))
    tex = _make_field(rs, w, h, cell)[:, :, 0:1] / 255.0
    return {"c1": c1, "c2": c2, "tex": tex}


def render_gradient(t, p, w=W, h=H):
    """Fine-grained two-tone mottled field, fixed per shot. A literal smooth mathematical gradient
    (perfectly flat, near-zero local contrast) tested badly: TransNetV2 flagged it as a false-cut
    storm even fully static, apparently because there's no spatial texture for the per-frame grain
    to sit on. This mottled construction has real local contrast throughout."""
    arr = p["c1"] * (1 - p["tex"]) + p["c2"] * p["tex"]
    return np.clip(arr, 0, 255).astype(np.uint8)


def prepare_shapes(seed_rng, w=W, h=H, n_shapes=5):
    base_val = seed_rng.randint(10, 40)
    shapes = []
    for i in range(n_shapes):
        shapes.append({
            "cx0": seed_rng.randint(int(w * 0.15), int(w * 0.85)),
            "cy0": seed_rng.randint(int(h * 0.15), int(h * 0.85)),
            "amp_x": seed_rng.randint(30, 90),
            "amp_y": seed_rng.randint(24, 70),
            "speed": 0.06 + seed_rng.random() * 0.06,
            "phase0": seed_rng.random() * math.tau,
            "r": seed_rng.randint(50, 150),
            "col": tuple(seed_rng.randint(60, 255) for _ in range(3)),
            "is_ellipse": i % 2 == 0,
        })
    return {"base_val": base_val, "shapes": shapes}


def render_shapes(t, p, w=W, h=H):
    """Drifting circles/rects over a dark base. Positions orbit smoothly (sin/cos), small amplitude
    and slow speed so per-frame displacement stays a few pixels."""
    base = np.full((h, w, 3), p["base_val"], dtype=np.uint8)
    img = Image.fromarray(base)
    d = ImageDraw.Draw(img)
    for s in p["shapes"]:
        cx = s["cx0"] + s["amp_x"] * math.sin(t * s["speed"] + s["phase0"])
        cy = s["cy0"] + s["amp_y"] * math.cos(t * s["speed"] * 0.8 + s["phase0"])
        r, col = s["r"], s["col"]
        if s["is_ellipse"]:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
        else:
            d.rectangle([cx - r, cy - r, cx + r, cy + r], fill=col)
    return np.asarray(img.convert("RGB"))


def prepare_noise(seed_rng, w=W, h=H, cell=64):
    rs = np.random.RandomState(seed_rng.randint(0, 2**31 - 1))
    return {"field": _make_field(rs, w, h, cell, lo=40, hi=255)}


def render_noise(t, p, w=W, h=H):
    """Blocky noise field, position FIXED for the whole shot (no pan at all) - the film-grain pass
    already gives this one per-frame texture variation, so it doesn't need its own motion."""
    return p["field"].astype(np.uint8)


def prepare_pan(seed_rng, w=W, h=H):
    big_w, big_h = w + 240, h + 200
    rs = np.random.RandomState(seed_rng.randint(0, 2**31 - 1))
    tile = rs.randint(0, 255, size=(8, 8, 3)).astype(np.uint8)
    big = np.asarray(Image.fromarray(tile, "RGB").resize((big_w, big_h), Image.BICUBIC))
    speed = 0.05 + seed_rng.random() * 0.04
    return {"big": big, "big_w": big_w, "big_h": big_h, "speed": speed}


def render_pan(t, p, w=W, h=H):
    """Slow camera-drift pan across a big soft-focus procedural tile (simulates plate-cam drift).
    Small amplitude, slow speed -> sub-pixel-to-few-pixel motion per frame."""
    speed = p["speed"]
    x0 = int((math.sin(t * speed) * 0.5 + 0.5) * (p["big_w"] - w))
    y0 = int((math.cos(t * speed * 0.75) * 0.5 + 0.5) * (p["big_h"] - h))
    return p["big"][y0:y0 + h, x0:x0 + w]


CONTENT_KINDS = [
    ("gradient", prepare_gradient, render_gradient),
    ("shapes", prepare_shapes, render_shapes),
    ("noise", prepare_noise, render_noise),
    ("pan", prepare_pan, render_pan),
]


def add_film_grain(arr, rng, amount=3.0):
    """Light per-frame luminance noise so the detector sees the grain floor real footage has.
    Cheap: single-channel noise broadcast across RGB (fast, still reads as grain)."""
    h, w = arr.shape[:2]
    noise = rng.normal(0, amount, size=(h, w, 1)).astype(np.float32)
    out = arr.astype(np.float32) + noise
    return np.clip(out, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------------------------- cut plan
def build_shot_plan(rng, total_seconds=90, fps=FPS):
    """Returns list of dicts: {frames, scene, slate, take, note, content_fn_idx, seed}."""
    total_frames = total_seconds * fps
    # duration pool: mix of very short (8f) to long (8s), weighted toward short/medium like a real edit
    pool = []
    while sum(pool) < total_frames:
        r = rng.random()
        if r < 0.18:
            d = rng.randint(8, 16)          # very short punchy cuts
        elif r < 0.55:
            d = rng.randint(24, 72)         # 1-3s
        elif r < 0.85:
            d = rng.randint(72, 144)        # 3-6s
        else:
            d = rng.randint(144, 192)       # 6-8s
        pool.append(d)
    # trim/pad to hit ~28 shots and ~total_frames
    target_n = 28
    while len(pool) > target_n:
        # merge two shortest neighbours
        i = min(range(len(pool) - 1), key=lambda k: pool[k] + pool[k + 1])
        pool[i] += pool.pop(i + 1)
    while len(pool) < target_n:
        i = max(range(len(pool)), key=lambda k: pool[k])
        half = pool[i] // 2
        if half < 8:
            break
        pool[i] -= half
        pool.insert(i + 1, half)
    # rescale to exactly total_frames
    scale = total_frames / sum(pool)
    pool = [max(8, int(round(p * scale))) for p in pool]
    diff = total_frames - sum(pool)
    pool[-1] += diff

    scenes_used = list(range(101, 101 + len(pool) // 2 + 3))
    rng.shuffle(scenes_used)

    shots = []
    slate_ctr = {}
    scene_cursor = 0
    special_slots = {
        3: "pt2",       # scene/part join variant e.g. "104pt2"
        11: "join",     # "5+6" style composite join
        19: "stock",    # non-slate stock footage burn-in
    }
    for i, frames in enumerate(pool):
        content_idx = i % len(CONTENT_KINDS)
        seed = rng.randint(0, 2**31 - 1)
        kind = special_slots.get(i, "normal")
        if kind == "stock":
            scene, slate, take = "", None, ""
            slate_disp = f"Snowfield-{rng.randint(100,999):03d}"
        else:
            scene_num = scenes_used[scene_cursor % len(scenes_used)]
            scene_cursor += 1
            if kind == "pt2":
                scene_field = f"{scene_num}pt2"
            elif kind == "join":
                scene_field = f"{scene_num}+{scene_num+1}pt1"
            else:
                scene_field = str(scene_num)
            slate_num = 100 + i * 5 + rng.randint(0, 3)
            slate_ctr[slate_num] = slate_ctr.get(slate_num, 0) + 1
            take = ROMANS[(slate_ctr[slate_num] - 1) % len(ROMANS)]
            scene, slate = scene_field, str(slate_num)
            slate_disp = f"{scene_field}/{slate_num}/{take}"

        has_note = (i % 5 in (0, 2)) and kind != "stock"   # ~40% of shots
        note = rng.choice(NOTE_VOCAB) if has_note else ""

        shots.append({
            "frames": frames, "scene": scene, "slate": slate, "take": take,
            "slate_disp": slate_disp, "note": note, "content_idx": content_idx, "seed": seed,
            "kind": kind,
        })
    return shots


# ------------------------------------------------------------------------------------- render
def render_shot_frames(shot, start_frame_global, tmp_dir, fps=FPS):
    """Writes one PNG per frame for this shot into tmp_dir, burnt-in and letterboxed.
    Returns list of written file paths (frame-ordered).

    All of this shot's content randomness is drawn ONCE via prepare_*(), then render_*() is a pure
    function of (t, params) for every frame -- see the CONTENT_KINDS contract note above."""
    _name, prepare_fn, render_fn = CONTENT_KINDS[shot["content_idx"]]
    rng = random.Random(shot["seed"])
    params = prepare_fn(rng)
    grain_rng = np.random.RandomState(shot["seed"] ^ 0x5EED)
    paths = []
    for local_f in range(shot["frames"]):
        t = local_f / fps
        arr = render_fn(t, params)
        arr = add_film_grain(arr, grain_rng)
        img = Image.fromarray(arr, "RGB")
        if img.size != (W, H):
            img = img.resize((W, H))
        global_f = start_frame_global + local_f
        tc = tc_string(global_f, fps)
        img = draw_burnins(img, shot["slate_disp"], shot["note"], tc)
        p = tmp_dir / f"f_{global_f:07d}.png"
        img.save(p)
        paths.append(p)
    return paths


def find_ffmpeg(explicit):
    if explicit:
        return explicit
    env = os.environ.get("BS_FFMPEG")
    if env:
        return env
    which = shutil.which("ffmpeg")
    if which:
        return which
    sys.exit("ERROR: no ffmpeg found. Pass --ffmpeg <path>, set BS_FFMPEG, or add ffmpeg to PATH.")


def encode_movie(frame_glob_dir, total_frames, fps, ffmpeg, out_path):
    """PNG sequence -> MP4 (H.264) with a generated sine tone bed for audio."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = total_frames / fps
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(fps), "-i", str(frame_glob_dir / "f_%07d.png"),
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration:.3f}",
        "-filter_complex", "[1:a]volume=0.06[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ERROR: ffmpeg encode failed:\n{r.stderr[-3000:]}")
    return out_path


def write_truth_csv(shots, fps, out_path):
    out_path = Path(out_path)
    rows = []
    cursor0 = 0  # 0-based
    for i, s in enumerate(shots, start=1):
        start0 = cursor0
        end0 = cursor0 + s["frames"] - 1
        rows.append({
            "shot": i,
            "start_frame": start0 + 1,          # 1-based, matches Scenes.csv convention
            "end_frame": end0 + 1,
            "length_frames": s["frames"],
            "duration_s": round(s["frames"] / fps, 3),
            "scene": s["scene"],
            "slate": s["slate"] or "",
            "take": s["take"],
            "slate_display": s["slate_disp"],
            "note": s["note"],
            "kind": s["kind"],
        })
        cursor0 = end0 + 1
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--out", default=str(HERE / "demo_cut.mp4"))
    ap.add_argument("--truth-out", default=str(HERE / "demo_scenes_truth.csv"))
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep-frames", action="store_true", help="don't delete the temp PNG sequence (debugging)")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg)
    rng = random.Random(args.seed)
    shots = build_shot_plan(rng, total_seconds=args.seconds, fps=args.fps)
    total_frames = sum(s["frames"] for s in shots)

    print(f"[make_demo_cut] {len(shots)} shots, {total_frames} frames "
          f"({total_frames/args.fps:.1f}s) @ {args.fps}fps")

    tmp_dir = Path(tempfile.mkdtemp(prefix="demo_cut_frames_"))
    try:
        cursor = 0
        for i, s in enumerate(shots, 1):
            render_shot_frames(s, cursor, tmp_dir, fps=args.fps)
            cursor += s["frames"]
            print(f"  rendered shot {i}/{len(shots)}  frames={s['frames']:4d}  "
                  f"slate={s['slate_disp']:<22} note={s['note']}")
        out = encode_movie(tmp_dir, total_frames, args.fps, ffmpeg, args.out)
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"[make_demo_cut] wrote {out} ({size_mb:.1f} MB)")
        if size_mb > 80:
            print(f"WARNING: output is {size_mb:.1f}MB, over the ~80MB target", file=sys.stderr)
    finally:
        if not args.keep_frames:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            print(f"[make_demo_cut] kept frames at {tmp_dir}")

    truth = write_truth_csv(shots, args.fps, args.truth_out)
    print(f"[make_demo_cut] wrote {truth}")


if __name__ == "__main__":
    main()
