#!/usr/bin/env python
"""Pure-logic unit tests for bs_gsheets.py's OCR-enrichment + real-TC features (FEATURE 1 / 2).

Run:  "C:/Program Files/Shotgun/Python3/python.exe" tests/test_gsheets_enrich.py
Exit 0 = all pass. Stdlib only -- the Sheets service is mocked with plain dicts/lists, never a
live connection. Covers: the slate_ocr.csv/vfxnote_ocr.csv join, the non-clobber rule (empty vs.
operator-corrected vs. already-correct), Slate Takes label building, and the file-TC + offset ->
real show-TC math (parse_tc_offset_file / real_tc / plan_real_tc_writes).
"""
import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_gsheets as G  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


# ================================================================================================
# 1. slate_takes_label
# ================================================================================================
check("slate_takes_label basic", G.slate_takes_label("125", "I") == "125 I",
      G.slate_takes_label("125", "I"))
check("slate_takes_label no take", G.slate_takes_label("125", "") == "125",
      G.slate_takes_label("125", ""))
check("slate_takes_label no slate -> blank", G.slate_takes_label("", "I") == "",
      G.slate_takes_label("", "I"))
check("slate_takes_label strips whitespace", G.slate_takes_label(" 125 ", " I ") == "125 I")


# ================================================================================================
# 2. build_enrichment_for_shot -- the slate_ocr.csv / vfxnote_ocr.csv join
# ================================================================================================
def test_build_enrichment_full_row():
    slate_row = {"scene": "109", "slate": "101", "take_roman": "I",
                 "official_code": "DEM_0101_01_010"}
    note_row = {"vfx_note": "STABILIZE", "is_vfx": "1"}
    out = G.build_enrichment_for_shot("00000000", slate_row, note_row)
    check("enrichment: scene", out.get("scene") == "109", out)
    check("enrichment: slate", out.get("slate") == "101", out)
    check("enrichment: take", out.get("take") == "I", out)
    check("enrichment: slate_takes", out.get("slate_takes") == "101 I", out)
    check("enrichment: shot_code", out.get("shot_code") == "DEM_0101_01_010", out)
    check("enrichment: vfx_note", out.get("vfx_note") == "STABILIZE", out)


test_build_enrichment_full_row()


def test_build_enrichment_note_not_vfx_skipped():
    # note OCR row present but is_vfx=0 -> vfx_note must NOT be populated (only OCR-marked-VFX
    # rows get a note write, per the spec).
    note_row = {"vfx_note": "some bleed text", "is_vfx": "0"}
    out = G.build_enrichment_for_shot("t1", None, note_row)
    check("enrichment: is_vfx=0 -> no vfx_note field at all", "vfx_note" not in out, out)


test_build_enrichment_note_not_vfx_skipped()


def test_build_enrichment_no_slate_no_note():
    out = G.build_enrichment_for_shot("t2", None, None)
    check("enrichment: no slate/note rows -> empty dict", out == {}, out)


test_build_enrichment_no_slate_no_note()


def test_build_enrichment_unparsed_slate():
    # slate_ocr.csv row with a BLANK slate (unparseable burn-in, e.g. a stock-footage plate) must
    # not fabricate scene/slate/take/shot_code/slate_takes fields.
    slate_row = {"scene": "", "slate": "", "take_roman": "", "official_code": ""}
    out = G.build_enrichment_for_shot("t3", slate_row, None)
    check("enrichment: blank slate row -> no fields written", out == {}, out)


test_build_enrichment_unparsed_slate()


# ================================================================================================
# 3. plan_enrichment_writes -- the non-clobber rule (empty / already-correct / operator-corrected)
# ================================================================================================
def test_plan_enrichment_writes_non_clobber():
    shots = [{"tcid": "a"}, {"tcid": "b"}, {"tcid": "c"}, {"tcid": "d"}]
    slate_by_tcid = {
        "a": {"scene": "109", "slate": "101", "take_roman": "I", "official_code": "DEM_0101_01_010"},
        "b": {"scene": "110", "slate": "102", "take_roman": "I", "official_code": "DEM_0102_01_010"},
        "c": {"scene": "111", "slate": "103", "take_roman": "I", "official_code": "DEM_0103_01_010"},
        "d": {"scene": "", "slate": "", "take_roman": "", "official_code": ""},  # unreadable
    }
    note_by_tcid = {}
    existing_row_values = {
        "a": {"scene": "", "slate": "", "take": "", "slate_takes": "", "shot_code": ""},  # empty -> write
        "b": {"scene": "110", "slate": "102", "take": "I", "slate_takes": "102 I",
              "shot_code": "DEM_0102_01_010"},  # already correct -> no-op (not counted as write)
        "c": {"scene": "OPERATOR OVERRIDE", "slate": "999", "take": "I", "slate_takes": "999 I",
              "shot_code": "SHW_9999_01_999"},  # operator-corrected, differs -> must be KEPT
    }
    enrich_fields = {"scene", "slate", "take", "slate_takes", "shot_code"}
    writes, summary = G.plan_enrichment_writes(shots, slate_by_tcid, note_by_tcid,
                                               existing_row_values, enrich_fields)
    check("non-clobber: empty cell (a) gets written", "a" in writes and writes["a"]["scene"] == "109", writes)
    check("non-clobber: already-correct cell (b) produces no write", "b" not in writes, writes)
    check("non-clobber: operator-corrected cell (c) is left alone entirely", "c" not in writes, writes)
    check("non-clobber: unreadable slate row (d) produces no write", "d" not in writes, writes)
    # row c's existing "take" (I) happens to already match the OCR take -> that single field is
    # a no-op, not a clobber; the other 4 fields (scene/slate/slate_takes/shot_code) genuinely
    # differ from OCR and must be counted as operator-kept.
    check("non-clobber: operator_kept counts the 4 genuinely differing fields on row c",
          summary["operator_kept"] == 4, summary)
    check("non-clobber: enriched counts only row a", summary["enriched"] == 1, summary)


test_plan_enrichment_writes_non_clobber()


def test_plan_enrichment_writes_vfx_note_gating():
    shots = [{"tcid": "x"}, {"tcid": "y"}]
    slate_by_tcid = {}
    note_by_tcid = {
        "x": {"vfx_note": "WIRE REMOVAL", "is_vfx": "1"},
        "y": {"vfx_note": "should not appear", "is_vfx": "0"},
    }
    existing_row_values = {}
    writes, summary = G.plan_enrichment_writes(shots, slate_by_tcid, note_by_tcid,
                                               existing_row_values, {"vfx_note"})
    check("vfx_note: is_vfx=1 row writes the note", writes.get("x", {}).get("vfx_note") == "WIRE REMOVAL", writes)
    check("vfx_note: is_vfx=0 row writes nothing", "y" not in writes, writes)


test_plan_enrichment_writes_vfx_note_gating()


def test_plan_enrichment_writes_field_not_in_sheet_skipped():
    # A field present in the OCR data but NOT in enrich_fields (i.e. the sheet has no such header)
    # must simply be dropped, not raise or get written anywhere.
    shots = [{"tcid": "z"}]
    slate_by_tcid = {"z": {"scene": "109", "slate": "101", "take_roman": "I",
                           "official_code": "DEM_0101_01_010"}}
    writes, summary = G.plan_enrichment_writes(shots, slate_by_tcid, {}, {}, {"shot_code"})
    check("field not in sheet: only shot_code written, others silently skipped",
          writes == {"z": {"shot_code": "DEM_0101_01_010"}}, writes)


test_plan_enrichment_writes_field_not_in_sheet_skipped()


# ================================================================================================
# 4. slate_ocr.csv / vfxnote_ocr.csv loaders -- round-trip through a real CSV file
# ================================================================================================
def test_load_slate_ocr_csv_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "slate_ocr.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["tcid", "scene", "slate", "take_roman", "take_int",
                                              "official_code", "tier"])
            w.writeheader()
            w.writerow({"tcid": "00000000", "scene": "109", "slate": "101", "take_roman": "I",
                       "take_int": "1", "official_code": "DEM_0101_01_010", "tier": "OK"})
        out = G.load_slate_ocr_csv(p)
        check("load_slate_ocr_csv: keyed on tcid", "00000000" in out, out)
        check("load_slate_ocr_csv: fields intact", out["00000000"]["slate"] == "101", out)


test_load_slate_ocr_csv_roundtrip()


def test_load_vfxnote_ocr_csv_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "vfxnote_ocr.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["tcid", "vfx_note", "tier", "is_vfx"])
            w.writeheader()
            w.writerow({"tcid": "00000000", "vfx_note": "STABILIZE", "tier": "ALL3", "is_vfx": "1"})
        out = G.load_vfxnote_ocr_csv(p)
        check("load_vfxnote_ocr_csv: keyed on tcid", "00000000" in out, out)
        check("load_vfxnote_ocr_csv: is_vfx preserved as string '1'", out["00000000"]["is_vfx"] == "1", out)


test_load_vfxnote_ocr_csv_roundtrip()


# ================================================================================================
# 5. FEATURE 2 -- real show-TC offset math
# ================================================================================================
check("_smpte_to_frames / _frames_to_smpte round-trip @24fps",
      G._frames_to_smpte(G._smpte_to_frames("01:02:03:04", 24), 24) == "01:02:03:04")

check("real_tc: file TC + offset = real TC",
      G.real_tc("00:00:10:00", G._smpte_to_frames("00:59:50:00", 24), 24) == "01:00:00:00",
      G.real_tc("00:00:10:00", G._smpte_to_frames("00:59:50:00", 24), 24))

check("real_tc: zero offset -> unchanged",
      G.real_tc("00:10:00:00", 0, 24) == "00:10:00:00")


def test_parse_tc_offset_file_constant():
    text = ("fps=24.0, 5 shots sampled\n"
            "...\n"
            "offsets: n=5  min=00:59:50:00  max=00:59:50:00  spread=0 frames\n"
            "=> CONSTANT\n"
            "median offset = 00:59:50:00 (86160 frames @ 24.0fps)\n")
    info = G.parse_tc_offset_file(text)
    check("parse_tc_offset_file: extracts median frames", info["median_offset_frames"] == 86160, info)
    check("parse_tc_offset_file: extracts median tc", info["median_offset_tc"] == "00:59:50:00", info)
    check("parse_tc_offset_file: constant flag True", info["constant"] is True, info)


test_parse_tc_offset_file_constant()


def test_parse_tc_offset_file_not_constant():
    text = ("fps=24.0, 5 shots sampled\n"
            "offsets: n=5  min=00:10:00:00  max=01:40:00:00  spread=129600 frames\n"
            "=> DRIFTS (check fps mismatch, e.g. 23.976 vs 24)\n"
            "median offset = 00:55:00:00 (79200 frames @ 24.0fps)\n")
    info = G.parse_tc_offset_file(text)
    check("parse_tc_offset_file: drifting offset -> constant False", info["constant"] is False, info)


test_parse_tc_offset_file_not_constant()


def test_parse_tc_offset_file_no_data():
    text = "fps=24.0, 5 shots sampled\n(no offsets read)\n"
    info = G.parse_tc_offset_file(text)
    check("parse_tc_offset_file: no usable offset -> None", info is None, info)


test_parse_tc_offset_file_no_data()


def test_plan_real_tc_writes_basic_and_non_clobber():
    shots = [
        {"tcid": "a", "tc_in": "00:00:10:00", "duration": "24"},   # 1s @24fps -> 24 frames, out=00:00:10:23
        {"tcid": "b", "tc_in": "00:00:20:00", "duration": "12"},
    ]
    offset_frames = G._smpte_to_frames("00:59:50:00", 24)
    existing = {
        "a": {"real_tc_in": "", "real_tc_out": ""},                       # empty -> write both
        "b": {"real_tc_in": "OPERATOR OVERRIDE", "real_tc_out": ""},      # in kept, out written
    }
    writes, summary = G.plan_real_tc_writes(shots, offset_frames, 24, existing,
                                            {"real_tc_in", "real_tc_out"})
    check("real_tc writes: row a gets real_tc_in", writes.get("a", {}).get("real_tc_in") == "01:00:00:00", writes)
    check("real_tc writes: row a gets real_tc_out",
          "real_tc_out" in writes.get("a", {}), writes)
    check("real_tc writes: row b real_tc_in NOT clobbered (operator-kept)",
          "real_tc_in" not in writes.get("b", {}), writes)
    check("real_tc writes: row b real_tc_out still written (was empty)",
          "real_tc_out" in writes.get("b", {}), writes)
    check("real_tc writes: operator_kept counted", summary["operator_kept"] == 1, summary)


test_plan_real_tc_writes_basic_and_non_clobber()


def test_plan_real_tc_writes_no_offset_no_crash():
    # zero duration / missing tc_in shots must be skipped cleanly, not raise.
    shots = [{"tcid": "z", "tc_in": "", "duration": "0"}]
    writes, summary = G.plan_real_tc_writes(shots, 0, 24, {}, {"real_tc_in", "real_tc_out"})
    check("real_tc writes: blank tc_in shot produces no writes", writes == {}, writes)


test_plan_real_tc_writes_no_offset_no_crash()


# ================================================================================================
# 6. demo-data end-to-end (synthetic DEM_ prefix, zero client data): drive the pure enrichment
#    pipeline directly off the checked-in demo OCR CSVs, proving FEATURE 1 without touching any
#    real Google Sheet.
# ================================================================================================
def test_demo_data_enrichment_end_to_end():
    demo_dir = Path(__file__).resolve().parent.parent / "demo" / "output" / "demo_cut"
    slate_csv = demo_dir / "slate_ocr.csv"
    notes_csv = demo_dir / "vfxnote_ocr.csv"
    if not (slate_csv.exists() and notes_csv.exists()):
        print("  skip demo-data enrichment test (demo OCR CSVs not present)")
        return
    slate_by_tcid = G.load_slate_ocr_csv(slate_csv)
    note_by_tcid = G.load_vfxnote_ocr_csv(notes_csv)
    shots = [{"tcid": t} for t in slate_by_tcid]
    enrich_fields = {"scene", "slate", "take", "slate_takes", "shot_code", "vfx_note"}
    writes, summary = G.plan_enrichment_writes(shots, slate_by_tcid, note_by_tcid, {}, enrich_fields)
    check("demo data: at least one shot enriched", summary["enriched"] > 0, summary)
    check("demo data: no operator_kept on a totally empty sheet", summary["operator_kept"] == 0, summary)
    check("demo data: every write uses only DEM_-prefixed shot codes (no client codes)",
          all(str(v.get("shot_code", "DEM_")).startswith("DEM_") for v in writes.values()), writes)
    # the OCR fixture has at least one BLANK/SUSPECT slate row (tcid 00010306, a stock-footage
    # burn-in) -- it must never fabricate a shot_code.
    check("demo data: unreadable-slate shot (00010306) writes no shot_code",
          "shot_code" not in writes.get("00010306", {}), writes.get("00010306"))


test_demo_data_enrichment_end_to_end()


# ================================================================================================
# 7. FEATURE 3 -- bs_enrich.py descriptions.csv / dialogue.csv join (Description / Dialogue cols)
# ================================================================================================
def test_build_desc_dialogue_enrichment_prefers_revised():
    desc_row = {"visual_caption": "a wide shot of a house", "revised_description": "a family home, wide shot"}
    out = G.build_desc_dialogue_enrichment_for_shot("t1", desc_row, None)
    check("desc/dialogue join: prefers revised_description over visual_caption",
          out.get("description") == "a family home, wide shot", out)


test_build_desc_dialogue_enrichment_prefers_revised()


def test_build_desc_dialogue_enrichment_falls_back_to_caption():
    desc_row = {"visual_caption": "a wide shot of a house", "revised_description": ""}
    out = G.build_desc_dialogue_enrichment_for_shot("t2", desc_row, None)
    check("desc/dialogue join: falls back to visual_caption when no revision yet",
          out.get("description") == "a wide shot of a house", out)


test_build_desc_dialogue_enrichment_falls_back_to_caption()


def test_build_desc_dialogue_enrichment_dialogue_field():
    dialogue_row = {"dialogue": "hello there"}
    out = G.build_desc_dialogue_enrichment_for_shot("t3", None, dialogue_row)
    check("desc/dialogue join: dialogue field populated", out.get("dialogue") == "hello there", out)
    check("desc/dialogue join: no description field when desc_row is None", "description" not in out, out)


test_build_desc_dialogue_enrichment_dialogue_field()


def test_build_desc_dialogue_enrichment_empty_rows():
    out = G.build_desc_dialogue_enrichment_for_shot("t4", None, None)
    check("desc/dialogue join: both rows missing -> empty dict", out == {}, out)
    out2 = G.build_desc_dialogue_enrichment_for_shot(
        "t5", {"visual_caption": "", "revised_description": ""}, {"dialogue": ""})
    check("desc/dialogue join: blank values -> empty dict (no fabricated writes)", out2 == {}, out2)


test_build_desc_dialogue_enrichment_empty_rows()


def test_plan_enrichment_writes_with_desc_dialogue_non_clobber():
    shots = [{"tcid": "a"}, {"tcid": "b"}]
    desc_by_tcid = {
        "a": {"visual_caption": "cap a", "revised_description": "revised a"},
        "b": {"visual_caption": "cap b", "revised_description": "revised b"},
    }
    dialogue_by_tcid = {"a": {"dialogue": "spoken line a"}}
    existing_row_values = {
        "a": {"description": "", "dialogue": ""},                      # empty -> both written
        "b": {"description": "OPERATOR WROTE THIS", "dialogue": ""},   # description operator-kept
    }
    writes, summary = G.plan_enrichment_writes(
        shots, {}, {}, existing_row_values, {"description", "dialogue"},
        desc_by_tcid=desc_by_tcid, dialogue_by_tcid=dialogue_by_tcid)
    check("desc/dialogue writes: row a description written", writes.get("a", {}).get("description") == "revised a", writes)
    check("desc/dialogue writes: row a dialogue written", writes.get("a", {}).get("dialogue") == "spoken line a", writes)
    check("desc/dialogue writes: row b description NOT clobbered (operator-kept)",
          "description" not in writes.get("b", {}), writes)
    check("desc/dialogue writes: row b description counted as operator_kept", summary["operator_kept"] == 1, summary)


test_plan_enrichment_writes_with_desc_dialogue_non_clobber()


def test_plan_enrichment_writes_desc_dialogue_optional_args_default_empty():
    # Callers that don't pass desc_by_tcid/dialogue_by_tcid at all (older call sites, or a build
    # with neither CSV present) must behave exactly as before FEATURE 3 was added -- no crash, no
    # phantom fields.
    shots = [{"tcid": "a"}]
    slate_by_tcid = {"a": {"scene": "1", "slate": "10", "take_roman": "I", "official_code": "DEM_0010_01_010"}}
    writes, summary = G.plan_enrichment_writes(shots, slate_by_tcid, {}, {}, {"shot_code"})
    check("desc/dialogue optional args: backward-compatible call still works",
          writes.get("a", {}).get("shot_code") == "DEM_0010_01_010", writes)


test_plan_enrichment_writes_desc_dialogue_optional_args_default_empty()


def test_load_descriptions_csv_and_dialogue_csv_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        dp = Path(td) / "descriptions.csv"
        with open(dp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["tcid", "shot_code", "visual_caption", "revised_description"])
            w.writeheader()
            w.writerow({"tcid": "00000000", "shot_code": "DEM_00000000", "visual_caption": "cap",
                       "revised_description": "revised"})
        out = G.load_descriptions_csv(dp)
        check("load_descriptions_csv: keyed on tcid", "00000000" in out, out)
        check("load_descriptions_csv: revised_description intact", out["00000000"]["revised_description"] == "revised", out)

        gp = Path(td) / "dialogue.csv"
        with open(gp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["tcid", "shot_code", "start", "end", "dialogue"])
            w.writeheader()
            w.writerow({"tcid": "00000000", "shot_code": "DEM_00000000", "start": "0.0", "end": "1.0",
                       "dialogue": "hi"})
        out2 = G.load_dialogue_csv(gp)
        check("load_dialogue_csv: keyed on tcid", "00000000" in out2, out2)
        check("load_dialogue_csv: dialogue text intact", out2["00000000"]["dialogue"] == "hi", out2)


test_load_descriptions_csv_and_dialogue_csv_roundtrip()


print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
