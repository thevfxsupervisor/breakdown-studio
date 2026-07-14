#!/usr/bin/env python
"""transnet_detect.py - shot-boundary detection with TransNetV2, emitting a PySceneDetect-format
Scenes.csv that the rest of shot_breakdown_pipeline consumes unchanged.

Drop-in replacement for the PySceneDetect `detect` stage. Runs the TransNetV2 CNN (far more robust
than detect-content on low-contrast same-set cuts and dissolves). Fully local: the model weights ship
inside the `transnetv2-pytorch` pip package, so no network access and no footage ever leaves the box.

MUST be run with the TransNet venv interpreter (which has torch), NOT the ShotGrid Python:
    & "/path/to/transnet_env/Scripts/python.exe" transnet_detect.py --movie <mov> [--threshold 0.5]

By default writes to a SEPARATE `scenes_transnet/` dir so it never clobbers a hand-edited live
Scenes.csv. Point the pipeline at it (or copy over) once you've reconciled.
"""
import argparse, csv, os, subprocess, sys
from pathlib import Path

# TransNetV2's predict_video shells out to `ffmpeg` (via ffmpeg-python) -> make our build findable.
# Honour Breakdown Studio's configured binaries (BS_FFMPEG/BS_FFPROBE), else fall back to PATH.
_FFMPEG = os.environ.get("BS_FFMPEG", "")
if _FFMPEG and os.path.dirname(_FFMPEG):
    os.environ["PATH"] = os.path.dirname(_FFMPEG) + os.pathsep + os.environ.get("PATH", "")
FFPROBE = os.environ.get("BS_FFPROBE") or "ffprobe"


def get_fps(movie):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(movie)],
                         capture_output=True, text=True).stdout.strip()
    try:
        num, den = out.split("/"); return float(num) / float(den)
    except Exception:
        return 24.0


def fmt_tc(seconds):                         # seconds -> "HH:MM:SS.mmm" (PySceneDetect style)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:                            # rounding carry
        seconds += 1; ms = 0
    s = int(seconds)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}.{ms:03d}"


def detect(movie, threshold):
    import numpy as np, torch
    from transnetv2_pytorch import TransNetV2
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = TransNetV2(device=dev); model.eval()
    print(f"[transnet] device={dev}  analysing {Path(movie).name} ...", flush=True)
    with torch.no_grad():
        _, single, _ = model.predict_video(str(movie), quiet=True)
    preds = np.asarray(single.detach().cpu()).flatten()
    n = len(preds)
    scenes = TransNetV2.predictions_to_scenes(preds, threshold)   # [[start,end], ...] 0-based
    starts = sorted({0, *[int(s[0]) for s in scenes]})            # shot start frames (0-based)
    bounds = starts + [n]                                         # exclusive end of last shot
    shots = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]
    return shots, n


def write_csv(shots, fps, out_path):
    """shots: list of (start0, end0_excl) 0-based. Emits PySceneDetect Scenes.csv exactly."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tc_list = [fmt_tc(s / fps) for s, _ in shots[1:]]            # cut timecodes (scene starts 2..N)
    header = ["Scene Number", "Start Frame", "Start Timecode", "Start Time (seconds)",
              "End Frame", "End Timecode", "End Time (seconds)",
              "Length (frames)", "Length (timecode)", "Length (seconds)"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Timecode List:"] + tc_list)
        w.writerow(header)
        for i, (s0, e0) in enumerate(shots, start=1):
            length = e0 - s0
            w.writerow([i, s0 + 1, fmt_tc(s0 / fps), f"{s0/fps:.3f}",
                        e0, fmt_tc(e0 / fps), f"{e0/fps:.3f}",
                        length, fmt_tc(length / fps), f"{length/fps:.3f}"])
    return len(shots)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--movie", required=True)
    ap.add_argument("--out", help="output Scenes.csv path (default: <base>/<stem>/scenes_transnet/<stem>-Scenes.csv)")
    ap.add_argument("--threshold", type=float, default=0.25)  # benchmarked sweet spot (recall 1.0, prec 1.0); pkg default 0.5 misses soft cuts
    ap.add_argument("--output-base", default=os.environ.get("BS_OUTPUT_BASE", "pipeline_output"))
    args = ap.parse_args()

    movie = Path(args.movie); stem = movie.stem
    out = Path(args.out) if args.out else Path(args.output_base) / stem / "scenes_transnet" / f"{stem}-Scenes.csv"
    fps = get_fps(movie)
    shots, n = detect(movie, args.threshold)
    cnt = write_csv(shots, fps, out)
    print(f"[transnet] {n} frames @ {fps}fps -> {cnt} shots (threshold {args.threshold})")
    print(f"[transnet] wrote {out}")


if __name__ == "__main__":
    main()
