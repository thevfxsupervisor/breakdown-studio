#!/usr/bin/env python
"""Pure-logic unit tests for bs_match.py - the cross-cut matching + audit module.

Run:  python tests/test_match.py         (from the breakdown_studio folder)
Exit 0 = all pass. Stdlib only (same convention as tests/smoke_test.py) -- no numpy/cv2/torch
needed because these tests exercise only the pure-logic pieces (assign/audit), not compare
(which needs opencv) or the CLIP/hash visual backends (exercised via injected fake sim functions).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_match as M  # noqa: E402

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
# 1. parse_code / is_protected - basic parsing sanity
# ================================================================================================
check("parse_code standard", M.parse_code("SHW_0519_3_20") == (519, 3, 20), M.parse_code("SHW_0519_3_20"))
check("parse_code any prefix", M.parse_code("XYZ_0001_1_010") == (1, 1, 10))
check("parse_code garbage -> None", M.parse_code("not-a-code") is None)
check("parse_code empty -> None", M.parse_code("") is None)
check("parse_code None -> None", M.parse_code(None) is None)

check("is_protected 'NEW Cut7'", M.is_protected("NEW Cut7") is True)
check("is_protected 'manual add'", M.is_protected("manual add") is True)
check("is_protected 'added by operator'", M.is_protected("added by operator") is True)
check("is_protected case-insensitive", M.is_protected("MANUAL") is True)
check("is_protected clean text -> False", M.is_protected("looks fine") is False)
check("is_protected empty/None -> False", M.is_protected("") is False and M.is_protected(None) is False)


# ================================================================================================
# 2. 1:1 uniqueness invariant - construct a COLLISION scenario, assert global assignment resolves it
# ================================================================================================
# Two new-cut shots that would BOTH argmax to the same master row under a naive per-shot best-match
# (e.g. both have the exact same code by construction, or both visually resemble the same master
# shot best). The global assignment must give the master row to exactly one of them.
def test_uniqueness_exact_code_collision():
    # Two new-cut shots claim to have the SAME exact code (a data/edit error, or two shots that
    # both look plausible for one master code). Only one can win Tier 1; the loser must NOT also
    # claim it via a later tier for the SAME row.
    new_shots = [
        {"tcid": "aaa", "code": "SHW_0100_1_010", "order": 1},
        {"tcid": "bbb", "code": "SHW_0100_1_010", "order": 2},   # duplicate code (collision bait)
    ]
    master = [{"row": 1, "code": "SHW_0100_1_010", "revision": ""}]
    matches, leftovers, omit = M.assign(new_shots, master)
    M.uniqueness_check(matches)  # must not raise
    return matches, leftovers


matches, leftovers = test_uniqueness_exact_code_collision()
check("collision: exactly one match for the single master row", len(matches) == 1, matches)
check("collision: the loser becomes a leftover (NEW or EXTRA)", len(leftovers) == 1, leftovers)
check("collision: master row 1 claimed at most once", len([m for m in matches if m["master_row"] == 1]) == 1)


def test_uniqueness_visual_collision():
    # Two slate-less (placeholder) new-cut shots both score highest against the SAME master row
    # under a naive per-shot argmax visual match. The global greedy assignment must give the
    # master row to only one; per Geoff's noted 42-collision bug this is the exact failure mode
    # being guarded against.
    new_shots = [
        {"tcid": "p1", "code": "", "order": 1},
        {"tcid": "p2", "code": "", "order": 2},
    ]
    master = [
        {"row": 10, "code": "SHW_0200_1_010", "revision": ""},
        {"row": 11, "code": "SHW_0201_1_010", "revision": ""},
    ]

    def naive_argmax_sim(tcid, row):
        # both new shots "prefer" row 10 (score 0.9) over row 11 (score 0.1) -- a per-shot
        # argmax would send BOTH p1 and p2 to row 10, a collision. Global 1:1 must not.
        return 0.9 if row == 10 else 0.1

    matches, leftovers, omit = M.assign(new_shots, master, sim=naive_argmax_sim)
    M.uniqueness_check(matches)  # must not raise
    return matches, leftovers


matches2, leftovers2 = test_uniqueness_visual_collision()
check("visual collision: both master rows claimed at most once each",
      all(v <= 1 for v in __import__("collections").Counter(m["master_row"] for m in matches2).values()),
      matches2)
check("visual collision: p1 and p2 do not BOTH land on row 10",
      not (any(m["tcid"] == "p1" and m["master_row"] == 10 for m in matches2) and
           any(m["tcid"] == "p2" and m["master_row"] == 10 for m in matches2)),
      matches2)
check("visual collision: the higher-scoring shot wins row 10, the other gets row 11 or leftover",
      len(matches2) == 2 or (len(matches2) == 1 and len(leftovers2) == 1), (matches2, leftovers2))


# ================================================================================================
# 3. tier precedence - slate beats visual (a clear slate disagreement wins over look-alike)
# ================================================================================================
def test_slate_beats_visual():
    # new shot 'c' has ITS OWN slate/take/counter that matches master row 2 exactly (Tier 1).
    # Even if it would visually resemble master row 1 MORE (higher sim), the exact-code tier
    # runs first and claims row 2 for it -- Tier 4 (visual) never even considers 'c' because it
    # has a real slate (parse_code succeeds), so VISUAL is skipped for it entirely.
    new_shots = [{"tcid": "c", "code": "SHW_0300_1_020", "order": 1}]
    master = [
        {"row": 1, "code": "SHW_0999_1_999", "revision": ""},   # visually similar (per fake sim) but WRONG
        {"row": 2, "code": "SHW_0300_1_020", "revision": ""},   # exact code match -- correct
    ]

    def sim_prefers_wrong_row(tcid, row):
        return 0.99 if row == 1 else 0.01   # visual signal points at the WRONG shot

    matches, leftovers, omit = M.assign(new_shots, master, sim=sim_prefers_wrong_row)
    return matches


matches3 = test_slate_beats_visual()
check("slate beats visual: exact-code match wins despite a stronger (wrong) visual score",
      len(matches3) == 1 and matches3[0]["master_row"] == 2 and matches3[0]["tier"] == "EXACT-CODE",
      matches3)


def test_visual_only_for_slateless():
    # A slate-less (no parseable code) new shot legitimately uses VISUAL; a shot WITH a slate
    # that has no exact/ordinal master match still does NOT fall through to VISUAL (per the
    # tier-4 guard: "has its own slate -> do NOT visual-match"), even if visual sim exists.
    new_shots = [
        {"tcid": "placeholder", "code": "", "order": 1},               # no slate -> eligible for VISUAL
        {"tcid": "has_slate_no_master", "code": "SHW_0777_1_010", "order": 2},  # has slate, no master match
    ]
    master = [{"row": 5, "code": "", "revision": ""}]   # master with no code (placeholder-style), tcid n/a

    def sim(tcid, row):
        return 0.8

    matches, leftovers, omit = M.assign(new_shots, master, sim=sim)
    return matches, leftovers


matches4, leftovers4 = test_visual_only_for_slateless()
check("visual tier only matches the slate-less shot",
      any(m["tcid"] == "placeholder" for m in matches4), matches4)
check("shot with an unmatched slate is NOT force-matched via visual (stays leftover)",
      any(l["tcid"] == "has_slate_no_master" for l in leftovers4), leftovers4)


# ================================================================================================
# 4. ordinal counter-drift absorption - pairs pieces in counter order within a (slate,take) group,
#    never reordering by picture, and leaves surplus unmatched rather than forcing it
# ================================================================================================
def test_ordinal_counter_drift():
    # Same scenario as cut7_assign.py's own self-test: 3 new-cut pieces of slate 519/take 3 at
    # counters 20/30/40 (drifted from the master's 10/20/30) must pair up 1st->1st, 2nd->2nd,
    # 3rd->3rd IN COUNTER ORDER, i.e. by their own relative order, not by re-sorting to match
    # picture content. A 4th unrelated shot (879/01/080) exact-codes directly.
    new_shots = [
        {"tcid": "a", "code": "SHW_0519_3_020", "order": 1},
        {"tcid": "b", "code": "SHW_0519_3_030", "order": 2},
        {"tcid": "c", "code": "SHW_0879_1_080", "order": 3},
        {"tcid": "d", "code": "SHW_0519_3_040", "order": 4},
    ]
    master = [
        {"row": 1, "code": "SHW_0519_3_010", "revision": ""},
        {"row": 2, "code": "SHW_0519_3_020", "revision": ""},
        {"row": 3, "code": "SHW_0879_1_080", "revision": ""},
        {"row": 4, "code": "SHW_0519_3_030", "revision": ""},
    ]
    matches, leftovers, omit = M.assign(new_shots, master)
    return matches, leftovers, omit


matches5, leftovers5, omit5 = test_ordinal_counter_drift()
by_tcid = {m["tcid"]: m for m in matches5}
check("ordinal: 'c' exact-codes to row 3", by_tcid.get("c", {}).get("master_row") == 3, matches5)
check("ordinal: 'c' tier is EXACT-CODE", by_tcid.get("c", {}).get("tier") == "EXACT-CODE", matches5)
check("ordinal: 3 remaining 519/03 pieces (a,b,d) map onto rows 1,2,4 in counter order",
      {by_tcid.get(t, {}).get("master_row") for t in ("a", "b", "d")} == {1, 2, 4}, matches5)
# The 519/03 group has 3 new-cut pieces (a,b,d) and 3 master pieces (rows 1,2,4) -- an even
# match, so there is no surplus here; all 4 new-cut shots match, nothing is left over.
check("ordinal: all 4 new-cut shots matched, no leftovers in this even-count scenario",
      len(leftovers5) == 0, leftovers5)
check("ordinal: no omit candidates (all 4 master rows claimed)", omit5 == [], omit5)
M.uniqueness_check(matches5)  # must not raise
check("ordinal: uniqueness holds", True)


def test_ordinal_surplus_leftover():
    # Now genuinely oversupply the 519/03 group: 3 new-cut pieces but only 2 master pieces.
    # The surplus new-cut piece must be left over as EXTRA (a candidate additional piece of an
    # existing, already-represented slate), not forced onto an already-claimed row.
    new_shots = [
        {"tcid": "a", "code": "SHW_0519_3_010", "order": 1},
        {"tcid": "b", "code": "SHW_0519_3_020", "order": 2},
        {"tcid": "d", "code": "SHW_0519_3_030", "order": 3},   # surplus: 3rd piece, only 2 master slots
    ]
    master = [
        {"row": 1, "code": "SHW_0519_3_010", "revision": ""},
        {"row": 2, "code": "SHW_0519_3_020", "revision": ""},
    ]
    matches, leftovers, omit = M.assign(new_shots, master)
    return matches, leftovers, omit


matches5b, leftovers5b, omit5b = test_ordinal_surplus_leftover()
check("surplus: exactly one leftover (3 new pieces vs 2 master pieces)", len(leftovers5b) == 1, leftovers5b)
check("surplus: the leftover tier is EXTRA (slate has a master presence)",
      leftovers5b and leftovers5b[0]["tier"] == "EXTRA", leftovers5b)
check("surplus: the leftover is the highest-counter piece ('d'), a/b matched in order",
      leftovers5b and leftovers5b[0]["tcid"] == "d", leftovers5b)
check("surplus: no omit candidates (both master rows claimed)", omit5b == [], omit5b)


def test_ordinal_never_reorders_by_picture():
    # Even if a hypothetical visual signal would prefer pairing a's counter-20 piece with the
    # master's counter-30 piece, ordinal tier-2 pairs strictly by COUNTER ORDER within the
    # group, not by any similarity score. This test only exercises tiers 1-3 (sim=None), which
    # is exactly the point: ordinal alignment must not need or consult a similarity signal.
    new_shots = [
        {"tcid": "x1", "code": "SHW_0050_1_010", "order": 1},
        {"tcid": "x2", "code": "SHW_0050_1_030", "order": 2},   # counters drifted: 10,30 vs master 10,20
    ]
    master = [
        {"row": 1, "code": "SHW_0050_1_010", "revision": ""},
        {"row": 2, "code": "SHW_0050_1_020", "revision": ""},
    ]
    matches, leftovers, omit = M.assign(new_shots, master)
    return matches


matches6 = test_ordinal_never_reorders_by_picture()
by_tcid6 = {m["tcid"]: m for m in matches6}
check("ordinal drift: x1 (own counter 010, first in order) -> master row 1 (first in order)",
      by_tcid6.get("x1", {}).get("master_row") == 1, matches6)
check("ordinal drift: x2 (own counter 030, second in order) -> master row 2 (second in order)",
      by_tcid6.get("x2", {}).get("master_row") == 2, matches6)


# ================================================================================================
# 5. protected-shot omit exclusion - manual-add markers in revision text must NEVER be proposed
#    as omit candidates, even when no new-cut shot claims them
# ================================================================================================
def test_protected_omit_exclusion():
    new_shots = [{"tcid": "only_one", "code": "SHW_0001_1_010", "order": 1}]
    master = [
        {"row": 1, "code": "SHW_0001_1_010", "revision": ""},              # claimed -> not an omit candidate anyway
        {"row": 2, "code": "SHW_0002_1_010", "revision": "NEW Cut6 - DMP add"},  # protected, unclaimed
        {"row": 3, "code": "SHW_0003_1_010", "revision": "manual invisible vfx"},  # protected, unclaimed
        {"row": 4, "code": "SHW_0004_1_010", "revision": "added by operator, no slate"},  # protected, unclaimed
        {"row": 5, "code": "SHW_0005_1_010", "revision": ""},              # UNPROTECTED, unclaimed -> real omit candidate
    ]
    matches, leftovers, omit = M.assign(new_shots, master)
    return omit


omit6 = test_protected_omit_exclusion()
check("protected: rows 2,3,4 (manual-add markers) excluded from omit", all(r not in omit6 for r in (2, 3, 4)), omit6)
check("protected: row 5 (unprotected, unclaimed) IS an omit candidate", 5 in omit6, omit6)
check("protected: omit list is exactly [5]", omit6 == [5], omit6)


def test_protected_via_explicit_rows_arg():
    # protected_rows can also be passed explicitly (e.g. loaded from a JSON side-file), in
    # addition to text-marker detection.
    new_shots = []
    master = [{"row": 9, "code": "SHW_0009_1_010", "revision": ""}]
    matches, leftovers, omit = M.assign(new_shots, master, protected_rows={9})
    check("protected via explicit protected_rows arg", omit == [], omit)


test_protected_via_explicit_rows_arg()


# ================================================================================================
# 6. invisible-VFX audit truth table
# ================================================================================================
def test_invisible_vfx_truth_table():
    ocr_rows = {
        "t1": {"is_vfx": True, "note": "DMP"},      # OCR says VFX
        "t2": {"is_vfx": False, "note": ""},        # OCR says not VFX
        "t3": {"is_vfx": False, "note": ""},        # OCR says not VFX, but operator flags it (invisible VFX)
        "t4": {"is_vfx": True, "note": "wire rem"}, # OCR says VFX, operator does NOT flag it
    }
    sheet_flags = {
        "t1": True,    # confirmed both ways
        "t2": False,   # confirmed not-VFX both ways
        "t3": True,    # operator-only: invisible VFX (cam shake, no burn-in)
        "t4": False,   # OCR-only: QC gap the other direction
    }
    return M.invisible_vfx_audit(ocr_rows, sheet_flags)


result = test_invisible_vfx_truth_table()
check("truth table: t1 is_vfx True (OCR+operator agree)", result["is_vfx"]["t1"] is True)
check("truth table: t2 is_vfx False (both agree not-vfx)", result["is_vfx"]["t2"] is False)
check("truth table: t3 is_vfx True (operator-flagged despite no OCR note)", result["is_vfx"]["t3"] is True)
check("truth table: t4 is_vfx True (OCR note is ground truth even though operator didn't flag)",
      result["is_vfx"]["t4"] is True)
check("truth table: invisible_vfx list is exactly ['t3']", result["invisible_vfx"] == ["t3"], result["invisible_vfx"])
check("truth table: ocr_only list is exactly ['t4']", result["ocr_only"] == ["t4"], result["ocr_only"])
check("truth table: counts.total == 4", result["counts"]["total"] == 4, result["counts"])
check("truth table: counts.is_vfx == 3 (t1,t3,t4)", result["counts"]["is_vfx"] == 3, result["counts"])
check("truth table: counts.operator_only_invisible == 1", result["counts"]["operator_only_invisible"] == 1)
check("truth table: counts.ocr_only_unflagged == 1", result["counts"]["ocr_only_unflagged"] == 1)


def test_invisible_vfx_sheet_note_not_ground_truth():
    # The sheet's OWN note-text column must NEVER be treated as OCR ground truth, even if it
    # contains VFX-sounding text -- only the OCR export CSV's is_vfx column is ground truth.
    # This test simulates a sheet row with manual free text that looks like a note but the
    # audit function only ever consults the flag columns passed to it, not arbitrary note text,
    # so a sheet CSV with an unrelated 'note' column must not influence is_vfx.
    ocr_rows = {"t5": {"is_vfx": False, "note": ""}}
    sheet_flags = {"t5": False}   # operator did not flag, regardless of any note text elsewhere
    result = M.invisible_vfx_audit(ocr_rows, sheet_flags)
    check("sheet note text is not ground truth: t5 stays not-vfx", result["is_vfx"]["t5"] is False)


test_invisible_vfx_sheet_note_not_ground_truth()


# ================================================================================================
# 7. fps_mismatch guard
# ================================================================================================
check("fps_mismatch: 24 vs 23.976 within tolerance -> False", M.fps_mismatch(24.0, 23.976) is False)
check("fps_mismatch: 24 vs 25 -> True", M.fps_mismatch(24.0, 25.0) is True)
check("fps_mismatch: 24 vs 24 -> False", M.fps_mismatch(24.0, 24.0) is False)
check("fps_mismatch: None input -> None (unknown, not safe)", M.fps_mismatch(None, 24.0) is None)
check("fps_mismatch: custom tight tolerance flags 23.976 vs 24", M.fps_mismatch(23.976, 24.0, tol=0.001) is True)


# ================================================================================================
# 8. crosscheck_new_against_master - engine-missed-exact-code detection
# ================================================================================================
def test_crosscheck_engine_missed():
    # Construct a leftover whose own code DOES exist in the master list unclaimed (simulating a
    # bug where tier 1 should have caught it but didn't reach this point in a hand-built test).
    leftovers = [{"tcid": "z1", "tier": "NEW"}]
    new_by_tcid = {"z1": {"tcid": "z1", "code": "SHW_0042_1_010"}}
    master = [{"row": 1, "code": "SHW_0042_1_010", "revision": ""}]
    out = M.crosscheck_new_against_master(leftovers, new_by_tcid, master)
    check("crosscheck flags engine-missed-exact-code", out[0]["note"] == "engine-missed-exact-code", out)


test_crosscheck_engine_missed()


def test_crosscheck_genuinely_new():
    leftovers = [{"tcid": "z2", "tier": "NEW"}]
    new_by_tcid = {"z2": {"tcid": "z2", "code": "SHW_9999_1_010"}}
    master = [{"row": 1, "code": "SHW_0042_1_010", "revision": ""}]
    out = M.crosscheck_new_against_master(leftovers, new_by_tcid, master)
    check("crosscheck: no slate overlap -> genuinely-new", out[0]["note"] == "genuinely-new", out)


test_crosscheck_genuinely_new()


# ================================================================================================
# 9. FEATURE 3 -- review-gate pure logic: build_review_rows / plan_review_header_insert /
#    build_duplicate_cf_rule. No live Sheets connection -- write_match_proposal_to_sheet's I/O is
#    exercised only by construction here, not called (it needs a real `sheets` service object).
# ================================================================================================
def test_build_review_rows_matches_and_leftovers():
    matches = [{"tcid": "a", "master_row": 1, "master_code": "SHW_0100_1_010", "tier": "EXACT-CODE"}]
    leftovers = [{"tcid": "b", "tier": "NEW", "note": "genuinely-new"}]
    out = M.build_review_rows(matches, leftovers, {})
    check("review rows: match gets tier + proposed code, blank note",
          out["a"] == {"Match Tier": "EXACT-CODE", "Proposed Master Code": "SHW_0100_1_010",
                       "Match Note": ""}, out)
    check("review rows: leftover gets tier + note, blank proposed code",
          out["b"] == {"Match Tier": "NEW", "Proposed Master Code": "", "Match Note": "genuinely-new"}, out)


test_build_review_rows_matches_and_leftovers()


def test_plan_review_header_insert_all_missing():
    header = ["TC_ID", "Shot Code", "Status"]
    new_header, col = M.plan_review_header_insert(header)
    check("header insert: all 3 review headers appended at the right",
          new_header == ["TC_ID", "Shot Code", "Status"] + M.REVIEW_HEADERS, new_header)
    check("header insert: col map covers all 3, indices 3,4,5",
          [col[h] for h in M.REVIEW_HEADERS] == [3, 4, 5], col)


test_plan_review_header_insert_all_missing()


def test_plan_review_header_insert_idempotent():
    # Headers already present (e.g. a second run) must be reused at their EXISTING index, never
    # duplicated -- this is the idempotency guarantee --write-sheet promises on re-run.
    header = ["TC_ID", "Match Tier", "Proposed Master Code", "Match Note", "Status"]
    new_header, col = M.plan_review_header_insert(header)
    check("header insert idempotent: header row unchanged (no duplicates appended)",
          new_header == header, new_header)
    check("header insert idempotent: col map points at the EXISTING columns",
          (col["Match Tier"], col["Proposed Master Code"], col["Match Note"]) == (1, 2, 3), col)


test_plan_review_header_insert_idempotent()


def test_plan_review_header_insert_partial():
    # Only some review headers exist (e.g. an operator manually added "Match Note" already) ->
    # the missing ones are appended adjacent to each other, the existing one is reused in place.
    header = ["TC_ID", "Match Note"]
    new_header, col = M.plan_review_header_insert(header)
    check("header insert partial: Match Note kept at its own index",
          col["Match Note"] == 1, col)
    check("header insert partial: Match Tier + Proposed Master Code appended together at the right",
          new_header[2:] == ["Match Tier", "Proposed Master Code"], new_header)
    check("header insert partial: they are adjacent to each other",
          col["Proposed Master Code"] == col["Match Tier"] + 1, col)


test_plan_review_header_insert_partial()


def test_build_duplicate_cf_rule_shape():
    req = M.build_duplicate_cf_rule(sheet_id=42, col_index=4, num_rows=100, header_row_count=1)
    rule = req["addConditionalFormatRule"]["rule"]
    check("cf rule: targets the right sheet", rule["ranges"][0]["sheetId"] == 42, rule)
    check("cf rule: targets exactly column index 4",
          (rule["ranges"][0]["startColumnIndex"], rule["ranges"][0]["endColumnIndex"]) == (4, 5), rule)
    check("cf rule: starts after the header row", rule["ranges"][0]["startRowIndex"] == 1, rule)
    check("cf rule: is a CUSTOM_FORMULA boolean rule",
          rule["booleanRule"]["condition"]["type"] == "CUSTOM_FORMULA", rule)
    formula = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
    check("cf rule: formula references COUNTIF for duplicate detection", "COUNTIF" in formula, formula)
    check("cf rule: formula guards against blank cells counting as duplicates",
          '<>""' in formula, formula)
    check("cf rule: uses a red-ish background format",
          rule["booleanRule"]["format"]["backgroundColor"]["red"] == 1.0, rule)


test_build_duplicate_cf_rule_shape()


def test_write_sheet_full_pipeline_never_touches_other_columns():
    # End-to-end pure-logic check (no live Sheets connection): build_review_rows +
    # plan_review_header_insert together must only ever plan writes into the 3 review columns,
    # never into any pre-existing column (the "never touches any other column" contract).
    header = ["TC_ID", "Shot Code", "Status", "Cost"]
    new_header, col = M.plan_review_header_insert(header)
    matches = [{"tcid": "a", "master_row": 1, "master_code": "SHW_0100_1_010", "tier": "EXACT-CODE"}]
    review = M.build_review_rows(matches, [], {})
    touched_cols = {col[h] for h in M.REVIEW_HEADERS}
    protected_cols = {0, 1, 2, 3}   # TC_ID, Shot Code, Status, Cost
    check("write-sheet: review columns never overlap pre-existing columns",
          touched_cols.isdisjoint(protected_cols), (touched_cols, protected_cols))
    check("write-sheet: review dict only has the 3 documented keys",
          set(review["a"].keys()) == set(M.REVIEW_HEADERS), review)


test_write_sheet_full_pipeline_never_touches_other_columns()


# ================================================================================================
# 10. FEATURE 4 -- tidy change-report (build_change_report / format_change_summary) on synthetic
#     in-memory cut lists (no cv2/numpy/real footage needed -- classify()-shaped rows are hand-built)
# ================================================================================================
def test_build_change_report_verdict_mapping():
    # one row of every classify() status, hand-built (this is the synthetic "two in-memory cut
    # lists" fixture -- classify()'s row tuple shape is (status, A_tcid, B_tcid, hamming,
    # len_delta_frames, method), constructed directly rather than run through cv2 hashing).
    rows = [
        ("SAME", "a1", "b1", 3, 0, "phash"),
        ("RETIMED", "a2", "b2", 5, 6, "phash"),
        ("MOVED", "a3", "b4", 2, None, "phash"),
        ("CHANGED", "a4", "b3", 30, None, "phash"),
        ("REVIEW", "a5", "b5", 50, None, "color"),
        ("ADDED", "", "b6", "", None, ""),
        ("REMOVED", "a6", "", "", None, ""),
    ]
    report = M.build_change_report(rows)
    by_old = {(r["old_code"], r["new_code"]): r for r in report}
    check("change report: SAME -> SAME", by_old[("a1", "b1")]["verdict"] == "SAME")
    check("change report: RETIMED -> CHANGED", by_old[("a2", "b2")]["verdict"] == "CHANGED")
    check("change report: MOVED -> SHIFTED", by_old[("a3", "b4")]["verdict"] == "SHIFTED")
    check("change report: CHANGED -> CHANGED", by_old[("a4", "b3")]["verdict"] == "CHANGED")
    check("change report: REVIEW -> CHANGED", by_old[("a5", "b5")]["verdict"] == "CHANGED")
    check("change report: ADDED -> ADDED, old_code blank", by_old[("", "b6")]["verdict"] == "ADDED")
    check("change report: REMOVED -> REMOVED, new_code blank", by_old[("a6", "")]["verdict"] == "REMOVED")
    check("change report: row count matches input", len(report) == len(rows), report)


test_build_change_report_verdict_mapping()


def test_build_change_report_durations():
    rows = [("RETIMED", "a1", "b1", 5, 6, "phash")]   # b1 is 6 frames LONGER than a1
    len_a = {"a1": 48}
    len_b = {"b1": 54}
    report = M.build_change_report(rows, len_a, len_b)
    r = report[0]
    check("change report: old_dur from lookup", r["old_dur"] == 48, r)
    check("change report: new_dur from lookup", r["new_dur"] == 54, r)
    check("change report: dur_delta matches the classify()-provided delta", r["dur_delta"] == 6, r)


test_build_change_report_durations()


def test_build_change_report_duration_fallback_from_delta():
    # When only ONE side's length is known but classify() gave a delta, the other side's
    # duration can be derived (new = old + delta / old = new - delta) rather than left blank.
    rows = [("RETIMED", "a1", "b1", 5, 4, "phash")]
    report = M.build_change_report(rows, len_by_tcid_a={"a1": 20}, len_by_tcid_b={})
    check("change report: new_dur derived from old_dur + delta when B length unknown",
          report[0]["new_dur"] == 24, report[0])


test_build_change_report_duration_fallback_from_delta()


def test_build_change_report_no_lengths_no_crash():
    # ADDED/REMOVED rows with no length lookups at all -> durations stay blank, no exception.
    rows = [("ADDED", "", "b1", "", None, ""), ("REMOVED", "a1", "", "", None, "")]
    report = M.build_change_report(rows)
    check("change report: no lengths -> old_dur/new_dur blank, not crashed",
          all(r["old_dur"] == "" and r["new_dur"] == "" for r in report), report)


test_build_change_report_no_lengths_no_crash()


def test_format_change_summary_shape():
    rows = [
        ("SAME", "a1", "b1", 3, 0, "phash"),
        ("SAME", "a2", "b2", 2, 0, "phash"),
        ("CHANGED", "a3", "b3", 30, 5, "phash"),
        ("ADDED", "", "b4", "", None, ""),
        ("REMOVED", "a5", "", "", None, ""),
    ]
    report = M.build_change_report(rows)
    summary = M.format_change_summary(report)
    check("summary: mentions SAME with count 2", "SAME" in summary and "2" in summary, summary)
    check("summary: mentions CHANGED", "CHANGED" in summary, summary)
    check("summary: mentions ADDED", "ADDED" in summary, summary)
    check("summary: mentions REMOVED", "REMOVED" in summary, summary)
    check("summary: mentions TOTAL", "TOTAL" in summary, summary)
    check("summary: is a single string (one-screen), not a list", isinstance(summary, str), type(summary))


test_format_change_summary_shape()


def test_build_change_report_two_synthetic_cut_lists():
    # Full pipeline on two synthetic in-memory "cut lists" (A = old cut, B = new cut), driven
    # through classify()'s OWN status vocabulary by hand-building the alignment result instead of
    # real footage -- proves build_change_report + format_change_summary work end to end on
    # exactly the kind of data compare's real cv2 pipeline would eventually produce.
    cut_a = [{"tcid": "0001", "length": 24}, {"tcid": "0002", "length": 48},
             {"tcid": "0003", "length": 12}, {"tcid": "0004", "length": 36}]
    cut_b = [{"tcid": "1001", "length": 24}, {"tcid": "1002", "length": 60},
             {"tcid": "1003", "length": 36}, {"tcid": "1004", "length": 18}]
    # hand-authored classify()-shaped verdicts: 0001->1001 SAME, 0002->1002 RETIMED (+12),
    # 0003 REMOVED (dropped from the new cut), 1003 ADDED (new shot), 0004->1004 CHANGED (recut).
    rows = [
        ("SAME", "0001", "1001", 4, 0, "phash"),
        ("RETIMED", "0002", "1002", 6, 12, "phash"),
        ("REMOVED", "0003", "", "", None, ""),
        ("ADDED", "", "1003", "", None, ""),
        ("CHANGED", "0004", "1004", 35, None, "phash"),
    ]
    len_a = {s["tcid"]: s["length"] for s in cut_a}
    len_b = {s["tcid"]: s["length"] for s in cut_b}
    report = M.build_change_report(rows, len_a, len_b)
    by_old = {(r["old_code"], r["new_code"]): r for r in report}
    check("synthetic cuts: SAME shot durations both 24", by_old[("0001", "1001")]["old_dur"] == 24
          and by_old[("0001", "1001")]["new_dur"] == 24, by_old[("0001", "1001")])
    check("synthetic cuts: RETIMED verdict is CHANGED with +12 delta",
          by_old[("0002", "1002")]["verdict"] == "CHANGED" and by_old[("0002", "1002")]["dur_delta"] == 12,
          by_old[("0002", "1002")])
    check("synthetic cuts: REMOVED shot has blank new_code",
          by_old[("0003", "")]["new_code"] == "" and by_old[("0003", "")]["verdict"] == "REMOVED")
    check("synthetic cuts: ADDED shot has blank old_code",
          by_old[("", "1003")]["old_code"] == "" and by_old[("", "1003")]["verdict"] == "ADDED")
    check("synthetic cuts: CHANGED shot durations both resolved from lookup",
          by_old[("0004", "1004")]["old_dur"] == 36 and by_old[("0004", "1004")]["new_dur"] == 18,
          by_old[("0004", "1004")])
    summary = M.format_change_summary(report)
    check("synthetic cuts: summary table produced without error", "TOTAL" in summary and "5" in summary, summary)


test_build_change_report_two_synthetic_cut_lists()


print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
