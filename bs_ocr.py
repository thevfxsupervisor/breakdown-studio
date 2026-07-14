#!/usr/bin/env python
"""bs_ocr.py - burn-in OCR + boundary-QC module for Breakdown Studio.

Generalized from a real production pipeline's slate/VFX-note/show-TC burn-in readers. Reads the
same frames/ layout bs_worker.py produces (frames/<PREFIX>_<tcid>-{start,mid,end}.jpg) and follows
its PROGRESS/LOG line protocol so the GUI can drive a progress bar. Self-contained: depends only on
Pillow + numpy + an OCR backend (EasyOCR by default; see _get_ocr_reader). No network, no
Google/ShotGrid coupling.

Four independent stages, each writing one artifact next to the frames dir and returning a summary
dict (also usable as a library from other tools):

  ocr_slate_frames(frames_dir, shots, cfg)  -> slate_ocr.csv       top-left slate burn-in -> scene/
                                                                    slate/take/official code
  ocr_vfx_notes(frames_dir, shots, cfg)     -> vfxnote_ocr.csv     bottom-left VFX editorial note,
                                                                    3-frame consistency gate
  probe_tc_offset(frames_dir, shots, cfg)   -> tc_offset.txt       top-right show-TC burn-in vs
                                                                    file-relative TC -> constant offset
  boundary_qc(frames_dir, shots, cfg, slate_csv=None) -> boundary_qc.csv
                                                                    slate-oracle QC of detected cuts
                                                                    (MERGE / SPLIT? candidates)

CLI:
  python bs_ocr.py slate      --frames-dir X --scenes-csv Y [--config config.json]
  python bs_ocr.py notes      --frames-dir X --scenes-csv Y [--config config.json]
  python bs_ocr.py tcoffset   --frames-dir X --scenes-csv Y [--config config.json]
  python bs_ocr.py boundaryqc --frames-dir X --scenes-csv Y [--config config.json] [--slate-csv slate_ocr.csv]
  python bs_ocr.py probecrops --movie X --frame N [--out Y.jpg] [--config config.json]

Each of the first four stages prints 'PROGRESS <stage> done/total' lines (matching
bs_worker.py) and a final 'DONE <stage>' line. probecrops is a one-shot visual check (see
below) and prints the boxes it drew instead.

probecrops verifies the configured OCR crop boxes BEFORE trusting a real OCR pass: it grabs
one frame straight from the movie and draws the slate/note/showtc crop rectangles on top of
it, so a producer can open the jpg and see at a glance whether the boxes actually hug the
burn-ins (see UX_PLAN.md P6 -- this replaces blind pixel-typing).
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from statistics import median

# bs_worker.py lives next to this file; reuse its scene/tcid helpers so both modules agree on the
# frame-index math and the frames/ naming convention.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bs_worker as W  # noqa: E402


# =============================================================================================
# Config / crop regions
# =============================================================================================
#
# Crop regions are ALWAYS config-driven -- never hardcode a crop tuned for one show's burn-in
# layout. Supply them via config.json:
#
#   "ocr_crops": {
#     "slate":  [0, 82, 660, 128],     # top-left slate burn-in band (scene/slate/take)
#     "note":   [0, 940, 960, 1080],   # bottom-left VFX editorial-note band (letterbox area)
#     "showtc": [1560, 55, 1920, 135]  # top-right burned-in show timecode
#   }
#
# The numbers above are EXAMPLES from the source production's 1920x1080 burn-in layout -- they are
# NOT safe defaults for a new show. Always verify crops against an actual frame first (crop, look,
# adjust) before trusting an OCR pass; a wrong crop silently reads garbage or a neighbouring field.
#
# Default crops here are deliberately full-frame-ish placeholders so a misconfigured run OCRs
# something (and probably nothing useful) rather than crashing; treat any output from the defaults
# as untrustworthy until real crops are set in config.
DEFAULT_CROPS = {
    "slate": [0, 0, 660, 140],
    "note": [0, 940, 960, 1080],
    "showtc": [1560, 0, 1920, 140],
}

DEFAULT_PREFIX = "SHW"


def load_config(config_path):
    cfg = {}
    if config_path and Path(config_path).exists():
        cfg.update(json.loads(Path(config_path).read_text(encoding="utf-8")))
    # env overrides win, matching bs_worker's BS_* convention
    if os.environ.get("BS_OCR_CROPS"):
        try:
            cfg["ocr_crops"] = json.loads(os.environ["BS_OCR_CROPS"])
        except Exception:
            pass
    if os.environ.get("BS_PREFIX"):
        cfg["prefix"] = os.environ["BS_PREFIX"]
    return cfg


def get_crop(cfg, name):
    crops = cfg.get("ocr_crops", {}) or {}
    box = crops.get(name, DEFAULT_CROPS[name])
    return tuple(int(v) for v in box)


def get_prefix(cfg):
    return cfg.get("prefix") or os.environ.get("BS_PREFIX") or DEFAULT_PREFIX


def get_fps(cfg):
    try:
        return float(cfg.get("fps") or 24.0)
    except (TypeError, ValueError):
        return 24.0


# =============================================================================================
# OCR backend
# =============================================================================================

_ocr_reader = None  # module-level model cache; constructing it is heavy


def _get_ocr_reader():
    """Lazily construct the OCR engine. EasyOCR by default (matches the source pipeline); CPU-only
    since these are small static crops and GPU driver support varies across workstations.

    Swap engines by setting cfg["ocr_backend"] = "easyocr" (default) and extending this function --
    kept as a single seam so a future Tesseract/other backend only needs one change.
    """
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    try:
        import easyocr
        import warnings
        warnings.filterwarnings("ignore")
    except ImportError as e:
        raise SystemExit(
            "bs_ocr requires the 'easyocr' package for burn-in OCR.\n"
            "Install it in the worker environment with:\n"
            "    pip install easyocr\n"
            f"(import failed: {e})"
        )
    _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _ocr_reader


def _upscale(im, factor=2):
    """Hi-res upscale of a small burn-in crop before OCR -- burn-in text is often only ~20-30px
    tall, and EasyOCR (like most OCR engines) reads small text more reliably after 2x-3x nearest/
    bicubic upscale. Source scripts crop-then-OCR at native res for larger crops; this helper is
    opt-in for callers/configs that want it (cfg["ocr_upscale"])."""
    from PIL import Image
    w, h = im.size
    return im.resize((w * factor, h * factor), Image.LANCZOS)


def ocr_crop(reader, image_path, crop, upscale=1):
    """OCR one crop region of one frame image. Returns '' if the frame is missing or unreadable."""
    if not os.path.exists(image_path):
        return ""
    from PIL import Image
    import numpy as np
    try:
        im = Image.open(image_path).convert("RGB").crop(crop)
    except Exception:
        return ""
    if upscale and upscale > 1:
        im = _upscale(im, upscale)
    results = reader.readtext(np.array(im), detail=0)
    return " ".join(results).strip()


# =============================================================================================
# Slate grammar: parse_slate() / parse_take()
# =============================================================================================
#
# Burn-in format (top-left slate band): "<scene> / <slate> / <extra>"
#
# Grammar rules (learned from a real production's operator conventions -- generalize, don't assume
# every show follows every rule, but this is the tolerant superset a slate parser needs):
#
#   - Fields are '/'-separated. Fewer than 2 '/'-separated fields with a numeric slate -> unreadable
#     (no '/' at all means OCR caught filler/noise, not a real burn-in).
#   - "pt" = PART of a scene, e.g. "79pt 1" = scene 79, part 1. A field containing "pt" is a SCENE
#     reference, never the slate.
#   - "+" JOINS scenes into a composite, e.g. "75pt +79pt 1" = scene 75-part + scene 79-part-1. Like
#     "pt", a field containing "+" is a scene reference, never the slate.
#   - The SLATE is the first standalone clean numeric field (2-5 digits, no pt/+/letters) -- i.e. the
#     smart parser SKIPS pt/+ fields when hunting for the slate instead of naively taking field[1].
#     This matters: a naive parser mis-reads "79pt 1" as slate "791".
#   - 5000-series (5001-5068ish) and similar 4-digit high-range slates are REAL slates (VFX/insert
#     slates), not garbage -- don't reject purely on digit count.
#   - TAKE is a roman numeral immediately after the slate field, tolerant of common OCR confusions:
#     '1'/'l'/'|'/'!' -> 'I' before roman-numeral parsing (so OCR "lll" -> "III", "ll" -> "II",
#     "l" -> "I", "1V" -> "IV"); lowercase roman letters uppercase ("i" -> I, "vl" -> VI); trailing
#     punctuation noise is stripped ("V*" -> V, "ll*" -> II, "Vl*" -> VI).
#   - SLATE+TAKE CAN SHARE A FIELD: "3pt3/56 I/ll*" carries "56 I" in one field -- leading numeric
#     token = slate, trailing ROMAN-LETTER token = in-field take. A take parsed from the FOLLOWING
#     extra field wins over the in-field one (here take = II from "ll*"); the in-field take is the
#     fallback when the extra fields yield no roman. BUT a trailing PURE-DIGIT token is NOT a take:
#     it is an OCR-inserted space inside the slate number, and the digit tokens concatenate
#     ("55 1" -> slate 551, "500 1" -> slate 5001, "5 18" -> slate 518) -- operator-verified.
#   - NOISY SLATE TOKENS: OCR glues letters onto a mostly-numeric slate ("503o" -> 503,
#     "1O0" -> 10). Strip the letters, keep the digits -- do NOT do O->0 style substitutions
#     (operator-verified data resolved "1O0" as 10, not 100; substitution guessing can fabricate
#     a wrong slate). This letter-strip is tried BEFORE the confusable-substitution fallback below,
#     so "1O0" keeps resolving to 10, never 100.
#   - OCR-CONFUSABLE SUBSTITUTION (fallback, only when digits/letter-strip both fail): some slates
#     OCR almost entirely as letters/symbols that are visually confusable with digits (e.g. "sso"
#     for slate "550", "55&" for slate "558") -- letter-stripping alone leaves too few/no digits to
#     be plausible. As a LAST resort, try a whole-token character substitution (s/S->5, o/O->0,
#     &->8, l/I/|->1, B->8, g/q->9, Z->2, e->6) and accept it only if the result is fully numeric
#     and 1-4 digits. This is intentionally lower priority than the letter-strip rule above so a
#     token that already resolves cleanly by stripping letters (like "1O0") never gets re-guessed
#     by substitution.
#   - TAKE-ONLY READS: when the scene is a composite join and no numeric slate parses at all
#     ("5+6pt2/I/V*"), the take is still extracted (last roman-parsing field -> V) with
#     slate=None -- a take-only read still disambiguates downstream matching.
#   - Non-slate tokens: stock filenames (e.g. "Snow-053", "istockphoto-..."), camera rolls (e.g.
#     "A040C012_260130"), AI-placeholder filenames, generic AE/comp labels, end-of-film markers
#     (e.g. "SLUTSCEN" = Swedish "end scene"), and other free text that isn't the scene/slate/extra
#     triplet. These must NOT be force-parsed into a fake slate -- return slate=None so callers fall
#     back to visual (e.g. CLIP/pHash) matching instead of trusting a garbage numeric code.
#
# parse_slate() returns (scene, slate, take_roman, extra):
#   scene       str   raw scene field, or "" if unavailable
#   slate       str   zero-free numeric string (e.g. "123"), or None if this isn't a real slate
#   take_roman  str   normalized roman numeral (e.g. "I", "II", "IV"), or "" if absent/unparseable
#   extra       str   remaining tail text after the take (camera, MOS, notes), or ""

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}

# Tokens that mark a scene-reference field (never the slate) when found anywhere in that field.
_SCENE_REF_MARKERS = ("pt", "+")

# A handful of well-known non-slate free-text markers. Not exhaustive by design -- the real guard
# is structural (see parse_slate docstring: no '/', or no clean numeric field survives pt/+
# filtering), this list just catches common literal tokens fast. Matched on WORD BOUNDARIES:
# a location/scene name that merely CONTAINS one of these words (e.g. a Stockholm-place-name
# starting with "STOCK...") must not be rejected -- only the standalone word means stock footage.
_NON_SLATE_RE = re.compile(
    r"(?:\b(?:stock|camera-roll|cameraroll|placeholder|slutscen|wip)\b"
    r"|work in progress|\.mov\b|\.mp4\b|\.mxf\b)",
    re.IGNORECASE)


def _has_non_slate_marker(text):
    """True if the text contains a known non-slate free-text marker (word-boundary matched)."""
    return bool(_NON_SLATE_RE.search(text or ""))


def _looks_like_scene_ref(field):
    """True if this '/'-separated field is a scene reference (contains pt / +), not a slate."""
    low = field.lower()
    return any(m in low for m in _SCENE_REF_MARKERS)


# OCR-confusable character map used ONLY as the last-resort substitution fallback in
# _slate_from_token (see the "OCR-CONFUSABLE SUBSTITUTION" grammar note above). Each mapped
# character is one commonly confused with a digit in burn-in OCR:
_CONFUSABLE_SUB = {
    "s": "5", "S": "5",   # s/S <-> 5
    "o": "0", "O": "0",   # o/O <-> 0
    "&": "8",             # & <-> 8 (ampersand glyph misread for a figure-8 slate digit)
    "l": "1", "I": "1", "|": "1",  # l/I/| <-> 1 (same family as the take-side roman-OCR fixups)
    "B": "8",             # B <-> 8
    "g": "9", "q": "9",   # g/q <-> 9
    "Z": "2",             # Z <-> 2
    "e": "6",             # e <-> 6
}


def _substitute_confusables(tok):
    """Whole-token OCR-confusable character substitution (see _CONFUSABLE_SUB). Returns the
    substituted string; callers decide whether the result is plausible."""
    return "".join(_CONFUSABLE_SUB.get(ch, ch) for ch in tok)


def _slate_from_token(tok):
    """One whitespace token -> slate numeric string, or None if it isn't a plausible slate token.

    Tried in order (first success wins), so a token that already resolves cleanly by an earlier,
    more conservative rule never gets re-guessed by a later, looser one:

      (a) pure digits, 1-5 chars (e.g. "56", "535", "5001")
      (b) mixed alphanumeric where OCR noise glued letters onto a mostly-numeric slate (e.g.
          "503o" -> "503", "1O0" -> "10"): strip ALL letters, keep the digits. NOTE the "1O0"
          case: operator-verified production data resolved it to slate 10 (letters stripped),
          NOT 100 (O->0 substitution) -- letter-stripping is deterministic and must run BEFORE
          the substitution fallback so this case stays stable. Mixed tokens need >= 2 digits and
          no more letters than digits, so free text ("MOS", "v2") never becomes a slate.
      (c) OCR-confusable substitution (fallback): when (a) and (b) both fail to produce a
          plausible slate, map confusable characters onto digits across the WHOLE token
          (s/S->5, o/O->0, &->8, l/I/|->1, B->8, g/q->9, Z->2, e->6) and accept only if the
          result is fully numeric and 1-4 digits (e.g. "sso" -> "550", "55&" -> "558"). This is
          intentionally the least-trusted rule -- it can fabricate a slate from ordinary free
          text, so it only runs once the safer rules above have both failed. It also refuses any
          token that itself already reads as a valid roman numeral ("I", "V", "Vl*", ...) --
          those are take tokens (a take-only field like "5+6pt2/I/V*" must keep slate=None), not
          misread slate digits, and substitution would otherwise turn "I" into "1".
    """
    tok = tok.strip()
    if not tok:
        return None
    # (a) pure digits
    if tok.isdigit():
        if 1 <= len(tok) <= 5:
            return tok.lstrip("0") or "0"  # normalize "045" -> "45"; keep a single "0" as "0"
        return None
    # (b) letters stripped, digits kept
    if tok.isalnum():
        digits = re.sub(r"[A-Za-z]", "", tok)
        letters = re.sub(r"[^A-Za-z]", "", tok)
        if digits.isdigit() and 2 <= len(digits) <= 5 and len(letters) <= len(digits):
            return digits.lstrip("0") or "0"
    # (c) OCR-confusable substitution, last resort -- but not for tokens that are themselves a
    # valid (possibly OCR-mangled) roman numeral, which are take tokens, not slate digits.
    if parse_take(tok)[0]:
        return None
    subbed = _substitute_confusables(tok)
    if subbed.isdigit() and 1 <= len(subbed) <= 4:
        return subbed.lstrip("0") or "0"
    return None


def _field_slate_take(field):
    """Parse one '/'-separated field as a possible slate field -> (slate, infield_take_roman).

    Two space-inside-the-field patterns coexist, disambiguated by the TYPE of the trailing token
    (both operator-verified against production data):

      - trailing PURE-DIGIT token(s) = OCR split a digit off the slate number: "55 1" -> slate 551,
        "500 1" -> slate 5001, "5 18" -> slate 518. All leading digit tokens CONCATENATE into the
        slate. (An OCR-inserted space inside a number is far more common than a digits-only take
        marker; production always resolved these as one slate number.)
      - trailing ROMAN-LETTER token = the take sharing the slate's field: "56 I" -> slate 56,
        in-field take I (used as a fallback when the following extra field yields no take).

    Returns (None, "") when the field isn't a slate field at all (scene refs with pt/+,
    roman-only fields, free text).
    """
    f = field.strip()
    if not f or _looks_like_scene_ref(f):
        return None, ""
    tokens = f.split()

    if tokens[0].isdigit():
        # concatenate the run of leading pure-digit tokens into the slate number
        i = 0
        digits = ""
        while i < len(tokens) and tokens[i].isdigit():
            digits += tokens[i]
            i += 1
        if not (1 <= len(digits) <= 5):
            # concatenation overflowed a plausible slate -> fall back to the first token alone
            digits = tokens[0]
            i = 1
            if not (1 <= len(digits) <= 5):
                return None, ""
        slate = digits.lstrip("0") or "0"  # normalize "045" -> "45"; keep a single "0" as "0"
        remaining = tokens[i:]
    else:
        slate = _slate_from_token(tokens[0])
        if slate is None:
            return None, ""
        remaining = tokens[1:]

    infield_take = ""
    if remaining:
        infield_take, _ = parse_take(remaining[-1])
    return slate, infield_take


def parse_take(extra):
    """Leading roman-numeral take from the extra field, tolerant of OCR confusions.

    OCR frequently misreads the roman numeral 'I' as '1', 'l' (lowercase L), '|', or '!' -- so
    "lll" -> "III", "ll" -> "II", "l" -> "I", "1V" -> "IV" must all normalize correctly before
    roman-numeral value parsing. Lowercase roman letters ("i", "v", "vl") are uppercased too.
    Trailing punctuation noise the OCR tacks onto the take ("V*", "ll*", "Vl*", "I.", "II,") is
    stripped before parsing. Returns (roman_normalized, int_value); ("", 0) if no take parses.
    """
    tok = extra.strip().split()[0] if extra.strip() else ""
    tok = tok.rstrip("*.,-")  # trailing OCR noise glued to the take token
    # keep only characters that could plausibly be a (possibly OCR-mangled) roman numeral
    tok = re.match(r"[IVXLCivxlc1|!]*", tok).group(0)
    norm = tok.upper()
    for bad in ("1", "L", "|", "!"):
        norm = norm.replace(bad, "I")
    norm = re.sub(r"[^IVXLC]", "", norm)
    if not norm:
        return "", 0
    total, prev = 0, 0
    for ch in reversed(norm):
        v = _ROMAN_VALUES[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    if total <= 0:
        return "", 0
    return norm, total


def parse_slate(raw):
    """Parse one OCR'd slate burn-in string into (scene, slate, take_roman, extra).

    raw: the raw OCR text of the top-left slate crop, expected format "scene / slate / extra".

    Returns (scene, slate, take_roman, extra):
      - slate is a numeric string, or None when the burn-in isn't a real scene/slate/take triplet
        (stock filenames, camera rolls, placeholders, end-markers, free text, or too few '/'
        fields). Callers MUST treat slate=None as "fall back to visual matching", not as an error.
      - scene/take_roman/extra are best-effort and may be "" even when slate is set.

    See the module-level "Slate grammar" comment block above for the full rule set (pt=part,
    + joins scenes, 5000-series is real, OCR-tolerant roman takes).
    """
    raw = (raw or "").strip()
    if not raw or "/" not in raw:
        return "", None, "", ""

    if _has_non_slate_marker(raw):
        return raw, None, "", ""

    fields = [f.strip() for f in raw.split("/")]

    # Field 0 is ALWAYS the scene position by grammar (never the slate), even when it happens to be
    # purely numeric (e.g. "12/535/I" -- "12" is a scene number, not the slate). The slate hunt
    # starts at field 1 and continues skipping any further pt/+ scene-reference fields, so a
    # composite scene that itself used extra '/' separators (e.g. "75pt/+79pt 1/535/I") still
    # resolves to slate=535: scene = every field up to and including the last pt/+ field.
    # A slate field may also carry the take space-separated ("56 I") -- _field_slate_take splits it.
    slate = None
    slate_idx = None
    infield_take = ""
    for i, f in enumerate(fields):
        if i == 0:
            continue  # field 0 is always scene, never a slate candidate
        cand, it = _field_slate_take(f)
        if cand is not None:
            slate, slate_idx, infield_take = cand, i, it
            break

    if slate is None:
        # No clean numeric field survived -- not a real slate burn-in for slate purposes. But a
        # TAKE may still be readable (e.g. "5+6pt2/I/V*": composite scene, unreadable/absent slate,
        # take V in the final position). A take-only read still helps downstream disambiguation
        # (matching against a candidate slate's takes), so extract it: the take is the LAST field
        # that parses as a roman numeral (the take holds the final grammar position). Callers must
        # still treat slate=None as "fall back to visual matching".
        take_roman = ""
        for f in reversed(fields[1:]):
            tr, _ = parse_take(f)
            if tr:
                take_roman = tr
                break
        return fields[0], None, take_roman, ""

    scene = "/".join(fields[:slate_idx]).strip()
    tail_fields = fields[slate_idx + 1:]
    extra_raw = "/".join(tail_fields).strip()
    take_roman, _take_int = parse_take(extra_raw)
    # extra = everything after the take token (camera/MOS/notes tail)
    if take_roman:
        # strip the leading take token off extra_raw for a clean "extra" tail
        parts = extra_raw.split(None, 1)
        extra = parts[1].strip() if len(parts) > 1 else ""
    else:
        # no take in the extra fields -> fall back to a take that shared the slate's field ("56 I")
        take_roman = infield_take
        extra = extra_raw

    return scene, slate, take_roman, extra


def build_official_code(slate, take_int, counter, prefix):
    """slate -> zero-padded 4-digit, counter (Nth occurrence of this slate, in edit order) *10,
    zero-padded 3-digit. Take is folded in as a 2-digit field between them. Example:
    slate=535, take=1, counter=2 (2nd occurrence) -> "<PREFIX>_0535_01_020".

    This mirrors the source production's counter scheme (occurrence*10, e.g. 1st->010, 2nd->020)
    but is prefix-configurable; callers needing a different code shape can ignore this helper and
    build codes from parse_slate()'s raw (scene, slate, take_roman) tuple instead.
    """
    try:
        slate_n = int(slate)
    except (TypeError, ValueError):
        return ""
    return f"{prefix}_{slate_n:04d}_{max(take_int, 0):02d}_{counter * 10:03d}"


# =============================================================================================
# Stage 1: slate OCR (top-left burn-in -> scene/slate/take, multi-frame best-parse)
# =============================================================================================

def ocr_slate_frames(frames_dir, shots, cfg):
    """OCR the top-left slate burn-in for every shot across start/mid/end frames and take a
    best (modal) parse. Writes slate_ocr.csv next to frames_dir (i.e. frames_dir.parent /
    slate_ocr.csv) and returns a summary dict.

    shots: list of dicts with at least {"tcid": str}. Frame files are expected at
      frames_dir / f"{prefix}_{tcid}-{pos}.jpg" for pos in (start, mid, end) -- the bs_worker.py
      frames/ layout.

    Best-parse rule per shot: among {start,mid,end} parses that yielded a slate, pick the modal
    (scene, slate) pair; tie-break to mid, then start, then end. tier = OK if a slate parsed on
    >=2 of the 3 frames (stable), SUSPECT if only 1 frame parsed a slate (or raw text was present
    but unparseable), BLANK if no burn-in text was read on any frame at all. Multi-frame parsing
    matters because partial postviz/comp contamination often covers only part of a shot, so a
    clean slate usually survives on at least one frame.
    """
    frames_dir = Path(frames_dir)
    prefix = get_prefix(cfg)
    crop = get_crop(cfg, "slate")
    upscale = int(cfg.get("ocr_upscale", 1) or 1)
    reader = _get_ocr_reader()

    total = len(shots)
    rows = []
    for i, s in enumerate(shots, 1):
        tcid = s["tcid"]
        raws, per = {}, {}
        for pos in ("start", "mid", "end"):
            path = frames_dir / f"{prefix}_{tcid}-{pos}.jpg"
            raw = ocr_crop(reader, str(path), crop, upscale)
            raws[pos] = raw
            per[pos] = parse_slate(raw)  # (scene, slate, take_roman, extra)

        cand = [(pos, per[pos]) for pos in ("mid", "start", "end") if per[pos][1] is not None]
        n_slate = len(cand)
        if cand:
            votes = Counter((sc, sl) for _, (sc, sl, tr, ex) in cand)
            best_key, _ = votes.most_common(1)[0]
            best = next((per[pos] for pos in ("mid", "start", "end")
                         if per[pos][1] is not None and (per[pos][0], per[pos][1]) == best_key),
                        cand[0][1])
        else:
            best = ("", None, "", "")

        scene, slate, take_roman, extra = best
        _, take_int = parse_take(f"{take_roman} " if take_roman else "")
        if take_roman:
            take_int = parse_take(take_roman)[1]

        if n_slate >= 2:
            tier = "OK"
        elif n_slate == 1 or any(raws.values()):
            tier = "SUSPECT"
        else:
            tier = "BLANK"

        rows.append({
            "tcid": tcid, "scene": scene, "slate": slate or "", "take_roman": take_roman,
            "take_int": take_int, "tier": tier,
            "start_scene": per["start"][0], "start_slate": per["start"][1] or "",
            "mid_scene": per["mid"][0], "mid_slate": per["mid"][1] or "",
            "end_scene": per["end"][0], "end_slate": per["end"][1] or "",
            "n_slate_frames": n_slate,
            "raw_start": raws["start"], "raw_mid": raws["mid"], "raw_end": raws["end"],
        })
        if i % 25 == 0 or i == total:
            print(f"PROGRESS slate {i}/{total}", flush=True)

    # official code per shot: slate + take + per-slate occurrence counter, in edit order
    counters = {}
    for r in rows:
        if not r["slate"]:
            r["official_code"] = ""
            continue
        counters[r["slate"]] = counters.get(r["slate"], 0) + 1
        r["official_code"] = build_official_code(r["slate"], r["take_int"], counters[r["slate"]], prefix)

    cols = ["tcid", "scene", "slate", "take_roman", "take_int", "official_code", "tier",
            "start_scene", "start_slate", "mid_scene", "mid_slate", "end_scene", "end_slate",
            "n_slate_frames", "raw_start", "raw_mid", "raw_end"]
    out_csv = frames_dir.parent / "slate_ocr.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    n_ok = sum(1 for r in rows if r["tier"] == "OK")
    n_suspect = sum(1 for r in rows if r["tier"] == "SUSPECT")
    n_blank = sum(1 for r in rows if r["tier"] == "BLANK")
    summary = {"n_shots": total, "ok": n_ok, "suspect": n_suspect, "blank": n_blank,
               "out_csv": str(out_csv)}
    print(f"LOG [slate] {total} OCR'd: OK={n_ok} SUSPECT={n_suspect} BLANK={n_blank} -> {out_csv}",
          flush=True)
    return summary


# =============================================================================================
# Stage 2: VFX editorial-note OCR (bottom-left burn-in, 3-frame consistency gate)
# =============================================================================================

def _norm_note(s):
    """Normalize note text for consistency comparison only (uppercase, strip non-alnum)."""
    return re.sub(r"[^A-Z0-9 ]", "", (s or "").upper()).strip()


def _note_consensus(raws_present):
    """From non-empty raw note reads, pick the verbatim text to keep: the modal normalized form,
    returning the longest raw variant within that modal group (longest is usually least-truncated
    by OCR)."""
    if not raws_present:
        return ""
    groups = {}
    for r in raws_present:
        groups.setdefault(_norm_note(r), []).append(r)
    modal = max(groups.items(), key=lambda kv: (len(kv[1]), max(len(x) for x in kv[1])))
    return max(modal[1], key=len)


def note_consistency_tier(raws):
    """Given {"start":str,"mid":str,"end":str} raw OCR reads of the VFX-note crop, classify the
    shot per the 3-frame bleed-guard rule and return (tier, is_vfx, note_text).

    A genuine VFX note is burned onto the whole shot, so it must appear on START and MID and END
    to count as this shot's note (rejects bleed: a note visible only on the shot's early frame(s)
    is actually the tail of the PREVIOUS shot's note bleeding across the cut, not a real note on
    this shot).

    Tiers:
      ALL3    note present on start AND mid AND end   -> real VFX shot (confident)
      MIDEND  note present on mid AND end, not start  -> real; slate/handles trimmed the start frame
      PARTIAL note present on only some other subset  -> bleed-ish, manual QC, treated as NOT vfx
      NONE    no note present on any frame             -> not a VFX shot
    """
    present = {pos: bool(_norm_note(raws.get(pos, ""))) for pos in ("start", "mid", "end")}
    n = sum(present.values())
    if present["start"] and present["mid"] and present["end"]:
        tier, is_vfx = "ALL3", 1
    elif present["mid"] and present["end"] and not present["start"]:
        tier, is_vfx = "MIDEND", 1
    elif n == 0:
        tier, is_vfx = "NONE", 0
    else:
        tier, is_vfx = "PARTIAL", 0
    note = _note_consensus([raws[p] for p in ("start", "mid", "end") if present[p]])
    return tier, is_vfx, note, present, n


def ocr_vfx_notes(frames_dir, shots, cfg):
    """OCR the bottom-left VFX editorial-note burn-in for every shot across start/mid/end frames,
    apply the 3-frame consistency gate (note_consistency_tier), and write vfxnote_ocr.csv next to
    frames_dir. Returns a summary dict.

    The note is captured RAW VERBATIM (only OCR-noise-level cleanup, no controlled vocabulary) --
    downstream tools decide what the note means; this stage only decides whether a note reliably
    belongs to this shot.
    """
    frames_dir = Path(frames_dir)
    prefix = get_prefix(cfg)
    crop = get_crop(cfg, "note")
    upscale = int(cfg.get("ocr_upscale", 1) or 1)
    reader = _get_ocr_reader()

    total = len(shots)
    rows = []
    for i, s in enumerate(shots, 1):
        tcid = s["tcid"]
        raws = {pos: ocr_crop(reader, str(frames_dir / f"{prefix}_{tcid}-{pos}.jpg"), crop, upscale)
                for pos in ("start", "mid", "end")}
        tier, is_vfx, note, present, n = note_consistency_tier(raws)
        rows.append({
            "tcid": tcid, "vfx_note": note, "tier": tier, "is_vfx": is_vfx,
            "p_start": int(present["start"]), "p_mid": int(present["mid"]), "p_end": int(present["end"]),
            "n_present": n,
            "raw_start": raws["start"], "raw_mid": raws["mid"], "raw_end": raws["end"],
        })
        if i % 25 == 0 or i == total:
            print(f"PROGRESS notes {i}/{total}", flush=True)

    cols = ["tcid", "vfx_note", "tier", "is_vfx", "p_start", "p_mid", "p_end", "n_present",
            "raw_start", "raw_mid", "raw_end"]
    out_csv = frames_dir.parent / "vfxnote_ocr.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    tiers = Counter(r["tier"] for r in rows)
    n_vfx = sum(r["is_vfx"] for r in rows)
    summary = {"n_shots": total, "all3": tiers["ALL3"], "midend": tiers["MIDEND"],
               "partial": tiers["PARTIAL"], "none": tiers["NONE"], "is_vfx": n_vfx,
               "out_csv": str(out_csv)}
    print(f"LOG [notes] {total} shots: ALL3={tiers['ALL3']} MIDEND={tiers['MIDEND']} "
          f"PARTIAL={tiers['PARTIAL']} NONE={tiers['NONE']} -> VFX={n_vfx} -> {out_csv}", flush=True)
    return summary


# =============================================================================================
# Stage 3: show-TC offset probe (top-right burn-in vs file-relative TC)
# =============================================================================================

def _tc_frames(h, m, s, f, fps):
    return ((h * 60 + m) * 60 + s) * round(fps) + f


def _frames_to_tc(fr, fps):
    r = round(fps)
    f = fr % r
    t = fr // r
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}:{f:02d}"


def parse_ocr_tc(text, fps):
    """Pull 8 digits (HHMMSSFF) out of an OCR'd timecode burn-in; return a frame count, or None if
    the text doesn't look like a plausible timecode (wrong digit count, or MM/SS/FF out of range).
    """
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 8:
        return None
    h, m, s, f = int(digits[:2]), int(digits[2:4]), int(digits[4:6]), int(digits[6:8])
    if m > 59 or s > 59 or f >= round(fps):
        return None
    return _tc_frames(h, m, s, f, fps)


def offset_is_constant(offsets, tolerance_frames=1):
    """True if the spread (max-min) of a list of measured offsets is within tolerance -- i.e. the
    file-relative-TC-to-show-TC offset is a genuine constant (safe to apply film-wide) rather than
    drifting (which usually indicates an fps mismatch, e.g. 23.976 vs 24)."""
    if not offsets:
        return False
    return (max(offsets) - min(offsets)) <= tolerance_frames


def probe_tc_offset(frames_dir, shots, cfg, sample_positions=("start",)):
    """OCR the top-right show-TC burn-in on a sample of shots' start frames, compute
    offset = show_TC - file_TC per sample (in frames), and check constancy across the sample.
    Writes tc_offset.txt next to frames_dir. Returns a summary dict including the median offset
    (frames and formatted TC) and whether it is constant.

    This is a read-only measurement stage: it never rewrites shot codes itself, it only reports
    whether a single constant offset is safe to apply.
    """
    frames_dir = Path(frames_dir)
    prefix = get_prefix(cfg)
    crop = get_crop(cfg, "showtc")
    fps = get_fps(cfg)
    upscale = int(cfg.get("ocr_upscale", 1) or 1)
    reader = _get_ocr_reader()

    total = len(shots)
    lines = [f"fps={fps}, {total} shots sampled"]
    lines.append(f"{'tcid(file_TC)':>16} {'show_TC(OCR)':>13} {'offset':>12}  raw_ocr")
    offsets = []
    for i, s in enumerate(shots, 1):
        tcid = s["tcid"]
        # file-relative frame count implied by tcid's HHMMSSFF encoding (matches bs_worker.tc_to_id)
        h, m, sec, ff = int(tcid[0:2]), int(tcid[2:4]), int(tcid[4:6]), int(tcid[6:8])
        file_fr = _tc_frames(h, m, sec, ff, fps)
        raw = ""
        for pos in sample_positions:
            path = frames_dir / f"{prefix}_{tcid}-{pos}.jpg"
            raw = ocr_crop(reader, str(path), crop, upscale)
            if raw:
                break
        show_fr = parse_ocr_tc(raw, fps)
        if show_fr is None:
            lines.append(f"{tcid:>16} {'??':>13} {'--':>12}  {raw!r}")
        else:
            off = show_fr - file_fr
            offsets.append(off)
            lines.append(f"{tcid:>16} {_frames_to_tc(show_fr, fps):>13} "
                          f"{_frames_to_tc(off, fps):>12}  {raw!r}")
        if i % 25 == 0 or i == total:
            print(f"PROGRESS tcoffset {i}/{total}", flush=True)

    summary = {"n_samples": total, "n_read": len(offsets)}
    if offsets:
        lo, hi = min(offsets), max(offsets)
        constant = offset_is_constant(offsets)
        med = median(offsets)
        lines.append("")
        lines.append(f"offsets: n={len(offsets)}  min={_frames_to_tc(lo, fps)}  "
                      f"max={_frames_to_tc(hi, fps)}  spread={hi - lo} frames")
        lines.append("=> CONSTANT" if constant else "=> DRIFTS (check fps mismatch, e.g. 23.976 vs 24)")
        lines.append(f"median offset = {_frames_to_tc(round(med), fps)} ({round(med)} frames @ {fps}fps)")
        summary.update({"min_offset_frames": lo, "max_offset_frames": hi,
                         "spread_frames": hi - lo, "constant": constant,
                         "median_offset_frames": round(med),
                         "median_offset_tc": _frames_to_tc(round(med), fps)})
    else:
        summary["constant"] = False

    out_txt = frames_dir.parent / "tc_offset.txt"
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary["out_txt"] = str(out_txt)
    print(f"LOG [tcoffset] {len(offsets)}/{total} read; "
          f"{'CONSTANT' if summary.get('constant') else 'not constant / insufficient data'} -> {out_txt}",
          flush=True)
    return summary


# =============================================================================================
# Stage 4: boundary QC (slate oracle)
# =============================================================================================

def _small_gray(path, size=(192, 108)):
    """Downscaled grayscale array for cheap picture-continuity comparison across a boundary."""
    import numpy as np
    from PIL import Image
    try:
        return np.asarray(Image.open(path).convert("L").resize(size), dtype=np.float32)
    except Exception:
        return None


def _mad(a, b):
    import numpy as np
    if a is None or b is None:
        return None
    return float(np.abs(a - b).mean())


def clean_slate_diff(a, b):
    """True if two slate-number strings differ in a way unlikely to be pure OCR noise (i.e. a
    believable real slate change, not a single misread digit)."""
    if not a or not b or a == b:
        return False
    if a in b or b in a:          # prefix/substring -> likely a dropped/extra digit, OCR noise
        return False
    try:
        return abs(int(a) - int(b)) > 1
    except ValueError:
        return a != b


def _is_unreliable_slate(raw_or_parsed):
    """True if a slate reading is a known-unparseable placeholder (work-in-progress labels,
    filenames, etc.) rather than a real scene/slate/take burn-in -- these must be flagged
    UNRELIABLE, never guessed at, per the boundary-QC design. Word-boundary matched (same
    markers as parse_slate's non-slate guard)."""
    return _has_non_slate_marker(raw_or_parsed or "")


def boundary_qc(frames_dir, shots, cfg, slate_csv=None,
                 pic_same_threshold=12.0, pic_jump_threshold=26.0, min_shot_len=8):
    """QC detected shot boundaries using the "slate oracle": at each boundary between consecutive
    shots, compare the slate of the PREVIOUS shot's END frame against the NEXT shot's START frame.

      - A slate CHANGE across the boundary corroborates a real cut.
      - The SAME slate on both sides of a boundary, combined with visually-continuous picture
        (low frame-to-frame difference), flags a suspect over-split -> MERGE candidate.
      - A large picture jump WITHIN a single shot (start<->mid or mid<->end), corroborated by a
        clean slate-number change from that shot's own start to its own end, flags a missed cut
        hidden inside one detected shot -> SPLIT candidate.
      - Placeholder slates (stock/camera-roll/filename/work-in-progress burn-ins) can't be parsed
        into a real scene/slate/take triplet -- these are flagged UNRELIABLE rather than guessed at
        (the oracle must not fabricate a slate identity it doesn't have).

    shots must be in edit order. If slate_csv is not given, this stage runs its own slate OCR pass
    via ocr_slate_frames() first (needs start/mid/end frames + slate crop configured).

    Writes boundary_qc.csv next to frames_dir. Returns a summary dict with merges/splits/unreliable
    counts.
    """
    frames_dir = Path(frames_dir)
    prefix = get_prefix(cfg)

    if slate_csv and Path(slate_csv).exists():
        ocr = {r["tcid"]: r for r in csv.DictReader(open(slate_csv, encoding="utf-8"))}
    else:
        ocr_slate_frames(frames_dir, shots, cfg)
        default_csv = frames_dir.parent / "slate_ocr.csv"
        ocr = {r["tcid"]: r for r in csv.DictReader(open(default_csv, encoding="utf-8"))}

    def frame_img(tcid, pos):
        p = frames_dir / f"{prefix}_{tcid}-{pos}.jpg"
        return _small_gray(p) if p.exists() else None

    total = len(shots)
    merges, splits, unreliable = [], [], []

    # MERGE candidates: consecutive shots sharing the same MID slate+take, with continuous picture
    # across the boundary (a slate legitimately spans multiple intercut editorial shots, so "same
    # slate" alone is not an error -- continuity of picture is the corroborating signal).
    for k in range(1, total):
        prev_tcid, cur_tcid = shots[k - 1]["tcid"], shots[k]["tcid"]
        po, co = ocr.get(prev_tcid, {}), ocr.get(cur_tcid, {})
        if _is_unreliable_slate(po.get("raw_mid", "")) or _is_unreliable_slate(co.get("raw_mid", "")):
            unreliable.append({"kind": "MERGE_CHECK", "prev_tcid": prev_tcid, "cur_tcid": cur_tcid,
                                "reason": "placeholder slate burn-in, can't anchor oracle"})
            continue
        if not (po.get("slate") and po["slate"] == co.get("slate")
                and po.get("take_roman", "") == co.get("take_roman", "")):
            continue
        pe, cs = frame_img(prev_tcid, "end"), frame_img(cur_tcid, "start")
        pic = _mad(pe, cs)
        if pic is not None and pic < pic_same_threshold:
            merges.append({"boundary_at": cur_tcid, "prev_tcid": prev_tcid, "after_tcid": cur_tcid,
                            "slate": po["slate"], "take": po.get("take_roman", ""),
                            "pic_mad": round(pic, 1)})

    # SPLIT? candidates: large intra-shot picture jump, corroborated by a clean start->end slate
    # number change (weak per-frame endpoint OCR is noisy, so it's used only as corroboration; the
    # picture-jump signal drives detection).
    for s in shots:
        tcid = s["tcid"]
        length = s.get("len_frames") or 0
        if length and length < min_shot_len:
            continue
        st, mi, en = frame_img(tcid, "start"), frame_img(tcid, "mid"), frame_img(tcid, "end")
        j1, j2 = _mad(st, mi), _mad(mi, en)
        jump = max(v for v in (j1, j2) if v is not None) if (j1 is not None or j2 is not None) else None
        if jump is None or jump < pic_jump_threshold:
            continue
        row = ocr.get(tcid, {})
        if _is_unreliable_slate(row.get("raw_start", "")) or _is_unreliable_slate(row.get("raw_end", "")):
            unreliable.append({"kind": "SPLIT_CHECK", "prev_tcid": "", "cur_tcid": tcid,
                                "reason": "placeholder slate burn-in, can't anchor oracle"})
            continue
        corroborated = clean_slate_diff(row.get("start_slate", ""), row.get("end_slate", ""))
        splits.append({"shot_tcid": tcid, "len_frames": length,
                        "jump_startmid": round(j1, 1) if j1 is not None else "",
                        "jump_midend": round(j2, 1) if j2 is not None else "",
                        "slate_corroborated": corroborated,
                        "start_slate": row.get("start_slate", ""), "end_slate": row.get("end_slate", "")})

    out_csv = frames_dir.parent / "boundary_qc.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "boundary_frame_tcid", "prev_tcid", "after_tcid", "slate", "detail"])
        for r in merges:
            w.writerow(["MERGE", r["boundary_at"], r["prev_tcid"], r["after_tcid"], r["slate"],
                        f"same slate+take, continuous picture (mad={r['pic_mad']})"])
        for r in splits:
            if not r["slate_corroborated"]:
                continue
            jump = max(v for v in (r["jump_startmid"], r["jump_midend"]) if v != "")
            w.writerow(["SPLIT", "", r["shot_tcid"], "", f"{r['start_slate']}->{r['end_slate']}",
                        f"intra-shot jump {jump} + slate change"])
        for r in unreliable:
            w.writerow(["UNRELIABLE", "", r["prev_tcid"], r["cur_tcid"], "", r["reason"]])

    n_split_corr = sum(1 for r in splits if r["slate_corroborated"])
    summary = {"n_shots": total, "merges": len(merges), "splits_picture_jump": len(splits),
               "splits_slate_corroborated": n_split_corr, "unreliable": len(unreliable),
               "out_csv": str(out_csv)}
    print(f"PROGRESS boundaryqc {total}/{total}", flush=True)
    print(f"LOG [boundaryqc] MERGE={len(merges)}  SPLIT?(picture jump)={len(splits)} "
          f"(slate-corroborated={n_split_corr})  UNRELIABLE={len(unreliable)} -> {out_csv}", flush=True)
    return summary


# =============================================================================================
# probecrops: verify OCR crop boxes visually against a real frame, before trusting OCR output
# (see UX_PLAN.md P6; module-level "Crop regions are ALWAYS config-driven" note above)
# =============================================================================================

def extract_frame(movie, frame_idx, out_path, fps=None):
    """Grab one frame (0-based frame_idx) from `movie` via ffmpeg, using bs_worker's accurate
    single-frame seek (W._seek_args) so the frame matches what the rest of the pipeline would
    extract for that shot. Returns (ok, ffmpeg_completed_process)."""
    movie = str(movie)
    fps = fps or W.get_fps(movie)
    pre, post = W._seek_args(frame_idx, None, fps)
    r = subprocess.run([W.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
                       + pre + ["-i", movie] + post
                       + ["-frames:v", "1", "-q:v", "2", str(out_path)],
                       capture_output=True)
    return r.returncode == 0, r


# distinct, high-contrast colors per crop region so overlapping/adjacent boxes stay readable
PROBE_COLORS = {
    "slate": (255, 64, 64),    # red
    "note": (64, 200, 255),    # cyan
    "showtc": (255, 210, 40),  # yellow
}


def probe_crops(movie, frame_idx, out_path, cfg):
    """Extract one frame from `movie` and draw the configured slate/note/showtc OCR crop boxes
    on top of it, each labelled with its region name at the box's top-left corner, so a
    producer can eyeball whether the crops actually hug the burn-ins before trusting a real
    OCR pass. Writes `out_path` (a jpg) and returns [(name, (x0, y0, x1, y1)), ...] drawn, in
    the same (slate, note, showtc) order every time.
    """
    from PIL import Image, ImageDraw
    movie = Path(movie)
    out_path = Path(out_path)
    fps = get_fps(cfg)
    ok, r = extract_frame(movie, frame_idx, out_path, fps=fps)
    if not ok:
        raise SystemExit(f"ERROR: could not extract frame {frame_idx} from {movie}: "
                         f"{W._tail(r.stderr)}")

    im = Image.open(out_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    boxes = []
    for name in ("slate", "note", "showtc"):
        box = get_crop(cfg, name)
        color = PROBE_COLORS.get(name, (0, 255, 0))
        draw.rectangle(box, outline=color, width=3)
        draw.text((box[0] + 4, box[1] + 4), name, fill=color)
        boxes.append((name, box))
    im.save(out_path, quality=92)
    return boxes


# =============================================================================================
# CLI
# =============================================================================================

def _load_shots(scenes_csv, fps, prefix):
    """Load shots from a Scenes.csv via bs_worker's parser and attach a tcid to each (matching the
    frames/<prefix>_<tcid>-{pos}.jpg naming bs_worker.py produces)."""
    scenes = W.parse_scenes_csv(scenes_csv)
    for s in scenes:
        s["tcid"] = W.tc_to_id(s["start_tc"], fps)
    return scenes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["slate", "notes", "tcoffset", "boundaryqc", "probecrops"])
    ap.add_argument("--frames-dir", default=None,
                    help="[slate/notes/tcoffset/boundaryqc] directory containing "
                         "<prefix>_<tcid>-{start,mid,end}.jpg")
    ap.add_argument("--scenes-csv", default=None,
                    help="[slate/notes/tcoffset/boundaryqc] PySceneDetect/TransNetV2-format Scenes.csv")
    ap.add_argument("--config", default="", help="path to config.json (ocr_crops, prefix, fps, ...)")
    ap.add_argument("--prefix", default=None, help="override cfg/BS_PREFIX shot-code prefix")
    ap.add_argument("--fps", type=float, default=None, help="override cfg fps (default 24.0)")
    ap.add_argument("--slate-csv", default=None, help="[boundaryqc] reuse an existing slate_ocr.csv instead of re-OCRing")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N shots (debugging)")
    ap.add_argument("--movie", default=None, help="[probecrops] path to the reference movie")
    ap.add_argument("--frame", type=int, default=None,
                    help="[probecrops] 0-based frame index to extract and check")
    ap.add_argument("--out", default=None,
                    help="[probecrops] output jpg path (default: _<movie-stem>_crop_check.jpg "
                         "beside the movie)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.prefix:
        cfg["prefix"] = args.prefix
    if args.fps:
        cfg["fps"] = args.fps

    if args.stage == "probecrops":
        if not args.movie or args.frame is None:
            ap.error("probecrops requires --movie and --frame")
        movie = Path(args.movie)
        out_path = Path(args.out) if args.out else movie.parent / f"_{movie.stem}_crop_check.jpg"
        print(f"=== bs_ocr probecrops | movie={movie} frame={args.frame} ===", flush=True)
        boxes = probe_crops(movie, args.frame, out_path, cfg)
        print(f"LOG [probecrops] wrote {out_path}", flush=True)
        for name, box in boxes:
            print(f"  {name}: {box}", flush=True)
        print("DONE probecrops", flush=True)
        return

    if not args.frames_dir or not args.scenes_csv:
        ap.error(f"{args.stage} requires --frames-dir and --scenes-csv")

    fps = get_fps(cfg)
    prefix = get_prefix(cfg)
    frames_dir = Path(args.frames_dir)

    print(f"=== bs_ocr {args.stage} | frames={frames_dir} | prefix={prefix} @ {fps:.3f}fps ===", flush=True)
    shots = _load_shots(args.scenes_csv, fps, prefix)
    if args.limit:
        shots = shots[:args.limit]
    print(f"LOG loaded {len(shots)} shots from {Path(args.scenes_csv).name}", flush=True)

    if args.stage == "slate":
        ocr_slate_frames(frames_dir, shots, cfg)
    elif args.stage == "notes":
        ocr_vfx_notes(frames_dir, shots, cfg)
    elif args.stage == "tcoffset":
        probe_tc_offset(frames_dir, shots, cfg)
    elif args.stage == "boundaryqc":
        boundary_qc(frames_dir, shots, cfg, slate_csv=args.slate_csv)

    print(f"DONE {args.stage}", flush=True)


if __name__ == "__main__":
    main()
