#!/usr/bin/env python
"""bs_ocr pure-logic tests - stdlib only, no network/ffmpeg/OCR-engine required.

Run:  python tests/test_ocr.py         (from the breakdown_studio folder)
Exit 0 = all pass. Covers the slate grammar (parse_slate/parse_take), the 3-frame VFX-note
consistency gate, and the tc-offset constancy math -- the parts of bs_ocr.py that are pure
functions and don't need a real OCR backend or frame images.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_ocr as O  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


# =============================================================================================
# parse_take: roman-numeral takes, tolerant of OCR confusions
# =============================================================================================

check("parse_take 'I' -> I/1", O.parse_take("I") == ("I", 1), O.parse_take("I"))
check("parse_take 'II' -> II/2", O.parse_take("II") == ("II", 2), O.parse_take("II"))
check("parse_take 'III' -> III/3", O.parse_take("III") == ("III", 3), O.parse_take("III"))
check("parse_take 'IV' -> IV/4", O.parse_take("IV") == ("IV", 4), O.parse_take("IV"))

# OCR-mangled forms: lowercase L / pipe / bang / '1' all stand in for 'I'
check("parse_take 'l' -> I/1 (OCR lowercase-L)", O.parse_take("l") == ("I", 1), O.parse_take("l"))
check("parse_take 'll' -> II/2 (OCR lowercase-LL)", O.parse_take("ll") == ("II", 2), O.parse_take("ll"))
check("parse_take 'lll' -> III/3 (OCR lowercase-LLL)", O.parse_take("lll") == ("III", 3), O.parse_take("lll"))
check("parse_take '1' -> I/1 (OCR digit-1)", O.parse_take("1") == ("I", 1), O.parse_take("1"))
check("parse_take '11' -> II/2 (OCR digit-11)", O.parse_take("11") == ("II", 2), O.parse_take("11"))
check("parse_take '1V' -> IV/4 (OCR digit-1 + V)", O.parse_take("1V") == ("IV", 4), O.parse_take("1V"))
check("parse_take '|' -> I/1 (OCR pipe)", O.parse_take("|") == ("I", 1), O.parse_take("|"))
check("parse_take '!' -> I/1 (OCR bang)", O.parse_take("!") == ("I", 1), O.parse_take("!"))

# take followed by trailing tail text (camera/MOS notes) -- only the first token is the take
check("parse_take 'III A-CAM' -> III/3 (ignores tail)", O.parse_take("III A-CAM") == ("III", 3), O.parse_take("III A-CAM"))

# no take present
check("parse_take '' -> ('',0)", O.parse_take("") == ("", 0), O.parse_take(""))
check("parse_take 'MOS' -> ('',0) (not a roman numeral)", O.parse_take("MOS") == ("", 0), O.parse_take("MOS"))


# =============================================================================================
# parse_slate: scene / slate / take / extra grammar
# =============================================================================================

# basic triplet
sc, sl, tr, ex = O.parse_slate("12/535/I")
check("parse_slate basic scene/slate/take", (sc, sl, tr) == ("12", "535", "I"), (sc, sl, tr, ex))

# no '/' at all -> unreadable / filler (slate is None)
sc, sl, tr, ex = O.parse_slate("garbage no slashes here")
check("parse_slate no-slash -> slate=None", sl is None, (sc, sl, tr, ex))

# empty / falsy raw
sc, sl, tr, ex = O.parse_slate("")
check("parse_slate empty -> slate=None", sl is None, (sc, sl, tr, ex))
sc, sl, tr, ex = O.parse_slate(None)
check("parse_slate None -> slate=None", sl is None, (sc, sl, tr, ex))

# "pt" = PART -- a field containing 'pt' is a scene ref, never the slate
sc, sl, tr, ex = O.parse_slate("79pt 1/535/I")
check("parse_slate 'pt' field is scene not slate", sl == "535", (sc, sl, tr, ex))
check("parse_slate 'pt' field kept as scene text", "79pt" in sc, sc)
# the naive-parser failure mode this guards against: naive field[1] would misread "79pt 1" itself
# as if IT were the slate-ish field; here the real slate field (535) must still win.
check("parse_slate 'pt' does not leak digits into slate ('791')", sl != "791", sl)

# "+" joins scenes -- composite scene ref field also must not be treated as slate
sc, sl, tr, ex = O.parse_slate("75pt +79pt 1/535/I")
check("parse_slate '+' join scene, slate=535", sl == "535", (sc, sl, tr, ex))
check("parse_slate '+' kept in scene field", "+" in sc, sc)

# 5000-series slates are REAL slates (not rejected for being 4-digit / high-range)
sc, sl, tr, ex = O.parse_slate("7/5001/III")
check("parse_slate 5000-series is a real slate", sl == "5001", (sc, sl, tr, ex))
check("parse_slate 5000-series take parses", tr == "III", (sc, sl, tr, ex))

# 6000 slate (also called out as real in the grammar)
sc, sl, tr, ex = O.parse_slate("3/6000/I")
check("parse_slate 6000 slate is real", sl == "6000", (sc, sl, tr, ex))

# leading-zero slate normalizes (045 -> 45) same as a bare 45
sc, sl, tr, ex = O.parse_slate("12/045/I")
check("parse_slate leading-zero slate normalizes", sl == "45", (sc, sl, tr, ex))

# roman-numeral OCR confusions flow through parse_slate's take extraction too
sc, sl, tr, ex = O.parse_slate("12/535/lll")
check("parse_slate take 'lll' -> III through full parse", tr == "III", (sc, sl, tr, ex))
sc, sl, tr, ex = O.parse_slate("12/535/ll A-CAM")
check("parse_slate take 'll' -> II + tail kept in extra", tr == "II" and "A-CAM" in ex, (sc, sl, tr, ex))

# non-slate tokens: stock filenames, camera rolls, placeholders, end-markers -> slate=None so
# callers fall back to visual matching instead of trusting a fabricated code
for bad_raw in (
    "Snow-053/stock/footage",
    "A040C012_260130/camera-roll/take2",
    "dreamina-2026-04-13/placeholder/gen",
    "SLUTSCEN/end/of film",
    "work in progress/edit/v3",
    "render_v012.mov/comp/notes",
):
    sc, sl, tr, ex = O.parse_slate(bad_raw)
    check(f"parse_slate non-slate token -> None ({bad_raw!r})", sl is None, (sc, sl, tr, ex))

# free text with a '/' but no clean numeric field anywhere -> still None, not a fabricated slate
sc, sl, tr, ex = O.parse_slate("some words / more words / and more")
check("parse_slate free text (no numeric field) -> None", sl is None, (sc, sl, tr, ex))
check("parse_slate free text yields no spurious take", tr == "", (sc, sl, tr, ex))

# --- OCR-noise patterns from the production regression corpus (generic, no client data) --------

# 1. slate+take sharing a field: "56 I" -> slate=56; the take from the FOLLOWING extra field wins
sc, sl, tr, ex = O.parse_slate("3pt3/56 I/ll*")
check("parse_slate shared field '56 I' -> slate 56", sl == "56", (sc, sl, tr, ex))
check("parse_slate shared field: extra-field take 'll*' wins -> II", tr == "II", (sc, sl, tr, ex))

# in-field take is the fallback when no extra field follows
sc, sl, tr, ex = O.parse_slate("3pt3/56 I")
check("parse_slate shared field, no extra -> in-field take I", sl == "56" and tr == "I",
      (sc, sl, tr, ex))

# 2. trailing asterisk/punctuation noise on the take token
check("parse_take 'V*' -> V/5", O.parse_take("V*") == ("V", 5), O.parse_take("V*"))
check("parse_take 'll*' -> II/2", O.parse_take("ll*") == ("II", 2), O.parse_take("ll*"))
check("parse_take 'Vl*' -> VI/6", O.parse_take("Vl*") == ("VI", 6), O.parse_take("Vl*"))
check("parse_take 'I.' -> I/1", O.parse_take("I.") == ("I", 1), O.parse_take("I."))
check("parse_take 'II,' -> II/2", O.parse_take("II,") == ("II", 2), O.parse_take("II,"))
check("parse_take lowercase 'i' -> I/1", O.parse_take("i") == ("I", 1), O.parse_take("i"))
sc, sl, tr, ex = O.parse_slate("7pt2/503o/i")
check("parse_slate lowercase take 'i' through full parse -> I", tr == "I", (sc, sl, tr, ex))

# 3. take-only reads (composite scene join, no numeric slate): slate=None but take still parses
sc, sl, tr, ex = O.parse_slate("5+6pt2/I/V*")
check("parse_slate take-only: slate stays None", sl is None, (sc, sl, tr, ex))
check("parse_slate take-only: take = last roman field (V)", tr == "V", (sc, sl, tr, ex))
sc, sl, tr, ex = O.parse_slate("5+6pt2/I/Vl*")
check("parse_slate take-only: 'Vl*' -> VI", sl is None and tr == "VI", (sc, sl, tr, ex))

# 4. trailing non-digit noise on the slate token: strip letters, keep digits
sc, sl, tr, ex = O.parse_slate("7pt2/503o/i")
check("parse_slate noisy slate '503o' -> 503", sl == "503", (sc, sl, tr, ex))
# ambiguous mixed token: production stripped ALL letters ("1O0" -> 10), no O->0 substitution
sc, sl, tr, ex = O.parse_slate("6/1O0/l*")
check("parse_slate noisy slate '1O0' -> 10 (letters stripped, NOT O->0)", sl == "10",
      (sc, sl, tr, ex))
check("parse_slate noisy slate '1O0' take 'l*' -> I", tr == "I", (sc, sl, tr, ex))
# mixed-token guards: free-text-ish tokens must not become slates
sc, sl, tr, ex = O.parse_slate("12/v2/notes")
check("parse_slate 'v2' (1 digit) not a slate", sl is None, (sc, sl, tr, ex))
sc, sl, tr, ex = O.parse_slate("12/MOS/x")
check("parse_slate 'MOS' (no digits) not a slate", sl is None, (sc, sl, tr, ex))


# =============================================================================================
# OCR-confusable substitution fallback (last-resort char map: s/S->5, o/O->0, &->8, l/I/|->1,
# B->8, g/q->9, Z->2, e->6) -- only kicks in once pure-digits and letter-strip both fail.
# =============================================================================================

# live-OCR regression case 1: slate token OCR'd as almost-all-letters ("sso" -> 550, s->5 o->0);
# take comes from the following field ("ll*" -> II).
sc, sl, tr, ex = O.parse_slate("2+3pt2/sso/ll*")
check("parse_slate confusable-sub 'sso' -> slate 550", sl == "550", (sc, sl, tr, ex))
check("parse_slate confusable-sub 'sso' -> take II", tr == "II", (sc, sl, tr, ex))

# live-OCR regression case 2: slate token has an OCR ampersand glyph ("55&" -> 558, &->8); the
# following field "11 B-CAM*" is the TAKE field (digit-form roman "11" -> II), and must NOT be
# grabbed as the slate now that the true slate field ("55&") is parseable.
sc, sl, tr, ex = O.parse_slate("2+3pt2/55&/11 B-CAM*")
check("parse_slate confusable-sub '55&' -> slate 558", sl == "558", (sc, sl, tr, ex))
check("parse_slate confusable-sub '55&': take from '11' -> II, not grabbed as slate",
      tr == "II" and sl != "11", (sc, sl, tr, ex))
check("parse_slate confusable-sub '55&': tail camera tag kept in extra", "B-CAM" in ex, ex)

# "1O0" -> 10 regression stays intact: letter-strip (rule b) must still win over confusable
# substitution (rule c) so this doesn't flip to a fabricated "100" (1->1, O->0, 0->0).
sc, sl, tr, ex = O.parse_slate("6/1O0/l*")
check("parse_slate '1O0' letter-strip still wins over confusable-sub (-> 10, not 100)",
      sl == "10", (sc, sl, tr, ex))

# a genuine non-slate token ("stock") must still return None even with the substitution fallback
# active -- "stock" has no digit-plausible substitution (s/S->5, o/O->0 leaves 5t0ck, not numeric).
sc, sl, tr, ex = O.parse_slate("12/stock/footage")
check("parse_slate non-slate 'stock' token still -> None with confusable-sub active",
      sl is None, (sc, sl, tr, ex))

# take-only fields must not be swallowed by the substitution fallback: a token that is itself a
# valid (possibly OCR-mangled) roman numeral is a take, not a misread slate ("I" must not become
# "1" via l/I->1 substitution).
sc, sl, tr, ex = O.parse_slate("5+6pt2/I/V*")
check("parse_slate take-only 'I' field not swallowed by confusable-sub (slate stays None)",
      sl is None and tr == "V", (sc, sl, tr, ex))


# =============================================================================================
# clean_slate_diff: OCR-noise guard for the boundary-QC slate oracle
# =============================================================================================

check("clean_slate_diff identical -> False", O.clean_slate_diff("535", "535") is False)
check("clean_slate_diff empty either side -> False", O.clean_slate_diff("", "535") is False)
check("clean_slate_diff substring (dropped digit) -> False (OCR noise)",
      O.clean_slate_diff("53", "535") is False)
check("clean_slate_diff off-by-one -> False (OCR noise)", O.clean_slate_diff("535", "536") is False)
check("clean_slate_diff genuinely different slates -> True",
      O.clean_slate_diff("535", "629") is True)


# =============================================================================================
# note_consistency_tier: 3-frame VFX-note bleed guard
# =============================================================================================

# ALL3: note present start+mid+end -> confident real VFX shot
tier, is_vfx, note, present, n = O.note_consistency_tier(
    {"start": "SPARKS ENHANCE", "mid": "SPARKS ENHANCE", "end": "SPARKS ENHANCE"})
check("note tier ALL3", tier == "ALL3" and is_vfx == 1, (tier, is_vfx, note))

# MIDEND: mid+end present, start blank (slate/handles trimmed the start frame) -> still real
tier, is_vfx, note, present, n = O.note_consistency_tier(
    {"start": "", "mid": "SKY REPLACE", "end": "SKY REPLACE"})
check("note tier MIDEND", tier == "MIDEND" and is_vfx == 1, (tier, is_vfx, note))

# PARTIAL: only start present -> bleed from previous shot's tail, NOT counted as this shot's VFX
tier, is_vfx, note, present, n = O.note_consistency_tier(
    {"start": "PREVIOUS SHOT NOTE", "mid": "", "end": ""})
check("note tier PARTIAL (start-only = bleed) -> not vfx", tier == "PARTIAL" and is_vfx == 0,
      (tier, is_vfx, note))

# PARTIAL: start+mid present, end blank -> still partial (not the ALL3/MIDEND shapes)
tier, is_vfx, note, present, n = O.note_consistency_tier(
    {"start": "NOTE A", "mid": "NOTE A", "end": ""})
check("note tier PARTIAL (start+mid, no end)", tier == "PARTIAL" and is_vfx == 0, (tier, is_vfx, note))

# NONE: no note anywhere -> not a VFX shot
tier, is_vfx, note, present, n = O.note_consistency_tier({"start": "", "mid": "", "end": ""})
check("note tier NONE", tier == "NONE" and is_vfx == 0 and note == "", (tier, is_vfx, note))

# consensus picks the longest raw variant of the modal normalized group (least-truncated OCR)
tier, is_vfx, note, present, n = O.note_consistency_tier(
    {"start": "SPARKS ENHANC", "mid": "SPARKS ENHANCE", "end": "SPARKS ENHANCE."})
check("note consensus prefers longest modal variant", note in ("SPARKS ENHANCE", "SPARKS ENHANCE."),
      note)


# =============================================================================================
# tc-offset constancy math
# =============================================================================================

check("offset_is_constant: identical offsets -> True",
      O.offset_is_constant([100, 100, 100]) is True)
check("offset_is_constant: within 1-frame tolerance -> True",
      O.offset_is_constant([100, 101, 100]) is True)
check("offset_is_constant: spread of 2 frames -> False (default tolerance=1)",
      O.offset_is_constant([100, 102, 100]) is False)
check("offset_is_constant: empty list -> False", O.offset_is_constant([]) is False)
check("offset_is_constant: custom tolerance widens acceptance",
      O.offset_is_constant([100, 105, 100], tolerance_frames=5) is True)
check("offset_is_constant: drifting series -> False (fps-mismatch signature)",
      O.offset_is_constant([100, 110, 120, 130]) is False)

# parse_ocr_tc: pulling HHMMSSFF out of noisy OCR text
check("parse_ocr_tc clean 8-digit reads back to frame count",
      O.parse_ocr_tc("01:02:45:12", 24.0) == O._tc_frames(1, 2, 45, 12, 24.0),
      O.parse_ocr_tc("01:02:45:12", 24.0))
check("parse_ocr_tc wrong digit count -> None", O.parse_ocr_tc("12:34:56", 24.0) is None)
check("parse_ocr_tc frame field >= fps -> None (invalid)",
      O.parse_ocr_tc("00:00:00:30", 24.0) is None)
check("parse_ocr_tc seconds field > 59 -> None (invalid)",
      O.parse_ocr_tc("00:00:75:00", 24.0) is None)
check("parse_ocr_tc empty text -> None", O.parse_ocr_tc("", 24.0) is None)

# frames_to_tc / tc_frames round-trip
rt = O._frames_to_tc(O._tc_frames(1, 2, 3, 4, 24.0), 24.0)
check("_tc_frames/_frames_to_tc round-trip", rt == "01:02:03:04", rt)


# =============================================================================================
# build_official_code
# =============================================================================================

check("build_official_code basic", O.build_official_code("535", 1, 2, "SHW") == "SHW_0535_01_020",
      O.build_official_code("535", 1, 2, "SHW"))
check("build_official_code non-numeric slate -> ''", O.build_official_code("N/A", 1, 1, "SHW") == "",
      O.build_official_code("N/A", 1, 1, "SHW"))
check("build_official_code zero-pads slate to 4 digits",
      O.build_official_code("7", 3, 1, "SHW") == "SHW_0007_03_010",
      O.build_official_code("7", 3, 1, "SHW"))


print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
