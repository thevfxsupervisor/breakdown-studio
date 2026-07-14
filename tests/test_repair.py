#!/usr/bin/env python
"""bs_repair.py pure-logic unit tests - stdlib only, no ffmpeg/network/Sheets calls.

Covers: tcid normalization, burn-in -> file-TC offset conversion, split-frame math (including
the split-into-N decreasing-order invariant), ledger bottom-to-top ordering, and the scenes-CSV
rewrite logic exercised on an in-memory (tempfile) CSV.

Run:  "C:/Program Files/Shotgun/Python3/python.exe" tests/test_repair.py
Exit 0 = all pass.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_repair as R  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


def check_raises(name, fn, exc_types=(Exception,)):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        print(f"  FAIL {name}   (expected exception, none raised)")
    except exc_types:
        PASS += 1
        print(f"  ok   {name}")
    except Exception as e:
        FAIL += 1
        print(f"  FAIL {name}   (wrong exception type: {type(e).__name__}: {e})")


# =================================================================================== tcid norm
check("norm_tcid strips leading zeros", R.norm_tcid("00400813") == "400813", R.norm_tcid("00400813"))
check("norm_tcid passes plain number", R.norm_tcid("400813") == "400813")
check("norm_tcid all-zero -> '0'", R.norm_tcid("00000000") == "0", R.norm_tcid("00000000"))
check("norm_tcid both forms match", R.norm_tcid("00400813") == R.norm_tcid("400813"))
check("norm_tcid strips whitespace", R.norm_tcid("  00552910  ") == "552910")

check("tcid_of pulls 8 digits from shot code", R.tcid_of("SHW_00091413") == "00091413")
check("tcid_of passes through bare tcid", R.tcid_of("00091413") == "00091413")
check("tcid_of no digits -> passthrough", R.tcid_of("nonsense") == "nonsense")

# =================================================================================== tc <-> seconds/smpte
check("tc_to_seconds 01:02:45.917", abs(R.tc_to_seconds("01:02:45.917") - 3765.917) < 1e-6)
check("sec_to_tc round-trip", R.sec_to_tc(3765.917).startswith("01:02:45"))
check("sec_to_smpte 0s @24fps", R.sec_to_smpte(0.0, 24) == "00:00:00:00")
check("sec_to_smpte 1s @24fps", R.sec_to_smpte(1.0, 24) == "00:00:01:00", R.sec_to_smpte(1.0, 24))
check("smpte_to_frames/frames_to_smpte round-trip",
      R.frames_to_smpte(R.smpte_to_frames("01:02:45:10", 24), 24) == "01:02:45:10")
check("smpte_to_frames basic @24fps", R.smpte_to_frames("00:00:01:00", 24) == 24)
check("frames_to_smpte basic @24fps", R.frames_to_smpte(24, 24) == "00:00:01:00")

# =================================================================================== burn-in -> file TC offset
# show_TC = file_TC + offset  =>  file_TC = show_TC - offset (matches project_showtc_offset.md convention)
off = "00:59:50:00"
check("burnin_to_file_tc basic subtraction",
      R.burnin_to_file_tc("01:00:00:00", off, 24) == "00:00:10:00",
      R.burnin_to_file_tc("01:00:00:00", off, 24))
check("burnin_to_file_tc exact-offset -> file TC zero",
      R.burnin_to_file_tc("00:59:50:00", off, 24) == "00:00:00:00")
check("burnin_to_file_tc frame-level precision",
      R.burnin_to_file_tc("01:00:00:05", off, 24) == "00:00:10:05")
check_raises("burnin_to_file_tc raises on negative result",
             lambda: R.burnin_to_file_tc("00:00:00:00", off, 24), (ValueError,))

# =================================================================================== header_map / col_letter
hdr = ["Status", "Suggested Scene", "Shot Code", "File TC In", "File TC_ID"]
H = R.header_map(hdr)
check("header_map resolves plain header", H["Status"] == 0)
check("header_map aliases Note->Status", H.get("Note") == 0, H.get("Note"))
check("header_map aliases Scene->Suggested Scene", H.get("Scene") == 1)
check("header_map aliases TC In -> File TC In (renamed)", H.get("TC In") == 3, H.get("TC In"))
check("header_map aliases TC_ID -> File TC_ID (renamed)", H.get("TC_ID") == 4, H.get("TC_ID"))
check("col_letter 0 -> A", R.col_letter(0) == "A")
check("col_letter 25 -> Z", R.col_letter(25) == "Z")
check("col_letter 26 -> AA", R.col_letter(26) == "AA")
check("col_letter AE index", R.col_letter(30) == "AE", R.col_letter(30))

# =================================================================================== split frame math
# --- 2-way split (1 cut point) ---
check("split_frames_decreasing single point",
      R.split_frames_decreasing(100, [40]) == [40])
check_raises("split_frames_decreasing rejects at_frame==0",
             lambda: R.split_frames_decreasing(100, [0]), (ValueError,))
check_raises("split_frames_decreasing rejects at_frame==shot_len",
             lambda: R.split_frames_decreasing(100, [100]), (ValueError,))
check_raises("split_frames_decreasing rejects out-of-range",
             lambda: R.split_frames_decreasing(100, [150]), (ValueError,))
check_raises("split_frames_decreasing rejects empty",
             lambda: R.split_frames_decreasing(100, []), (ValueError,))

# --- N-way split: THE core invariant - points must be applied in DECREASING order so the
# keep-piece (always the first, lowest-frame piece) stays a valid index space across each
# successive split application. ---
pts = R.split_frames_decreasing(100, [20, 70, 40])
check("split_frames_decreasing sorts DECREASING", pts == [70, 40, 20], pts)
check("split_frames_decreasing dedupes", R.split_frames_decreasing(100, [40, 40, 70]) == [70, 40])

pieces = R.split_piece_lengths(100, [20, 70, 40])
check("split_piece_lengths sums to shot_len", sum(pieces) == 100, pieces)
check("split_piece_lengths correct segments", pieces == [20, 20, 30, 30], pieces)

plan_pieces, cut_desc = R.plan_split(100, [20, 70, 40], 24)
check("plan_split returns forward-ordered pieces",
      [p["frame_start"] for p in plan_pieces] == [0, 20, 40, 70], plan_pieces)
check("plan_split piece lengths match split_piece_lengths",
      [p["len"] for p in plan_pieces] == pieces)
check("plan_split cut_points kept decreasing for the apply loop", cut_desc == [70, 40, 20], cut_desc)
check("plan_split pieces cover the full range contiguously",
      all(plan_pieces[i]["frame_end"] + 1 == plan_pieces[i + 1]["frame_start"]
          for i in range(len(plan_pieces) - 1)))
check("plan_split last piece ends at shot_len-1", plan_pieces[-1]["frame_end"] == 99)

# extract_positions / frame_center_time
check("extract_positions single-frame shot", R.extract_positions(1) == (0, 0, 0))
check("extract_positions even length", R.extract_positions(10) == (0, 4, 9), R.extract_positions(10))
check("frame_center_time frame 0 at start_sec-0.5/fps",
      abs(R.frame_center_time(10.0, 0, 24) - (10.0 - 0.5 / 24)) < 1e-9)

# =================================================================================== ledger ordering
corrections = [
    {"id": "a", "sheet_row": 963, "merged_tcid": "00400813"},
    {"id": "b", "sheet_row": 1024, "merged_tcid": "00412101"},
    {"id": "c", "sheet_row": 1003, "merged_tcid": "00405300"},
]
ordered = R.order_ledger_bottom_to_top(corrections)
check("ledger ordered bottom-to-top (descending row)",
      [c["id"] for c in ordered] == ["b", "c", "a"], [c["id"] for c in ordered])
check("ledger rows strictly descending",
      all(ordered[i]["sheet_row"] > ordered[i + 1]["sheet_row"] for i in range(len(ordered) - 1)))

# fallback: no sheet_row recorded yet -> order by merged_tcid descending
no_rows = [
    {"id": "x", "merged_tcid": "00100000"},
    {"id": "y", "merged_tcid": "00500000"},
    {"id": "z", "merged_tcid": "00300000"},
]
ordered2 = R.order_ledger_bottom_to_top(no_rows)
check("ledger fallback orders by merged_tcid descending",
      [c["id"] for c in ordered2] == ["y", "z", "x"], [c["id"] for c in ordered2])

# nested cut7-style shape (as in known_boundary_corrections.json)
nested = [
    {"id": "p", "cut7": {"sheet_row": 500, "merged_tcid": "1"}},
    {"id": "q", "cut7": {"sheet_row": 2000, "merged_tcid": "2"}},
]
ordered3 = R.order_ledger_bottom_to_top(nested)
check("ledger reads nested cut7.sheet_row", [c["id"] for c in ordered3] == ["q", "p"])

# real ledger file shape (mirrors known_boundary_corrections.json's "corrections" key)
tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
import json as _json
_json.dump({"corrections": [
    {"id": "m1", "merged_tcid": "00100000", "cut_frame": 10},
    {"id": "m2", "cut7": {"merged_tcid": "00200000", "sheet_row": 50, "cut_frames": [15]}},
]}, tf)
tf.close()
loaded = R.load_ledger(tf.name)
os.unlink(tf.name)
check("load_ledger reads 'corrections' key", len(loaded) == 2, loaded)
check("load_ledger preserves ids", {c["id"] for c in loaded} == {"m1", "m2"})

# =================================================================================== scenes-CSV rewrite: split
CSV_HEADER = (
    "Timecode List:,00:00:00.000,00:00:10.000\n"
    "Scene Number,Start Frame,Start Timecode,Start Time (seconds),End Frame,End Timecode,"
    "End Time (seconds),Length (frames),Length (timecode),Length (seconds)\n"
)


def _write_csv(rows):
    tf = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    tf.write(CSV_HEADER)
    for r in rows:
        tf.write(",".join(str(x) for x in r) + "\n")
    tf.close()
    return Path(tf.name)


# one scene, 100 frames @24fps (0..99 0-based -> CSV is 1-based inclusive: 1..100)
csv_path = _write_csv([
    (1, 1, "00:00:00.000", "0.000", 100, "00:00:04.167", "4.167", 100, "00:00:04.167", "4.167"),
])
pieces_csv = [
    {"start_frame": 1, "start_tc": "00:00:00.000", "start_time": "0.000",
     "end_frame": 40, "end_tc": "00:00:01.667", "end_time": "1.667",
     "len_frames": 40, "len_tc": "00:00:01.667", "len_time": "1.667"},
    {"start_frame": 41, "start_tc": "00:00:01.667", "start_time": "1.667",
     "end_frame": 100, "end_tc": "00:00:04.167", "end_time": "4.167",
     "len_frames": 60, "len_tc": "00:00:02.500", "len_time": "2.500"},
]
R.rewrite_csv_split(csv_path, 1, pieces_csv, 24)
backup = Path(str(csv_path) + ".bak")
check("rewrite_csv_split writes a .bak", backup.exists())
result_scenes = R.parse_scenes_csv(csv_path)
check("rewrite_csv_split produces 2 rows from 1", len(result_scenes) == 2, result_scenes)
check("rewrite_csv_split renumbers 1..N",
      [s["scene"] for s in result_scenes] == [1, 2], [s["scene"] for s in result_scenes])
check("rewrite_csv_split piece A length", result_scenes[0]["len_frames"] == 40)
check("rewrite_csv_split piece B length", result_scenes[1]["len_frames"] == 60)
check("rewrite_csv_split pieces are contiguous (A end+1 == B start)",
      result_scenes[0]["end_frame"] + 1 == result_scenes[1]["start_frame"])
os.unlink(csv_path)
os.unlink(backup)

# 3-way split (N-way): verify all 3 rows land correctly and stay contiguous
csv_path2 = _write_csv([
    (1, 1, "00:00:00.000", "0.000", 100, "00:00:04.167", "4.167", 100, "00:00:04.167", "4.167"),
])
pieces3 = [
    {"start_frame": 1, "start_tc": "00:00:00.000", "start_time": "0.000",
     "end_frame": 20, "end_tc": "00:00:00.833", "end_time": "0.833",
     "len_frames": 20, "len_tc": "00:00:00.833", "len_time": "0.833"},
    {"start_frame": 21, "start_tc": "00:00:00.833", "start_time": "0.833",
     "end_frame": 60, "end_tc": "00:00:02.500", "end_time": "2.500",
     "len_frames": 40, "len_tc": "00:00:01.667", "len_time": "1.667"},
    {"start_frame": 61, "start_tc": "00:00:02.500", "start_time": "2.500",
     "end_frame": 100, "end_tc": "00:00:04.167", "end_time": "4.167",
     "len_frames": 40, "len_tc": "00:00:01.667", "len_time": "1.667"},
]
R.rewrite_csv_split(csv_path2, 1, pieces3, 24)
scenes3 = R.parse_scenes_csv(csv_path2)
check("rewrite_csv_split 3-way produces 3 rows", len(scenes3) == 3, len(scenes3))
check("rewrite_csv_split 3-way renumbers 1..3", [s["scene"] for s in scenes3] == [1, 2, 3])
check("rewrite_csv_split 3-way lengths", [s["len_frames"] for s in scenes3] == [20, 40, 40])
check("rewrite_csv_split 3-way contiguous throughout",
      all(scenes3[i]["end_frame"] + 1 == scenes3[i + 1]["start_frame"] for i in range(2)))
os.unlink(csv_path2)
os.unlink(str(csv_path2) + ".bak")

# split leaves OTHER scenes untouched
csv_path3 = _write_csv([
    (1, 1, "00:00:00.000", "0.000", 50, "00:00:02.083", "2.083", 50, "00:00:02.083", "2.083"),
    (2, 51, "00:00:02.083", "2.083", 150, "00:00:06.250", "6.250", 100, "00:00:04.167", "4.167"),
])
pieces_only2 = [
    {"start_frame": 51, "start_tc": "00:00:02.083", "start_time": "2.083",
     "end_frame": 90, "end_tc": "00:00:03.750", "end_time": "3.750",
     "len_frames": 40, "len_tc": "00:00:01.667", "len_time": "1.667"},
    {"start_frame": 91, "start_tc": "00:00:03.750", "start_time": "3.750",
     "end_frame": 150, "end_tc": "00:00:06.250", "end_time": "6.250",
     "len_frames": 60, "len_tc": "00:00:02.500", "len_time": "2.500"},
]
R.rewrite_csv_split(csv_path3, 2, pieces_only2, 24)
scenes4 = R.parse_scenes_csv(csv_path3)
check("rewrite_csv_split untouched scene 1 survives", scenes4[0]["len_frames"] == 50, scenes4[0])
check("rewrite_csv_split total rows after splitting scene 2", len(scenes4) == 3, len(scenes4))
check("rewrite_csv_split renumber accounts for untouched scenes",
      [s["scene"] for s in scenes4] == [1, 2, 3])
os.unlink(csv_path3)
os.unlink(str(csv_path3) + ".bak")

# =================================================================================== scenes-CSV rewrite: merge
csv_path4 = _write_csv([
    (1, 1, "00:00:00.000", "0.000", 40, "00:00:01.667", "1.667", 40, "00:00:01.667", "1.667"),
    (2, 41, "00:00:01.667", "1.667", 70, "00:00:02.917", "2.917", 30, "00:00:01.250", "1.250"),
    (3, 71, "00:00:02.917", "2.917", 100, "00:00:04.167", "4.167", 30, "00:00:01.250", "1.250"),
])
scenes_before = R.parse_scenes_csv(csv_path4)
last = scenes_before[2]  # scene 3 = the last absorbed
R.rewrite_csv_merge(csv_path4, keep_scene=1, absorb_scenes=[2, 3], merged_end=last,
                    merged_len=100, merged_dur=4.167)
merged_scenes = R.parse_scenes_csv(csv_path4)
check("rewrite_csv_merge drops absorbed rows", len(merged_scenes) == 1, merged_scenes)
check("rewrite_csv_merge keep row extends to absorbed end",
      merged_scenes[0]["end_frame"] == 100, merged_scenes[0])
check("rewrite_csv_merge keep row length recomputed",
      merged_scenes[0]["len_frames"] == 100, merged_scenes[0])
check("rewrite_csv_merge writes a .bak", Path(str(csv_path4) + ".bak").exists())
os.unlink(csv_path4)
os.unlink(str(csv_path4) + ".bak")

# merge of a middle scene leaves earlier/later scenes alone
csv_path5 = _write_csv([
    (1, 1, "00:00:00.000", "0.000", 20, "00:00:00.833", "0.833", 20, "00:00:00.833", "0.833"),
    (2, 21, "00:00:00.833", "0.833", 40, "00:00:01.667", "1.667", 20, "00:00:00.833", "0.833"),
    (3, 41, "00:00:01.667", "1.667", 60, "00:00:02.500", "2.500", 20, "00:00:00.833", "0.833"),
    (4, 61, "00:00:02.500", "2.500", 80, "00:00:03.333", "3.333", 20, "00:00:00.833", "0.833"),
])
sc5 = R.parse_scenes_csv(csv_path5)
R.rewrite_csv_merge(csv_path5, keep_scene=2, absorb_scenes=[3], merged_end=sc5[2],
                    merged_len=40, merged_dur=1.667)
merged5 = R.parse_scenes_csv(csv_path5)
check("rewrite_csv_merge middle-merge: scene 1 untouched",
      any(s["scene"] == 1 and s["len_frames"] == 20 for s in merged5))
check("rewrite_csv_merge middle-merge: scene 4 untouched",
      any(s["scene"] == 4 and s["start_frame"] == 61 for s in merged5))
check("rewrite_csv_merge middle-merge: scene 3 absent (absorbed)",
      not any(s["scene"] == 3 for s in merged5))
check("rewrite_csv_merge middle-merge: 3 rows remain", len(merged5) == 3, len(merged5))
os.unlink(csv_path5)
os.unlink(str(csv_path5) + ".bak")

print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
