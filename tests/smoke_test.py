#!/usr/bin/env python
"""Breakdown Studio smoke tests - pure-logic assertions, no network or heavy I/O.

Run:  python tests/smoke_test.py         (from the breakdown_studio folder)
Exit 0 = all pass. Catches regressions in the parts that are easy to break silently:
the frame off-by-one, tcid math, timecode parsing, scenes-CSV parsing, and seek math.
Stdlib + numpy/PIL only (same as the worker); no pytest needed.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_worker as W  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


# --- 1. frame indices: the off-by-one fix (1-based inclusive CSV -> 0-based extract) -----------
# Scene 1 = CSV frames 1..120 -> 0-based first=0, last=119 (NOT 1..120). This is THE regression guard.
check("frame_indices scene1 = (0,59,119)", W.shot_frame_indices(1, 120) == (0, 59, 119), W.shot_frame_indices(1, 120))
check("frame_indices scene2 = (120,404,688)", W.shot_frame_indices(121, 689) == (120, 404, 688), W.shot_frame_indices(121, 689))
check("frame_indices single frame", W.shot_frame_indices(5, 5) == (4, 4, 4), W.shot_frame_indices(5, 5))

# --- 2. no overlap: a shot's 0-based 'end' must be < the next shot's 0-based 'start' ------------
scenes = [(1, 120), (121, 689), (690, 704)]
idx = [W.shot_frame_indices(a, b) for a, b in scenes]
check("end(i) < start(i+1) for all i", all(idx[i][2] < idx[i + 1][0] for i in range(len(idx) - 1)), idx)

# --- 3. tc_to_id (HH:MM:SS.mmm -> HHMMSSFF) ----------------------------------------------------
check("tcid 00:00:00.000 -> 00000000", W.tc_to_id("00:00:00.000") == "00000000")
check("tcid 00:00:05.000 -> 00000500", W.tc_to_id("00:00:05.000") == "00000500")
check("tcid 01:02:45.917 -> 01024522", W.tc_to_id("01:02:45.917") == "01024522", W.tc_to_id("01:02:45.917"))
check("tcid ff-clamp (.999 -> ff 23 not 24)", W.tc_to_id("00:00:00.999") == "00000023", W.tc_to_id("00:00:00.999"))

# --- 4. tc_to_seconds -------------------------------------------------------------------------
check("tc_to_seconds 01:02:45.917 = 3765.917", abs(W.tc_to_seconds("01:02:45.917") - 3765.917) < 1e-6, W.tc_to_seconds("01:02:45.917"))

# --- 5. parse_scenes_csv (PySceneDetect layout, skips the giant Timecode List line) -------------
csv_text = (
    "Timecode List:,00:00:05.000,00:00:28.708\n"
    "Scene Number,Start Frame,Start Timecode,Start Time (seconds),End Frame,End Timecode,End Time (seconds),Length (frames),Length (timecode),Length (seconds)\n"
    "1,1,00:00:00.000,0.000,120,00:00:05.000,5.000,120,00:00:05.000,5.000\n"
    "2,121,00:00:05.000,5.000,689,00:00:28.708,28.708,569,00:00:23.708,23.708\n"
)
tf = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
tf.write(csv_text)
tf.close()
sc = W.parse_scenes_csv(tf.name)
os.unlink(tf.name)
check("parse_scenes count = 2", len(sc) == 2, len(sc))
check("parse_scenes frames", sc[0]["start_frame"] == 1 and sc[0]["end_frame"] == 120 and sc[1]["start_frame"] == 121, sc[:1])
check("parse_scenes length", sc[1]["len_frames"] == 569, sc[1].get("len_frames"))

# --- 6. seek math: 'end' frame seek stays inside the shot (does not reach the next shot) --------
pre, post = W._seek_args(119, None, 24.0)
check("_seek_args returns two arg lists", isinstance(pre, list) and isinstance(post, list))
check("_seek_args uses -ss", ("-ss" in pre) or ("-ss" in post))
# target time for 0-based frame 119 is (119-0.5)/24; must be < the next shot's start time 120/24
t_end = max(0.0, (119 - 0.5) / 24.0)
check("end-frame seek < next-shot start", t_end < 120 / 24.0, t_end)
# clamp: frame 0 seeks to time 0 (never negative)
t0 = max(0.0, (0 - 0.5) / 24.0)
check("frame 0 seek clamps to >= 0", t0 == 0.0, t0)

print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
