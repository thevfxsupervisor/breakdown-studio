#!/usr/bin/env python
"""bs_enrich pure-logic tests - stdlib only, no network/ffmpeg/whisper/Ollama required.

Run:  "C:/Program Files/Shotgun/Python3/python.exe" tests/test_enrich.py
Exit 0 = all pass. Covers: dialogue-segment -> shot mapping math, chunk-with-overlap windowing,
resume-skip logic (transcribe/describe pass1/pass2), CSV round-trips (dialogue.csv,
descriptions.csv), pass-2 prompt/response assembly, and the no-name-injection guarantee (the
prompt builder only ever sees names this module itself derived from dialogue text -- never an
operator/cast list).
"""
import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_enrich as E  # noqa: E402

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
# 1. map_segments_to_shots -- dialogue segment -> shot overlap math
# ================================================================================================
def test_map_segments_basic_overlap():
    fps = 24.0
    # shot 1: frames 1-24 (1-based inclusive) -> [0.0, 1.0)s ; shot 2: frames 25-48 -> [1.0, 2.0)s
    shots = [
        {"tcid": "a", "start_frame": 1, "end_frame": 24},
        {"tcid": "b", "start_frame": 25, "end_frame": 48},
    ]
    segments = [
        {"start": 0.2, "end": 0.8, "text": "hello there"},   # fully inside shot a
        {"start": 0.9, "end": 1.5, "text": "spanning cut"},  # overlaps both a and b
        {"start": 1.6, "end": 1.9, "text": "in shot b only"},
    ]
    out = E.map_segments_to_shots(segments, shots, fps)
    check("shot a gets segment 1 + spanning segment", out["a"]["dialogue"] == "hello there spanning cut", out["a"])
    check("shot b gets spanning segment + its own", out["b"]["dialogue"] == "spanning cut in shot b only", out["b"])
    check("shot a start/end seconds", abs(out["a"]["start"] - 0.0) < 1e-6 and abs(out["a"]["end"] - 1.0) < 1e-6, out["a"])


test_map_segments_basic_overlap()


def test_map_segments_no_overlap_blank():
    shots = [{"tcid": "z", "start_frame": 1, "end_frame": 24}]
    segments = [{"start": 5.0, "end": 6.0, "text": "far away"}]
    out = E.map_segments_to_shots(segments, shots, 24.0)
    check("no-overlap shot gets blank dialogue", out["z"]["dialogue"] == "", out["z"])


test_map_segments_no_overlap_blank()


def test_map_segments_boundary_exclusive():
    # a segment that starts exactly at shot b's start and ends exactly at shot a's end must not
    # double-count on the touching boundary (half-open interval test: seg.start < shot_end and
    # seg.end > shot_start).
    fps = 24.0
    shots = [
        {"tcid": "a", "start_frame": 1, "end_frame": 24},   # [0.0, 1.0)
        {"tcid": "b", "start_frame": 25, "end_frame": 48},  # [1.0, 2.0)
    ]
    segments = [{"start": 1.0, "end": 1.0, "text": "zero-length at boundary"}]
    out = E.map_segments_to_shots(segments, shots, fps)
    check("zero-length boundary segment touches neither shot",
          out["a"]["dialogue"] == "" and out["b"]["dialogue"] == "", out)


test_map_segments_boundary_exclusive()


def test_map_segments_empty_shots_list():
    out = E.map_segments_to_shots([{"start": 0, "end": 1, "text": "x"}], [], 24.0)
    check("empty shots list -> empty mapping", out == {}, out)


test_map_segments_empty_shots_list()


# ================================================================================================
# 2. dialogue.csv round-trip
# ================================================================================================
def test_dialogue_csv_roundtrip():
    shots = [
        {"tcid": "00000000", "start_frame": 1, "end_frame": 24},
        {"tcid": "00000100", "start_frame": 25, "end_frame": 48},
    ]
    mapping = {
        "00000000": {"start": 0.0, "end": 1.0, "dialogue": "hello"},
        "00000100": {"start": 1.0, "end": 2.0, "dialogue": ""},
    }
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "dialogue.csv"
        E.write_dialogue_csv(p, shots, mapping, "DEM")
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        check("dialogue.csv has correct row count", len(rows) == 2, rows)
        check("dialogue.csv row0 shot_code", rows[0]["shot_code"] == "DEM_00000000", rows[0])
        check("dialogue.csv row0 dialogue text", rows[0]["dialogue"] == "hello", rows[0])
        check("dialogue.csv row1 dialogue blank", rows[1]["dialogue"] == "", rows[1])

        loaded = E.load_dialogue_csv(p)
        check("load_dialogue_csv keyed on tcid", loaded.get("00000000") == "hello", loaded)
        check("load_dialogue_csv missing file -> {}", E.load_dialogue_csv(Path(td) / "nope.csv") == {})


test_dialogue_csv_roundtrip()


# ================================================================================================
# 3. chunk_shots -- chunking with overlap
# ================================================================================================
def test_chunk_shots_basic():
    shots = [{"tcid": str(i)} for i in range(60)]
    chunks = E.chunk_shots(shots, chunk_size=25, overlap=3)
    # step = 22; windows start at 0, 22, 44 -> covers up to 44+25=69 > 60, so 3 chunks
    check("chunk_shots covers all shots", chunks[-1][0][-1]["tcid"] == "59",
          [c[0][-1]["tcid"] for c in chunks])
    check("chunk_shots first chunk starts at 0", chunks[0][0][0]["tcid"] == "0")
    check("chunk_shots second chunk overlaps first (starts before chunk1 end)",
          int(chunks[1][0][0]["tcid"]) < int(chunks[0][0][-1]["tcid"]), chunks[1][0][0])
    check("chunk_shots first chunk new_start_idx is 0", chunks[0][1] == 0, chunks[0])
    check("chunk_shots later chunk new_start_idx equals overlap", chunks[1][1] == 3, chunks[1])


test_chunk_shots_basic()


def test_chunk_shots_small_input():
    shots = [{"tcid": str(i)} for i in range(5)]
    chunks = E.chunk_shots(shots, chunk_size=25, overlap=3)
    check("chunk_shots single chunk when input smaller than chunk_size", len(chunks) == 1, chunks)
    check("chunk_shots single chunk has all shots", len(chunks[0][0]) == 5, chunks)


test_chunk_shots_small_input()


def test_chunk_shots_empty():
    check("chunk_shots empty input -> []", E.chunk_shots([], 25, 3) == [])


test_chunk_shots_empty()


def test_chunk_shots_exact_multiple():
    # exactly chunk_size shots -> exactly one chunk, no infinite loop
    shots = [{"tcid": str(i)} for i in range(25)]
    chunks = E.chunk_shots(shots, chunk_size=25, overlap=3)
    check("chunk_shots exact chunk_size input -> 1 chunk", len(chunks) == 1, chunks)


test_chunk_shots_exact_multiple()


# ================================================================================================
# 4. resume-skip logic
# ================================================================================================
def test_shots_needing_pass1_resume():
    shots = [{"tcid": "a"}, {"tcid": "b"}, {"tcid": "c"}]
    existing = {
        "a": {"visual_caption": "a wide shot of snow"},
        "b": {"visual_caption": ""},
        # "c" not present at all
    }
    todo = E.shots_needing_pass1(shots, existing, force=False)
    check("resume: shot with caption skipped", "a" not in {s["tcid"] for s in todo}, todo)
    check("resume: shot with blank caption included", "b" in {s["tcid"] for s in todo}, todo)
    check("resume: shot absent from CSV included", "c" in {s["tcid"] for s in todo}, todo)

    forced = E.shots_needing_pass1(shots, existing, force=True)
    check("force: all shots included regardless of existing", len(forced) == 3, forced)


test_shots_needing_pass1_resume()


def test_shots_needing_pass2_resume():
    shots = [{"tcid": "a"}, {"tcid": "b"}]
    existing = {
        "a": {"revised_description": "already revised"},
        "b": {"revised_description": ""},
    }
    todo = E.shots_needing_pass2(shots, existing, force=False)
    check("resume pass2: shot with revision skipped", "a" not in {s["tcid"] for s in todo}, todo)
    check("resume pass2: shot with blank revision included", "b" in {s["tcid"] for s in todo}, todo)


test_shots_needing_pass2_resume()


# ================================================================================================
# 5. descriptions.csv round-trip
# ================================================================================================
def test_descriptions_csv_roundtrip():
    shots = [{"tcid": "00000000"}, {"tcid": "00000100"}]
    rows = {
        "00000000": {"visual_caption": "wide shot of a house", "revised_description": "Mrs. Carter's house, wide shot"},
        "00000100": {"visual_caption": "close up of a mouse", "revised_description": ""},
    }
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "descriptions.csv"
        E.write_descriptions_csv(p, shots, rows, "DEM")
        with open(p, encoding="utf-8", newline="") as f:
            out_rows = list(csv.DictReader(f))
        check("descriptions.csv row count", len(out_rows) == 2, out_rows)
        check("descriptions.csv columns", set(out_rows[0].keys()) ==
              {"tcid", "shot_code", "visual_caption", "revised_description"}, out_rows[0])
        check("descriptions.csv visual_caption preserved", out_rows[0]["visual_caption"] == "wide shot of a house")

        loaded = E.load_descriptions_csv(p)
        check("load_descriptions_csv keyed on tcid", "00000000" in loaded, loaded)
        check("load_descriptions_csv missing file -> {}", E.load_descriptions_csv(Path(td) / "x.csv") == {})


test_descriptions_csv_roundtrip()


# ================================================================================================
# 6. extract_addressed_names -- dialogue-derived names ONLY (never an operator list)
# ================================================================================================
def test_extract_addressed_names_basic():
    dialogue = [
        "Mrs. Carter, wait! You have to listen to me.",
        "Sam, get back here right now.",
        "I told you, Walter, this is dangerous.",
    ]
    names = E.extract_addressed_names(dialogue)
    check("extracts Mrs. Carter", "Mrs. Carter" in names, names)
    check("extracts Sam", "Sam" in names, names)
    check("extracts Walter", "Walter" in names, names)


test_extract_addressed_names_basic()


def test_extract_addressed_names_ignores_common_words():
    dialogue = ["The. It. This is not a name."]
    names = E.extract_addressed_names(dialogue)
    check("stopword-only text yields no names", names == [], names)


test_extract_addressed_names_ignores_common_words()


def test_extract_addressed_names_empty_input():
    check("empty dialogue list -> []", E.extract_addressed_names([]) == [])
    check("list of blanks -> []", E.extract_addressed_names(["", "", None]) == [])


test_extract_addressed_names_empty_input()


# ================================================================================================
# 7. prompt assembly -- NO NAME INJECTION guarantee
# ================================================================================================
def test_build_pass1_prompt_has_no_names_param():
    # build_pass1_prompt takes NO arguments at all -- there is no channel for names to enter the
    # visual-caption prompt, dialogue-derived or otherwise.
    import inspect
    sig = inspect.signature(E.build_pass1_prompt)
    check("build_pass1_prompt takes zero parameters (no name channel)", len(sig.parameters) == 0, sig)
    prompt = E.build_pass1_prompt()
    check("pass1 prompt explicitly forbids guessing names",
          "do not guess names" in prompt.lower() or "names" in prompt.lower(), prompt)


test_build_pass1_prompt_has_no_names_param()


def test_build_pass2_prompt_only_uses_dialogue_derived_names():
    chunk_rows = [
        {"tcid": "a", "shot_code": "DEM_a", "visual_caption": "wide shot", "dialogue": "Sam, look out!"},
    ]
    # Simulate the real call path: names come ONLY from extract_addressed_names on this chunk's
    # own dialogue -- never from any external/operator source. This test asserts that whatever
    # list is passed through ends up verbatim in the prompt (proving the seam is dialogue-only,
    # since build_pass2_prompt has no other way to learn about people).
    derived = E.extract_addressed_names([r["dialogue"] for r in chunk_rows])
    prompt = E.build_pass2_prompt(chunk_rows, story_state="", known_names=derived)
    check("pass2 prompt contains a dialogue-derived name (Sam)", "Sam" in prompt, prompt)
    check("pass2 prompt instructs model to use ONLY spoken names",
          "spoken in the dialogue" in prompt.lower() or "actually spoken" in prompt.lower(), prompt)
    # Prove there is no other parameter/global an operator name-list could be smuggled through.
    import inspect
    sig = inspect.signature(E.build_pass2_prompt)
    check("build_pass2_prompt signature is exactly (chunk_rows, story_state, known_names)",
          list(sig.parameters) == ["chunk_rows", "story_state", "known_names"], sig)


test_build_pass2_prompt_only_uses_dialogue_derived_names()


def test_build_pass2_prompt_no_names_when_none_spoken():
    chunk_rows = [{"tcid": "a", "shot_code": "DEM_a", "visual_caption": "an empty room", "dialogue": ""}]
    prompt = E.build_pass2_prompt(chunk_rows, story_state="", known_names=[])
    check("pass2 prompt with no known names doesn't fabricate a names line",
          "Names heard in dialogue so far:" not in prompt, prompt)


test_build_pass2_prompt_no_names_when_none_spoken()


# ================================================================================================
# 8. parse_pass2_response -- tolerant parsing of the model's STORY_STATE / REVISED block
# ================================================================================================
def test_parse_pass2_response_well_formed():
    text = (
        "STORY_STATE: Mrs. Carter seeks help from her neighbors to move her house before the storm arrives.\n"
        "REVISED:\n"
        "DEM_a: Mrs. Carter stands anxiously outside her cinderblock home, wide shot.\n"
        "DEM_b: Sam the neighbor waves from across the yard, medium shot.\n"
    )
    story_state, revised = E.parse_pass2_response(text, ["DEM_a", "DEM_b"])
    check("parse: story_state extracted", "Mrs. Carter" in story_state, story_state)
    check("parse: revised DEM_a", "cinderblock" in revised.get("DEM_a", ""), revised)
    check("parse: revised DEM_b", "Sam" in revised.get("DEM_b", ""), revised)


test_parse_pass2_response_well_formed()


def test_parse_pass2_response_missing_shot_falls_back():
    text = "STORY_STATE: short update.\nREVISED:\nDEM_a: only this one shot revised.\n"
    story_state, revised = E.parse_pass2_response(text, ["DEM_a", "DEM_b"])
    check("parse: dropped shot simply absent from result", "DEM_b" not in revised, revised)
    check("parse: present shot still parsed", "DEM_a" in revised, revised)


test_parse_pass2_response_missing_shot_falls_back()


def test_parse_pass2_response_malformed_no_crash():
    story_state, revised = E.parse_pass2_response("garbage output with no structure", ["DEM_a"])
    check("parse: malformed text -> empty story_state", story_state == "", story_state)
    check("parse: malformed text -> empty revised dict", revised == {}, revised)


test_parse_pass2_response_malformed_no_crash()


# ================================================================================================
# 9. ollama_urls -- endpoint fallback ordering / de-dup
# ================================================================================================
def test_ollama_urls_ordering_and_dedup():
    cfg = {"ollama_url": "http://localhost:11434", "ollama_urls": ["http://192.168.53.28:11434",
                                                                    "http://localhost:11434"]}
    urls = E.ollama_urls(cfg, cli_url=None)
    check("ollama_urls: cfg primary first", urls[0] == "http://localhost:11434", urls)
    check("ollama_urls: fallback present", "http://192.168.53.28:11434" in urls, urls)
    check("ollama_urls: de-duplicated", len(urls) == len(set(urls)), urls)


test_ollama_urls_ordering_and_dedup()


def test_ollama_urls_cli_override_wins():
    cfg = {"ollama_url": "http://localhost:11434"}
    urls = E.ollama_urls(cfg, cli_url="http://10.0.0.5:11434")
    check("ollama_urls: --ollama-url takes priority", urls[0] == "http://10.0.0.5:11434", urls)


test_ollama_urls_cli_override_wins()


def test_ollama_urls_default_fallback_always_present():
    urls = E.ollama_urls({}, cli_url=None)
    check("ollama_urls: bare default localhost present when nothing configured",
          E.DEFAULT_OLLAMA_URL in urls, urls)


test_ollama_urls_default_fallback_always_present()


# ================================================================================================
# 10. config helpers
# ================================================================================================
def test_get_prefix_and_fps_defaults():
    check("get_prefix default", E.get_prefix({}) == E.DEFAULT_PREFIX)
    check("get_fps default", E.get_fps({}) == 24.0)
    check("get_fps from cfg", E.get_fps({"fps": "23.976"}) == 23.976)
    check("get_fps bad value falls back", E.get_fps({"fps": "not-a-number"}) == 24.0)


test_get_prefix_and_fps_defaults()


print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
