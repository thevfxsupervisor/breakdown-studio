#!/usr/bin/env python
"""bs_match.py - cross-cut shot matching + audit for Breakdown Studio.

The tool's differentiator: when a new edit of the film arrives, figure out which shots in the
new cut correspond to which shots in the existing master breakdown, propose a stable shot code
for every new-cut shot, flag genuinely new shots, and flag master shots that may need to be
omitted -- all without ever guessing twice at the same master shot (a master shot code is
unique, so this is a GLOBAL 1:1 assignment problem, not a per-shot best-match problem).

Generalized from a production pipeline that matched real film cuts across multiple deliveries.
Every studio-specific path, sheet ID, and client name has been stripped; the algorithms are the
proven ones. Config is passed as plain CLI args / CSVs -- no hardcoded prefixes or IDs.

COMMANDS
  compare   <cutA_dir> <cutB_dir>       diff two cuts' shot lists -> SAME/RETIMED/MOVED/
                                        CHANGED/ADDED/REMOVED (perceptual hash + duration)
  assign    --new new_shots.csv --master master_shots.csv
                                        1:1 tiered assignment (the differentiator)
                 [--write-sheet SPREADSHEET_ID [--write-sheet-tab TAB]]
                                        THE GATE (staging only, see below) -- stage the proposal
                                        into a review sheet's Match Tier / Proposed Master Code /
                                        Match Note columns. NOTHING IS EVER APPLIED TO A MASTER
                                        BREAKDOWN by this flag; an operator reviews and applies by
                                        hand. Idempotent (safe to re-run).
  audit     --ocr vfxnote_ocr.csv --sheet sheet_rows.csv
                                        invisible-VFX audit (OCR ground truth vs operator flags)
  fpscheck  <cutA_movie> <cutB_movie>    warn if the two cuts were exported at different FPS
                                        (silently corrupts any frame-index-based comparison)
  compare's --out CSV is a tidy change report: old_code, new_code, verdict (SAME/SHIFTED/CHANGED/
  ADDED/REMOVED), old_dur, new_dur, dur_delta -- plus a one-screen summary table on stdout.

Every command also accepts --sheet-id/--tab (Google Sheet) instead of a CSV, via bs_gsheets'
auth helper, resolving columns BY HEADER NAME so a reordered sheet still works.

DEPENDENCIES
  Required: stdlib only for assign/audit. compare needs numpy + opencv-python (cv2).
  Optional: torch + transformers + Pillow, for CLIP visual similarity in the VISUAL tier of
  assign. When unavailable, assign degrades to a Pillow-based average-hash/difference-hash
  similarity (still useful, just less semantically robust) and prints a clear log line saying
  so. Neither is required to use the EXACT-CODE / ORDINAL / SLATE-ORDINAL tiers, which cover
  the large majority of shots in a typical re-cut.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# ----------------------------------------------------------------------------------------------
# shared config (override via CLI; these are just the out-of-the-box defaults)
# ----------------------------------------------------------------------------------------------
DEFAULT_PREFIX = "SHW"

# markers in a master row's revision/notes column that mean "operator hand-added this shot -
# never propose it as an omit candidate even if no new-cut shot claims it."
PROTECTED_MARKERS = ("new cut", "manual", "added")


# ================================================================================================
# 1. COMPARE  -  diff two cuts' shot lists (perceptual hash + duration)
# ================================================================================================
POS = ("start", "mid", "end")
HASH_BITS = 64
TOP_KEEP_DEFAULT = 0.74   # crop out the bottom band by default (subtitles / burn-in can differ
CROP_TOP_DEFAULT = 0.0    # between cuts and would corrupt matching); --full-frame disables this.


def _phash64(path, crop_top, top_keep):
    """DCT perceptual hash -> 64-bit numpy bool vector. Robust to resize/recompress/grade."""
    import cv2
    import numpy as np
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    h = img.shape[0]
    img = img[int(h * crop_top): int(h * top_keep), :]
    img = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(img)
    block = d[:8, :8].copy()
    block[0, 0] = 0.0
    med = np.median(block)
    return (block > med).flatten()


def _color_layout(path, crop_top, top_keep):
    """Coarse colour-layout descriptor: top-cropped BGR -> 8x8 -> 192 floats in [0,1]."""
    import cv2
    import numpy as np
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return None, 0.0
    h = img.shape[0]
    img = img[int(h * crop_top): int(h * top_keep), :]
    small = cv2.resize(img, (8, 8), interpolation=cv2.INTER_AREA).astype("float32") / 255.0
    return small.flatten(), float(small.std())


def _shot_fingerprint(thumbs_dir, tcid, prefix, crop_top, top_keep):
    import numpy as np
    bits, cols, stds, ok = [], [], [], False
    for p in POS:
        f = Path(thumbs_dir) / f"{prefix}_{tcid}-{p}.jpg"
        h = _phash64(f, crop_top, top_keep) if f.exists() else None
        c, std = _color_layout(f, crop_top, top_keep) if f.exists() else (None, 0.0)
        bits.append(h if h is not None else np.zeros(HASH_BITS, dtype=bool))
        cols.append(c if c is not None else np.zeros(192, dtype=np.float32))
        stds.append(std)
        ok = ok or h is not None
    if not ok:
        return None
    return {"phash": np.concatenate(bits), "color": np.concatenate(cols),
            "dark": float(np.mean(stds)) < 0.06}


def _tc_to_tcid(tc, fps=24):
    m = re.match(r"(\d+):(\d+):(\d+)(?:\.(\d+))?", tc)
    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = float("0." + m.group(4)) if m.group(4) else 0.0
    f = int(round(frac * fps))
    if f >= fps:
        f = fps - 1
    return f"{h:02d}{mn:02d}{s:02d}{f:02d}"


def _tcid_seconds(tcid, fps=24):
    h, m, s, f = int(tcid[0:2]), int(tcid[2:4]), int(tcid[4:6]), int(tcid[6:8])
    return h * 3600 + m * 60 + s + f / fps


def _parse_scenes_csv(scenes_dir):
    """{tcid: length_frames} from a PySceneDetect/TransNet -Scenes.csv."""
    files = glob.glob(os.path.join(scenes_dir, "*-Scenes.csv"))
    if not files:
        return {}
    with open(files[0], newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.reader(fh))
    hdr = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Scene Number"), None)
    if hdr is None:
        return {}
    cols = rows[hdr]
    ci = {name: cols.index(name) for name in cols}
    out = {}
    for r in rows[hdr + 1:]:
        if len(r) <= ci.get("Length (frames)", 99):
            continue
        tc = r[ci["Start Timecode"]].strip()
        out[_tc_to_tcid(tc)] = int(r[ci["Length (frames)"]])
    return out


def load_cut(out_dir, prefix, start_min=None, end_min=None, crop_top=CROP_TOP_DEFAULT,
             top_keep=TOP_KEEP_DEFAULT):
    """Return ordered list of shot dicts (tcid, secs, length, phash, color, dark) for one cut's
    output folder (expects thumbs/ + scenes/ subfolders, the Breakdown Studio worker layout)."""
    thumbs = os.path.join(out_dir, "thumbs")
    lengths = _parse_scenes_csv(os.path.join(out_dir, "scenes"))
    tcids = sorted({os.path.basename(f).split("-")[0].replace(f"{prefix}_", "")
                    for f in glob.glob(os.path.join(thumbs, f"{prefix}_*-mid.jpg"))})
    shots = []
    for tcid in tcids:
        secs = _tcid_seconds(tcid)
        if start_min is not None and secs < start_min * 60:
            continue
        if end_min is not None and secs > end_min * 60:
            continue
        fp = _shot_fingerprint(thumbs, tcid, prefix, crop_top, top_keep)
        if fp is None:
            continue
        shots.append({"tcid": tcid, "secs": secs, "length": lengths.get(tcid), **fp})
    return shots


def _hamming_matrix(A, B):
    import numpy as np
    FAi = np.array([s["phash"] for s in A], dtype=np.int16)
    FBi = np.array([s["phash"] for s in B], dtype=np.int16)
    Dp = (FAi.sum(1)[:, None] + FBi.sum(1)[None, :] - 2 * (FAi @ FBi.T)).astype(np.int16)
    CA = np.array([s["color"] for s in A], dtype=np.float32)
    CB = np.array([s["color"] for s in B], dtype=np.float32)
    Dc = np.empty((len(A), len(B)), dtype=np.float32)
    for i in range(len(A)):
        Dc[i] = np.abs(CB - CA[i]).mean(1) * 100.0
    return Dp, Dc


def _match_levels(Dp, Dc, darkA, darkB, strong, near, cstrong, cnear):
    import numpy as np
    nd = (~darkA[:, None]) & (~darkB[None, :])
    same = (Dp <= strong) | ((Dc <= cstrong) & nd)
    nearm = (Dp <= near) | ((Dc <= cnear) & nd)
    return np.where(same, 2, np.where(nearm, 1, 0)).astype(np.int8)


def _align(A, B, lvl, gap=-1.0):
    """Needleman-Wunsch global alignment. +2 SAME, +0.4 NEAR diagonal score."""
    import numpy as np
    n, m = len(A), len(B)
    NEG = -1e9
    score = np.full((n + 1, m + 1), 0.0)
    score[:, 0] = np.arange(n + 1) * gap
    score[0, :] = np.arange(m + 1) * gap
    diag = np.where(lvl == 2, 2.0, np.where(lvl == 1, 0.4, NEG))
    for i in range(1, n + 1):
        di = diag[i - 1]
        srow, prow = score[i], score[i - 1]
        for j in range(1, m + 1):
            srow[j] = max(prow[j - 1] + di[j - 1], prow[j] + gap, srow[j - 1] + gap)
    ops = []
    i, j = n, m
    while i > 0 and j > 0:
        d = diag[i - 1, j - 1]
        if score[i, j] == score[i - 1, j - 1] + d and d > NEG / 2:
            ops.append(("equal" if lvl[i - 1, j - 1] == 2 else "sub", i - 1, j - 1))
            i -= 1; j -= 1
        elif score[i, j] == score[i - 1, j] + gap:
            ops.append(("del", i - 1, None)); i -= 1
        else:
            ops.append(("ins", None, j - 1)); j -= 1
    while i > 0:
        ops.append(("del", i - 1, None)); i -= 1
    while j > 0:
        ops.append(("ins", None, j - 1)); j -= 1
    ops.reverse()
    return ops


def _detect_moves(ops, lvl):
    dels = [i for tag, i, _ in ops if tag == "del"]
    inss = [j for tag, _, j in ops if tag == "ins"]
    moved_a, moved_b, pairs = set(), set(), {}
    for i in dels:
        cand = [j for j in inss if j not in moved_b and lvl[i, j] == 2]
        if cand:
            j = cand[0]
            moved_a.add(i); moved_b.add(j)
            pairs[("a", i)] = j; pairs[("b", j)] = i
    return moved_a, moved_b, pairs


def classify(ops, A, B, Dp, lvl, strong, near, retime_tol):
    moved_a, moved_b, mpairs = _detect_moves(ops, lvl)
    rows = []
    for tag, i, j in ops:
        a = A[i] if i is not None else None
        b = B[j] if j is not None else None
        if tag in ("equal", "sub"):
            dist = int(Dp[i, j])
            dl = (b["length"] or 0) - (a["length"] or 0) if a["length"] and b["length"] else None
            retimed = dl is not None and abs(dl) > retime_tol
            if a["dark"] and b["dark"]:
                status, method = ("RETIMED" if retimed else "SAME"), "slate"
            elif dist <= strong:
                status, method = ("RETIMED" if retimed else "SAME"), "phash"
            elif dist <= near:
                status, method = "CHANGED", "phash"
            else:
                status, method = "REVIEW", "color"
            rows.append((status, a["tcid"], b["tcid"], dist, dl, method))
        elif tag == "del":
            if i in moved_a:
                rows.append(("MOVED", a["tcid"], B[mpairs[("a", i)]]["tcid"],
                             int(Dp[i, mpairs[("a", i)]]), None, "phash"))
            else:
                rows.append(("REMOVED", a["tcid"], "", "", None, ""))
        else:
            if j in moved_b:
                continue
            rows.append(("ADDED", "", b["tcid"], "", None, ""))
    return rows


# ------------------------------------------------------------------------------------------------
# FEATURE 4: tidy change-report (old_code/new_code/verdict/old_dur/new_dur/dur_delta) + a
# one-screen stdout summary table. Pure logic (build_change_report / format_change_summary) so it
# is unit-testable on synthetic in-memory cut lists without cv2/numpy -- classify()'s own status
# vocabulary (SAME/RETIMED/MOVED/CHANGED/REVIEW/ADDED/REMOVED) is mapped onto the tidy report's
# verdict vocabulary (SAME/SHIFTED/CHANGED/ADDED/REMOVED); RETIMED and REVIEW both collapse to
# CHANGED (a duration or a picture change either way -- REVIEW just means the classifier wasn't
# confident which), and MOVED collapses to SHIFTED (same content, different position in the cut).
# ------------------------------------------------------------------------------------------------
_VERDICT_MAP = {"SAME": "SAME", "MOVED": "SHIFTED", "RETIMED": "CHANGED",
                "CHANGED": "CHANGED", "REVIEW": "CHANGED", "ADDED": "ADDED", "REMOVED": "REMOVED"}


def build_change_report(rows, len_by_tcid_a=None, len_by_tcid_b=None):
    """classify()-shaped rows (status, A_tcid, B_tcid, hamming, len_delta_frames, method) -> a
    list of tidy dicts: old_code, new_code, verdict, old_dur, new_dur, dur_delta.

    len_by_tcid_a / len_by_tcid_b: optional {tcid: length_frames} lookups, used as a fallback for
    old_dur/new_dur when a row's own len_delta_frames is None (e.g. ADDED/REMOVED/MOVED rows,
    which classify() doesn't compute a delta for). Pass {} or omit if lengths aren't available --
    durations are then left blank rather than guessed.
    """
    len_by_tcid_a = len_by_tcid_a or {}
    len_by_tcid_b = len_by_tcid_b or {}
    report = []
    for status, a_tcid, b_tcid, dist, dl, method in rows:
        verdict = _VERDICT_MAP.get(status, status)
        old_dur = len_by_tcid_a.get(a_tcid) if a_tcid else None
        new_dur = len_by_tcid_b.get(b_tcid) if b_tcid else None
        if dl is not None and old_dur is None and new_dur is not None:
            old_dur = new_dur - dl
        if dl is not None and new_dur is None and old_dur is not None:
            new_dur = old_dur + dl
        delta = dl
        if delta is None and old_dur is not None and new_dur is not None:
            delta = new_dur - old_dur
        report.append({
            "old_code": a_tcid or "", "new_code": b_tcid or "", "verdict": verdict,
            "old_dur": old_dur if old_dur is not None else "",
            "new_dur": new_dur if new_dur is not None else "",
            "dur_delta": delta if delta is not None else "",
        })
    return report


def format_change_summary(report):
    """One-screen stdout summary table: verdict counts + total/avg duration delta for CHANGED/
    SHIFTED rows. Pure string formatting, no I/O -- caller prints/logs the result."""
    tally = Counter(r["verdict"] for r in report)
    order = ["SAME", "SHIFTED", "CHANGED", "ADDED", "REMOVED"]
    lines = ["", "=== change report summary ===",
             f"{'verdict':<10}{'count':>7}"]
    for v in order:
        if tally.get(v):
            lines.append(f"{v:<10}{tally[v]:>7}")
    other = set(tally) - set(order)
    for v in sorted(other):
        lines.append(f"{v:<10}{tally[v]:>7}")
    lines.append(f"{'TOTAL':<10}{sum(tally.values()):>7}")
    deltas = [r["dur_delta"] for r in report if isinstance(r["dur_delta"], (int, float))]
    if deltas:
        lines.append(f"\nduration deltas (frames): n={len(deltas)}  "
                     f"sum={sum(deltas)}  avg={sum(deltas) / len(deltas):.1f}  "
                     f"min={min(deltas)}  max={max(deltas)}")
    return "\n".join(lines)


def write_change_report_csv(path, report):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["old_code", "new_code", "verdict", "old_dur", "new_dur", "dur_delta"])
        for r in report:
            w.writerow([r["old_code"], r["new_code"], r["verdict"],
                        r["old_dur"], r["new_dur"], r["dur_delta"]])


def cmd_compare(args):
    prefix = args.prefix
    top_keep = 1.0 if args.full_frame else (args.top_keep if args.top_keep is not None
                                             else TOP_KEEP_DEFAULT)
    crop_top = args.crop_top if args.crop_top is not None else CROP_TOP_DEFAULT

    print(f"[load] A={args.a}")
    A = load_cut(args.a, prefix, args.start_min, args.end_min, crop_top, top_keep)
    print(f"[load] B={args.b}")
    B = load_cut(args.b, prefix, args.start_min, args.end_min, crop_top, top_keep)
    print(f"[load] A: {len(A)} shots   B: {len(B)} shots")
    if args.hash_only or not A or not B:
        return []

    print("[match] building pHash + colour matrices ...")
    Dp, Dc = _hamming_matrix(A, B)
    import numpy as np
    darkA = np.array([s["dark"] for s in A]); darkB = np.array([s["dark"] for s in B])
    lvl = _match_levels(Dp, Dc, darkA, darkB, args.strong, args.near, args.cstrong, args.cnear)
    print("[align] Needleman-Wunsch ...")
    ops = _align(A, B, lvl)
    rows = classify(ops, A, B, Dp, lvl, args.strong, args.near, args.retime_tol)

    tally = Counter(r[0] for r in rows)
    print("[result] " + "  ".join(f"{k}={tally[k]}" for k in
          ("SAME", "RETIMED", "MOVED", "CHANGED", "REVIEW", "ADDED", "REMOVED")))

    out = args.out or os.path.join(args.b, f"cut_compare_{Path(args.a).name}_vs_{Path(args.b).name}.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "A_tcid", "B_tcid", "hamming", "len_delta_frames", "method"])
        w.writerows(rows)
    print(f"[done] wrote {out}  ({len(rows)} rows)")

    # FEATURE 4: tidy change-report CSV (old_code/new_code/verdict/old_dur/new_dur/dur_delta) +
    # one-screen stdout summary, alongside the raw status CSV above.
    len_a = {s["tcid"]: s.get("length") for s in A}
    len_b = {s["tcid"]: s.get("length") for s in B}
    report = build_change_report(rows, len_a, len_b)
    report_out = args.report_out or os.path.join(
        args.b, f"cut_compare_report_{Path(args.a).name}_vs_{Path(args.b).name}.csv")
    write_change_report_csv(report_out, report)
    print(f"[done] wrote {report_out}  ({len(report)} rows)")
    print(format_change_summary(report))
    return rows


# ================================================================================================
# 2. ASSIGN  -  the differentiator: global 1:1 tiered cross-cut matching
# ================================================================================================
# PRIORITY TIERS (assign greedily, highest tier first; each master code claimed at most once):
#   1. EXACT-CODE     new-cut shot's own code == an unclaimed master code. Strongest signal.
#   2. ORDINAL        within a (slate, take) group, pair remaining new-cut pieces to remaining
#                      master pieces IN COUNTER ORDER (1st->1st, 2nd->2nd...). Absorbs counter
#                      DRIFT (edit order shifts the counter between cuts) the way an operator
#                      does by hand -- never reorders a slate group by picture. Surplus on either
#                      side is left unmatched, not forced.
#   3. SLATE-ORDINAL   same, but matched on slate only (take labels differ for the same shot).
#   4. VISUAL          ONLY for shots with no usable slate (placeholders / postviz composites),
#                      or to break a genuine tie: CLIP cosine similarity (falls back to a
#                      Pillow aHash/dHash similarity if torch is unavailable). VISUAL MUST NOT
#                      override a shot's own slate -- a clear slate that disagrees with a
#                      look-alike wins, i.e. the shot is NEW, not force-matched.
#   5. LEFTOVER        new-cut shot whose slate's master pieces are all taken -> EXTRA (candidate
#                      new piece of an existing shot); no usable signal at all -> NEW. Unclaimed
#                      master shot, if NOT protected (see PROTECTED_MARKERS) -> OMIT candidate.
#
# The operator always confirms; this only proposes. When uncertain, flag -- never silently drop
# or silently force a match.

CODE_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9]+)_(?P<slate>\d+)_(?P<take>\d+)_(?P<counter>\d+)$")


def parse_code(code):
    """<PREFIX>_<slate>_<take>_<counter> -> (slate:int, take:int, counter:int) or None.
    Any alnum prefix is accepted (SHW_, or whatever the project uses) so this is prefix-agnostic."""
    m = CODE_RE.match(code or "")
    return (int(m.group("slate")), int(m.group("take")), int(m.group("counter"))) if m else None


def is_protected(revision_text):
    """True if a master row's revision/notes text marks it as a hand-added, non-editorial shot
    (invisible VFX with no slate: DMP / screen-replace / transition / muzzle flash / CG-only...).
    Such rows must NEVER be proposed as omit candidates, matched or not."""
    t = (revision_text or "").lower()
    return any(marker in t for marker in PROTECTED_MARKERS)


def _group_key(code, with_take=True):
    p = parse_code(code)
    if not p:
        return None
    return (p[0], p[1]) if with_take else (p[0],)


def assign(new_shots, master, sim=None, protected_rows=frozenset()):
    """Global 1:1 cross-cut assignment.

    new_shots: list of dicts {tcid, code, order}  (code = the new cut's own shot code;
               order = position in the new cut's running order, used as an ordinal tiebreak
               when the code itself can't be parsed)
    master:    list of dicts {row, code, revision}   (row = a stable identifier -- sheet row
               number, DB id, whatever the caller uses; revision = free text checked for
               PROTECTED_MARKERS)
    sim:       optional callable sim(tcid, master_row) -> float similarity in [0,1], used only
               in the VISUAL tier. Pass None to skip visual matching entirely (tiers 1-3 only).
    protected_rows: optional explicit set of master rows to protect from omission, IN ADDITION
               to rows whose `revision` text matches PROTECTED_MARKERS.

    Returns (matches, leftovers, omit):
      matches:   list of {tcid, master_row, master_code, tier}   -- each master_row appears
                 in at most ONE match (the 1:1 invariant; see uniqueness_check()).
      leftovers: list of {tcid, tier}         tier in {"EXTRA", "NEW"}
      omit:      list of master rows unclaimed and not protected
    """
    m_by_code = {m["code"]: m for m in master if m.get("code")}
    protected = set(protected_rows) | {m["row"] for m in master if is_protected(m.get("revision"))}
    taken = set()
    matched = {}   # tcid -> (row, code, tier)

    def claim(tcid, row, code, tier):
        matched[tcid] = (row, code, tier)
        taken.add(row)

    # ---- Tier 1: exact code ----
    for c in new_shots:
        m = m_by_code.get(c["code"])
        if m and m["row"] not in taken:
            claim(c["tcid"], m["row"], m["code"], "EXACT-CODE")

    # ---- Tier 2 (slate+take) then Tier 3 (slate only): ordinal alignment ----
    for with_take, tier in ((True, "ORDINAL"), (False, "SLATE-ORDINAL")):
        cg, mg = {}, {}
        for c in new_shots:
            if c["tcid"] in matched:
                continue
            k = _group_key(c["code"], with_take)
            if k and k[0]:
                cg.setdefault(k, []).append(c)
        for m in master:
            if m["row"] in taken:
                continue
            k = _group_key(m["code"], with_take)
            if k and k[0]:
                mg.setdefault(k, []).append(m)
        for k, cs in cg.items():
            ms = mg.get(k, [])
            if not ms:
                continue
            cs.sort(key=lambda x: parse_code(x["code"])[2] if parse_code(x["code"]) else x.get("order", 0))
            ms.sort(key=lambda x: parse_code(x["code"])[2])
            for c, m in zip(cs, ms):   # 1:1 in counter order -- absorbs drift, never reorders
                claim(c["tcid"], m["row"], m["code"], tier)

    # ---- Tier 4: visual (placeholders / no-slate shots only), never overriding a clear slate ----
    if sim is not None:
        avail = [m for m in master if m["row"] not in taken]
        for c in new_shots:
            if c["tcid"] in matched:
                continue
            if parse_code(c["code"]) and parse_code(c["code"])[0]:   # has its own slate -> skip
                continue
            best, best_score = None, -1.0
            for m in avail:
                if m["row"] in taken:
                    continue
                sv = sim(c["tcid"], m["row"])
                if sv is not None and sv > best_score:
                    best, best_score = m, sv
            if best is not None:
                claim(c["tcid"], best["row"], best["code"], "VISUAL")

    matches = [{"tcid": t, "master_row": r, "master_code": cc, "tier": ti}
               for t, (r, cc, ti) in matched.items()]

    leftovers = []
    for c in new_shots:
        if c["tcid"] in matched:
            continue
        p = parse_code(c["code"])
        slate_has_master = bool(p) and any(parse_code(m["code"]) and parse_code(m["code"])[0] == p[0]
                                            for m in master)
        leftovers.append({"tcid": c["tcid"], "tier": "EXTRA" if slate_has_master else "NEW"})

    omit = [m["row"] for m in master if m["row"] not in taken and m["row"] not in protected]
    return matches, leftovers, omit


def uniqueness_check(matches):
    """FAIL LOUDLY if any proposed master_code (or master_row) was claimed more than once.
    Returns () on success, or raises AssertionError describing the collision(s) -- call sites
    should let this propagate; a collision means the 1:1 invariant broke and results must not
    be trusted."""
    row_counts = Counter(m["master_row"] for m in matches)
    dupe_rows = {r: n for r, n in row_counts.items() if n > 1}
    code_counts = Counter(m["master_code"] for m in matches if m["master_code"])
    dupe_codes = {c: n for c, n in code_counts.items() if n > 1}
    if dupe_rows or dupe_codes:
        raise AssertionError(
            f"1:1 ASSIGNMENT INVARIANT VIOLATED -- duplicate master rows: {dupe_rows}; "
            f"duplicate master codes: {dupe_codes}. This must never happen; treat the whole "
            f"assignment as untrustworthy and fix the tier logic before using it.")
    return ()


def crosscheck_new_against_master(leftovers, new_shots_by_tcid, master):
    """Master cross-check before trusting a NEW leftover: if the shot's OWN code equals an
    exact unclaimed... no -- ANY master code (claimed or not), the tier-1 pass should already
    have caught it, so a NEW/EXTRA leftover whose own code exactly matches a master code means
    the engine missed it (a bug) rather than a genuinely new shot. Returns a list of leftover
    dicts augmented with a 'note' field: 'engine-missed-exact-code' / 'counter-drift-candidate'
    / 'genuinely-new'."""
    codes = {m["code"]: m for m in master if m.get("code")}
    out = []
    for lo in leftovers:
        c = new_shots_by_tcid.get(lo["tcid"], {})
        code = c.get("code")
        note = "genuinely-new"
        if code and code in codes:
            note = "engine-missed-exact-code"
        elif code and parse_code(code):
            slate = parse_code(code)[0]
            if any(parse_code(m["code"]) and parse_code(m["code"])[0] == slate for m in master):
                note = "counter-drift-candidate" if lo["tier"] == "EXTRA" else "extra-piece-of-known-slate"
        out.append({**lo, "note": note})
    return out


# ---- visual similarity backends (CLIP optional, Pillow hash fallback) ----

def build_similarity_fn(new_thumb_path, master_thumb_path):
    """Return a sim(tcid, master_row) -> float[0,1] callable, or None if neither backend is
    available. Tries CLIP (torch + transformers) first for real semantic similarity; degrades
    to a Pillow aHash/dHash Hamming-based similarity if torch/transformers/CLIP weights aren't
    available, printing a clear log line either way. Both backends cache embeddings/hashes on
    first use per path.

    new_thumb_path(tcid) -> path-like | None
    master_thumb_path(row) -> path-like | None
    """
    try:
        return _clip_similarity_fn(new_thumb_path, master_thumb_path)
    except Exception as e:
        print(f"[assign] CLIP unavailable ({e.__class__.__name__}: {e}); "
              f"falling back to Pillow aHash/dHash similarity for the VISUAL tier.", flush=True)
    try:
        return _hash_similarity_fn(new_thumb_path, master_thumb_path)
    except Exception as e:
        print(f"[assign] Pillow hash fallback also unavailable ({e}); "
              f"VISUAL tier will be skipped entirely.", flush=True)
        return None


def _clip_similarity_fn(new_thumb_path, master_thumb_path):
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(device).eval()
    proc = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    print(f"[assign] VISUAL tier: CLIP ViT-B/32 on {device}", flush=True)
    cache = {}

    @torch.no_grad()
    def _embed(path):
        if path is None:
            return None
        key = str(path)
        if key in cache:
            return cache[key]
        if not os.path.exists(key):
            cache[key] = None
            return None
        img = Image.open(key).convert("RGB")
        inp = proc(images=[img], return_tensors="pt").to(device)
        vis = model.vision_model(pixel_values=inp["pixel_values"])
        f = model.visual_projection(vis.pooler_output)
        f = torch.nn.functional.normalize(f, dim=-1).cpu().numpy()[0]
        cache[key] = f
        return f

    def sim(tcid, master_row):
        a = _embed(new_thumb_path(tcid))
        b = _embed(master_thumb_path(master_row))
        if a is None or b is None:
            return None
        return float(a @ b)   # cosine sim (both unit-norm)

    return sim


def _hash_similarity_fn(new_thumb_path, master_thumb_path):
    from PIL import Image
    print("[assign] VISUAL tier: Pillow aHash/dHash fallback (no torch/CLIP found)", flush=True)
    cache = {}

    def _hashes(path):
        if path is None:
            return None
        key = str(path)
        if key in cache:
            return cache[key]
        if not os.path.exists(key):
            cache[key] = None
            return None
        img = Image.open(key).convert("L")
        # aHash: 8x8 downsample, threshold at mean
        small = img.resize((8, 8))
        px = list(small.getdata())
        mean = sum(px) / len(px)
        ahash = [1 if p > mean else 0 for p in px]
        # dHash: 9x8 downsample, compare adjacent columns
        small2 = img.resize((9, 8))
        px2 = list(small2.getdata())
        dhash = []
        for row in range(8):
            base = row * 9
            for col in range(8):
                dhash.append(1 if px2[base + col] > px2[base + col + 1] else 0)
        cache[key] = (ahash, dhash)
        return cache[key]

    def _hamming_sim(a, b):
        dist = sum(x != y for x, y in zip(a, b))
        return 1.0 - (dist / len(a))

    def sim(tcid, master_row):
        a = _hashes(new_thumb_path(tcid))
        b = _hashes(master_thumb_path(master_row))
        if a is None or b is None:
            return None
        return 0.5 * _hamming_sim(a[0], b[0]) + 0.5 * _hamming_sim(a[1], b[1])

    return sim


# ---- CSV / sheet adapters for assign ----

def _read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_new_shots_csv(path):
    """Expected columns (by header, case/space-insensitive): tcid, code, order (order optional)."""
    rows = _read_csv_rows(path)
    hmap = _header_map(rows[0].keys() if rows else [])
    out = []
    for i, r in enumerate(rows):
        out.append({
            "tcid": r[hmap["tcid"]].strip(),
            "code": r.get(hmap.get("code", ""), "").strip() if "code" in hmap else "",
            "order": int(r[hmap["order"]]) if "order" in hmap and r.get(hmap["order"], "").strip().isdigit() else i,
        })
    return out


def load_master_csv(path):
    """Expected columns (by header): row (or an id-like column), code, revision (optional --
    free text checked for PROTECTED_MARKERS)."""
    rows = _read_csv_rows(path)
    hmap = _header_map(rows[0].keys() if rows else [])
    row_field = hmap.get("row") or hmap.get("id") or hmap.get("master_row")
    out = []
    for i, r in enumerate(rows):
        raw_row = r[row_field].strip() if row_field else str(i)
        out.append({
            "row": int(raw_row) if raw_row.isdigit() else raw_row,
            "code": r.get(hmap.get("code", ""), "").strip() if "code" in hmap else "",
            "revision": r.get(hmap.get("revision", ""), "") if "revision" in hmap else "",
        })
    return out


def _norm_header(s):
    return re.sub(r"\s+", "_", str(s).strip().lower())


def _header_map(headers):
    """{normalized_name: original_header} for header-name resolution (order-independent)."""
    return {_norm_header(h): h for h in headers}


def load_shots_from_sheet(args, columns):
    """Read rows from a Google Sheet tab via bs_gsheets' auth, resolving `columns` by header
    name. Returns a list of dicts keyed by the SAME field names passed in `columns`
    (field_name -> list of acceptable header texts), e.g.
      columns = {"tcid": ["TC_ID", "tcid"], "code": ["Shot Code"], "revision": ["Revision", "Notes"]}
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bs_gsheets as G

    sheets, _ = G.services(args)
    tab = args.tab
    meta = sheets.spreadsheets().values().get(
        spreadsheetId=args.sheet_id, range=f"'{tab}'!1:1").execute().get("values", [[]])
    header = meta[0] if meta else []
    if not header:
        sys.exit(f"ERROR: no header row in tab '{tab}'.")
    hmap = {G._norm(h): i for i, h in enumerate(header)}

    def col_for(field):
        for cand in columns[field]:
            if G._norm(cand) in hmap:
                return hmap[G._norm(cand)]
        return None

    cidx = {f: col_for(f) for f in columns}
    missing = [f for f, i in cidx.items() if i is None and f in ("tcid",)]
    if missing:
        sys.exit(f"ERROR: sheet is missing required column(s): {missing}")

    data = sheets.spreadsheets().values().get(
        spreadsheetId=args.sheet_id, range=f"'{tab}'!A2:ZZ").execute().get("values", [])
    out = []
    for r, row in enumerate(data, start=2):
        rec = {"row": r}
        for f, i in cidx.items():
            rec[f] = (row[i].strip() if i is not None and i < len(row) else "")
        out.append(rec)
    return out


# ------------------------------------------------------------------------------------------------
# FEATURE 3: the 1:1 match review gate, staged IN-SHEET (never applied to any master)
# ------------------------------------------------------------------------------------------------
# assign() only ever PROPOSES; an operator confirms before anything touches a master breakdown.
# --write-sheet stages that proposal directly in the review sheet's own review columns (Match Tier
# / Proposed Master Code / Match Note), created adjacent to each other at the right of the header
# row only if absent, and installs one conditional-formatting rule that turns duplicate values in
# the Proposed Master Code column red -- the same 1:1 uniqueness guardrail uniqueness_check()
# enforces in code, made visible to a human reviewing the sheet. THIS IS A STAGING WRITE ONLY: it
# writes to the three review columns and nothing else, and never writes to a master spreadsheet.

REVIEW_HEADERS = ["Match Tier", "Proposed Master Code", "Match Note"]


def _col_a1(idx):
    """0-based column index -> A1 letter(s)."""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def build_review_rows(matches, leftovers, new_by_tcid):
    """Pure logic: matches + leftovers (as returned by assign()/crosscheck_new_against_master())
    -> {tcid: {"Match Tier": ..., "Proposed Master Code": ..., "Match Note": ...}}, the exact
    values --write-sheet stages into the review columns. Shared by cmd_assign's CSV writer and
    the sheet writer so both paths agree byte-for-byte."""
    out = {}
    for m in matches:
        out[m["tcid"]] = {"Match Tier": m["tier"], "Proposed Master Code": m["master_code"],
                           "Match Note": ""}
    for lo in leftovers:
        out[lo["tcid"]] = {"Match Tier": lo["tier"], "Proposed Master Code": "",
                            "Match Note": lo.get("note", "")}
    return out


def plan_review_header_insert(header, headers_needed=REVIEW_HEADERS):
    """Pure logic: given an existing header row (list of cell strings), decide which of
    headers_needed are missing and return (new_header, col_index_map) where new_header is the
    header row AFTER appending any missing ones (adjacent to each other, at the right), and
    col_index_map is {header_text: 0-based col index} for ALL headers_needed (existing or new).
    Idempotent: headers already present are reused at their existing index, never duplicated."""
    hmap = {_norm_header(h): i for i, h in enumerate(header)}
    new_header = list(header)
    col = {}
    to_add = [h for h in headers_needed if _norm_header(h) not in hmap]
    for h in headers_needed:
        nh = _norm_header(h)
        if nh in hmap:
            col[h] = hmap[nh]
    start = len(new_header)
    for i, h in enumerate(to_add):
        col[h] = start + i
        new_header.append(h)
    return new_header, col


def build_duplicate_cf_rule(sheet_id, col_index, num_rows, header_row_count=1, rule_index=0):
    """One Sheets API conditionalFormatRule request body: duplicate values in the Proposed Master
    Code column (col_index, 0-based) turn red. Uses a CUSTOM_FORMULA with COUNTIF over the column
    range so it re-evaluates live as values change -- the visible uniqueness guardrail.
    Blank cells never count as a 'duplicate' (COUNTIF guarded with a not-blank check)."""
    col_a1 = _col_a1(col_index)
    top = header_row_count + 1
    bottom = header_row_count + max(num_rows, 1)
    # column-locked, row-relative reference: Sheets evaluates this once per cell in the rule's
    # range, sliding the (unlocked) row reference -- so every cell checks "does MY value repeat
    # anywhere in the whole column range".
    formula = (f'=AND({col_a1}{top}<>"", COUNTIF(${col_a1}${top}:${col_a1}${bottom}, '
               f'{col_a1}{top})>1)')
    return {
        "addConditionalFormatRule": {
            "index": rule_index,
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": header_row_count,
                            "endRowIndex": bottom, "startColumnIndex": col_index,
                            "endColumnIndex": col_index + 1}],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.65, "blue": 0.65}},
                },
            },
        }
    }


def write_match_proposal_to_sheet(sheets, spreadsheet_id, tab, matches, leftovers, new_by_tcid,
                                   tcid_header_candidates=("TCID", "TC_ID")):
    """THE GATE: stages a match proposal into a review sheet's own Match Tier / Proposed Master
    Code / Match Note columns (created adjacent to each other at the right of the header row only
    if absent), keyed on the sheet's own TCID/TC_ID column, and installs/refreshes ONE duplicate-
    guard conditional-formatting rule on Proposed Master Code. Idempotent: safe to re-run.

    NEVER writes to any other column, and NEVER touches a master spreadsheet -- this only stages
    a proposal in the sheet passed as (spreadsheet_id, tab) for a human to review and apply by
    hand (or via a separate, explicit "apply" step outside this function, which does not exist
    here by design).

    Returns a summary dict: {rows_written, headers_added, cf_installed}.
    """
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title))").execute()
    sheet_id = None
    for sh in meta.get("sheets", []):
        if sh["properties"]["title"] == tab:
            sheet_id = sh["properties"]["sheetId"]
            break
    if sheet_id is None:
        sys.exit(f"ERROR: tab '{tab}' not found in spreadsheet {spreadsheet_id}")

    header = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!1:1").execute().get("values", [[]])
    header = header[0] if header else []
    if not header:
        sys.exit(f"ERROR: no header row in tab '{tab}'.")
    hmap = {_norm_header(h): i for i, h in enumerate(header)}
    tcid_col = next((hmap[_norm_header(c)] for c in tcid_header_candidates
                     if _norm_header(c) in hmap), None)
    if tcid_col is None:
        sys.exit(f"ERROR: sheet has no TCID/TC_ID column to key the review write on "
                 f"(looked for: {tcid_header_candidates}).")

    new_header, col = plan_review_header_insert(header)
    headers_added = [h for h in REVIEW_HEADERS if h not in header]
    if headers_added:
        pad = [""] * (len(new_header) - len(header))
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"'{tab}'!1:1",
            valueInputOption="RAW", body={"values": [header + pad]}).execute()
        for h in headers_added:
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id, range=f"'{tab}'!{_col_a1(col[h])}1",
                valueInputOption="RAW", body={"values": [[h]]}).execute()

    data_rows = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!A2:ZZ").execute().get("values", [])
    row_of_tcid = {}
    for r, row in enumerate(data_rows, start=2):
        v = row[tcid_col] if tcid_col < len(row) else ""
        if str(v).strip():
            row_of_tcid[str(v).strip()] = r

    review = build_review_rows(matches, leftovers, new_by_tcid)

    updates = []
    rows_written = 0
    for tcid, vals in review.items():
        row = row_of_tcid.get(str(tcid).strip())
        if row is None:
            continue  # this shot isn't in the review sheet at all -- nothing to stage against
        for h in REVIEW_HEADERS:
            updates.append({"range": f"'{tab}'!{_col_a1(col[h])}{row}", "values": [[vals[h]]]})
        rows_written += 1
    if updates:
        BATCH = 500
        for i in range(0, len(updates), BATCH):
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates[i:i + BATCH]}).execute()

    # idempotent CF install: a "duplicate guard" rule is identified structurally (a boolean CF
    # rule whose range starts at the Proposed Master Code column on this sheet), not by formula
    # text -- so a prior run's rule is always found and replaced, never stacked. Delete existing
    # matches first (descending index so earlier deletes don't shift later indices), then add one
    # fresh rule sized to the current data range.
    existing_meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId),conditionalFormats)").execute()
    requests = []
    for sh in existing_meta.get("sheets", []):
        if sh["properties"]["sheetId"] != sheet_id:
            continue
        for i, rule in enumerate(sh.get("conditionalFormats", [])):
            if any(rr.get("startColumnIndex") == col["Proposed Master Code"]
                   and rr.get("endColumnIndex") == col["Proposed Master Code"] + 1
                   for rr in rule.get("ranges", [])):
                requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}})
    requests.sort(key=lambda r: -r["deleteConditionalFormatRule"]["index"])
    num_rows = max(len(data_rows), 1)
    cf_req = build_duplicate_cf_rule(sheet_id, col["Proposed Master Code"], num_rows,
                                     header_row_count=1, rule_index=0)
    requests.append(cf_req)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

    return {"rows_written": rows_written, "headers_added": headers_added, "cf_installed": True}


def cmd_assign(args):
    new_shots = (load_shots_from_sheet(args, {"tcid": ["TCID", "TC_ID"], "code": ["Shot Code", "Code"]})
                 if args.new_sheet_id else load_new_shots_csv(args.new))
    master = (load_shots_from_sheet(args, {"tcid": ["TCID", "TC_ID"], "code": ["Shot Code", "Code"],
                                            "revision": ["Revision", "Notes"]})
              if args.master_sheet_id else load_master_csv(args.master))
    if args.new_sheet_id:
        args.sheet_id, args.tab = args.new_sheet_id, args.new_tab

    sim = None
    if args.visual:
        thumbs_new = Path(args.new_thumbs) if args.new_thumbs else None
        thumbs_master = Path(args.master_thumbs) if args.master_thumbs else None
        if thumbs_new and thumbs_master:
            def new_path(tcid):
                return thumbs_new / f"{args.prefix}_{tcid}-mid.jpg"

            def master_path(row):
                m = next((m for m in master if m["row"] == row), None)
                if not m or not m.get("tcid"):
                    return None
                return thumbs_master / f"{args.prefix}_{m['tcid']}-mid.jpg"
            sim = build_similarity_fn(new_path, master_path)
        else:
            print("[assign] --visual set but --new-thumbs/--master-thumbs not both given; "
                  "skipping VISUAL tier.", flush=True)

    protected_rows = set()
    if args.protected:
        protected_rows = {int(x) if str(x).isdigit() else x
                           for x in json.load(open(args.protected)).get("protected_rows", [])}

    matches, leftovers, omit = assign(new_shots, master, sim=sim, protected_rows=protected_rows)
    uniqueness_check(matches)   # raises loudly on any collision

    new_by_tcid = {c["tcid"]: c for c in new_shots}
    leftovers = crosscheck_new_against_master(leftovers, new_by_tcid, master)

    tier_tally = Counter(m["tier"] for m in matches)
    leftover_tally = Counter(l["tier"] for l in leftovers)
    print(f"[assign] {len(new_shots)} new-cut shots, {len(master)} master shots")
    print("[assign] matched: " + ", ".join(f"{k}={v}" for k, v in sorted(tier_tally.items())))
    print("[assign] leftover: " + ", ".join(f"{k}={v}" for k, v in sorted(leftover_tally.items())))
    print(f"[assign] omit candidates (unclaimed, unprotected): {len(omit)}")

    out = args.out or "bs_match_assign.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["new_tcid", "new_code", "tier", "proposed_master_code", "note"])
        for m in matches:
            w.writerow([m["tcid"], new_by_tcid.get(m["tcid"], {}).get("code", ""),
                        m["tier"], m["master_code"], ""])
        for lo in leftovers:
            w.writerow([lo["tcid"], new_by_tcid.get(lo["tcid"], {}).get("code", ""),
                        lo["tier"], "", lo["note"]])
    print(f"[done] wrote {out}  ({len(matches) + len(leftovers)} rows)")

    if omit:
        omit_out = args.omit_out or "bs_match_omit_candidates.csv"
        with open(omit_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["master_row", "master_code"])
            code_by_row = {m["row"]: m["code"] for m in master}
            for row in omit:
                w.writerow([row, code_by_row.get(row, "")])
        print(f"[done] wrote {omit_out}  ({len(omit)} omit candidates -- operator must confirm)")

    if getattr(args, "write_sheet", None):
        print("\n" + "=" * 78)
        print("THE GATE: --write-sheet stages this proposal into the review sheet's own")
        print("Match Tier / Proposed Master Code / Match Note columns ONLY. Nothing is applied")
        print("to any master breakdown. An operator must review and apply changes by hand.")
        print("=" * 78, flush=True)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import bs_gsheets as G
        sheets, _ = G.services(args)
        write_tab = args.write_sheet_tab or args.tab
        result = write_match_proposal_to_sheet(sheets, args.write_sheet, write_tab,
                                               matches, leftovers, new_by_tcid)
        if result["headers_added"]:
            print(f"[write-sheet] created review header(s): {result['headers_added']}", flush=True)
        print(f"[write-sheet] staged {result['rows_written']} row(s) into "
              f"'{write_tab}' -- Match Tier / Proposed Master Code / Match Note", flush=True)
        print(f"[write-sheet] duplicate-guard conditional format installed on "
              f"'Proposed Master Code' -- NOTHING WAS APPLIED TO ANY MASTER SHEET.", flush=True)

    return matches, leftovers, omit


# ================================================================================================
# 3. AUDIT  -  invisible-VFX audit (OCR ground truth vs operator sheet flags)
# ================================================================================================
# A row is a VFX shot ONLY if (a) the OCR burn-in note says so (ground truth = the OCR export
# CSV's is_vfx column -- NEVER the sheet's own note cell, which can hold manual/free text that
# looks similar but isn't the same signal) OR (b) the operator flagged it match/new in the
# sheet. The interesting output is the shots the OPERATOR flagged with NO OCR note behind them:
# those are the operator's manual/invisible-VFX identifications (camera shake, muzzle flash,
# morphcut, set extension, stabilize, transitions...) that no burn-in would ever catch.

def load_ocr_csv(path):
    """tcid -> is_vfx (bool) from an OCR export CSV with columns tcid, vfx_note, is_vfx
    (is_vfx: '1'/'0', 'true'/'false', or any truthy/falsy string)."""
    rows = _read_csv_rows(path)
    hmap = _header_map(rows[0].keys() if rows else [])
    tcid_h, note_h, isvfx_h = hmap.get("tcid"), hmap.get("vfx_note"), hmap.get("is_vfx")
    if not tcid_h or not isvfx_h:
        sys.exit("ERROR: OCR CSV must have 'tcid' and 'is_vfx' columns.")
    out = {}
    for r in rows:
        tcid = r[tcid_h].strip()
        val = str(r.get(isvfx_h, "")).strip().lower()
        out[tcid] = {"is_vfx": val in ("1", "true", "yes", "y"),
                     "note": r.get(note_h, "") if note_h else ""}
    return out


def load_sheet_flags_csv(path):
    """tcid -> operator-flag bool from a sheet-export CSV. Accepts a 'flag' boolean-ish column,
    OR (status/match/new)-style columns -- anything truthy in a column named status/flag/match/
    new (case-insensitive) counts as an operator flag."""
    rows = _read_csv_rows(path)
    hmap = _header_map(rows[0].keys() if rows else [])
    tcid_h = hmap.get("tcid")
    if not tcid_h:
        sys.exit("ERROR: sheet-flags CSV must have a 'tcid' column.")
    flag_candidates = [h for norm, h in hmap.items()
                        if norm in ("flag", "status", "match", "new", "vfx_flag", "operator_flag")]
    out = {}
    for r in rows:
        tcid = r[tcid_h].strip()
        flagged = any(str(r.get(h, "")).strip().lower() not in ("", "0", "false", "no", "n")
                      for h in flag_candidates)
        out[tcid] = flagged
    return out


def invisible_vfx_audit(ocr_rows, sheet_flags):
    """ocr_rows: {tcid: {"is_vfx": bool, "note": str}}  (from load_ocr_csv)
    sheet_flags: {tcid: bool}                            (from load_sheet_flags_csv)

    Returns dict with:
      is_vfx:            {tcid: bool}   final truth table (OCR OR operator flag)
      invisible_vfx:      list of tcid  operator-flagged, NO OCR note (manual/invisible VFX)
      ocr_only:           list of tcid  OCR says VFX, operator didn't flag (QC gap the other way)
      counts:             summary Counter
    """
    all_tcids = set(ocr_rows) | set(sheet_flags)
    is_vfx = {}
    invisible = []
    ocr_only = []
    for tcid in all_tcids:
        ocr = ocr_rows.get(tcid, {}).get("is_vfx", False)
        flag = sheet_flags.get(tcid, False)
        is_vfx[tcid] = ocr or flag
        if flag and not ocr:
            invisible.append(tcid)
        if ocr and not flag:
            ocr_only.append(tcid)
    counts = Counter({
        "total": len(all_tcids),
        "is_vfx": sum(is_vfx.values()),
        "ocr_confirmed": sum(1 for t in all_tcids if ocr_rows.get(t, {}).get("is_vfx", False)),
        "operator_only_invisible": len(invisible),
        "ocr_only_unflagged": len(ocr_only),
    })
    return {"is_vfx": is_vfx, "invisible_vfx": sorted(invisible), "ocr_only": sorted(ocr_only),
            "counts": counts}


def cmd_audit(args):
    ocr_rows = load_ocr_csv(args.ocr)
    sheet_flags = load_sheet_flags_csv(args.sheet)
    result = invisible_vfx_audit(ocr_rows, sheet_flags)

    c = result["counts"]
    print(f"[audit] {c['total']} shots total: is_vfx={c['is_vfx']} "
          f"(ocr_confirmed={c['ocr_confirmed']}, operator_only_invisible={c['operator_only_invisible']}, "
          f"ocr_only_unflagged={c['ocr_only_unflagged']})")

    out = args.out or "bs_match_invisible_vfx.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["tcid", "category"])
        for t in result["invisible_vfx"]:
            w.writerow([t, "operator_only_invisible"])
        for t in result["ocr_only"]:
            w.writerow([t, "ocr_only_unflagged"])
    print(f"[done] wrote {out} "
          f"({len(result['invisible_vfx'])} invisible-VFX + {len(result['ocr_only'])} ocr-only rows)")
    return result


# ================================================================================================
# 4. FPSCHECK  -  FPS-mismatch guard between two cuts
# ================================================================================================
# Any frame-index-based comparison (perceptual-hash sampling by frame index, tcid-from-timecode
# math, frame_correspondence-style slope/speed analysis) silently assumes both cuts share one
# FPS. If cut A was exported at 24fps and cut B at 23.976 (or 25, or a VFR export), frame indices
# drift against wall-clock time across the runtime and:
#   - tcid math (HH:MM:SS:FF) computed at the wrong fps assigns the WRONG frame number,
#     desyncing every downstream tcid-keyed join silently (no error, just wrong matches),
#   - a frame_correspondence-style slope test reads a pure FPS ratio as a false SPEED-RETIME
#     verdict, when nothing about the edit actually changed speed.
# This is a read-only guard: probe both movies' FPS via ffprobe and refuse to say "safe" unless
# they match within a tight tolerance.

def probe_fps(movie_path, ffprobe="ffprobe"):
    import subprocess
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=r_frame_rate", "-of", "csv=p=0", str(movie_path)],
            capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None   # ffprobe not found / not runnable -- unknown, not a crash
    try:
        num, den = out.split("/")
        return float(num) / float(den)
    except Exception:
        return None


def fps_mismatch(fps_a, fps_b, tol=0.05):
    """True if the two fps values differ by more than `tol` (absolute, in fps units).
    24 vs 23.976 (delta 0.024) is within tolerance by default; 24 vs 25 (delta 1.0) is not."""
    if fps_a is None or fps_b is None:
        return None   # could not determine -- caller should treat as unknown, not "safe"
    return abs(fps_a - fps_b) > tol


def cmd_fpscheck(args):
    ffprobe = args.ffprobe or os.environ.get("BS_FFPROBE", "ffprobe")
    fps_a = probe_fps(args.a, ffprobe)
    fps_b = probe_fps(args.b, ffprobe)
    print(f"[fpscheck] A={args.a} -> {fps_a}")
    print(f"[fpscheck] B={args.b} -> {fps_b}")
    mismatch = fps_mismatch(fps_a, fps_b, args.tol)
    if mismatch is None:
        print("[fpscheck] UNKNOWN -- could not read fps from one or both movies via ffprobe. "
              "Do NOT assume frame-based comparison is safe; verify manually.")
        return 2
    if mismatch:
        print(f"[fpscheck] MISMATCH -- A={fps_a:.3f}fps vs B={fps_b:.3f}fps "
              f"(delta {abs(fps_a - fps_b):.3f} > tol {args.tol}). Any frame-index-based "
              f"comparison (cut_compare tcid join, frame_correspondence slope) between these "
              f"two cuts WILL silently misalign. Re-derive tcids at each cut's own fps before "
              f"comparing, or treat slope/speed verdicts as suspect.")
        return 1
    print(f"[fpscheck] OK -- fps match within tolerance ({fps_a:.3f} vs {fps_b:.3f}).")
    return 0


# ================================================================================================
# CLI
# ================================================================================================

def _add_sheet_args(p, prefix=""):
    p.add_argument(f"--{prefix}sheet-id" if prefix else "--sheet-id", dest=f"{prefix}sheet_id".replace("-", "_"), default=None,
                   help="Google Sheet ID (use instead of a CSV; reads via bs_gsheets auth)")
    p.add_argument(f"--{prefix}tab" if prefix else "--tab", dest=f"{prefix}tab".replace("-", "_"), default="Sheet1")
    p.add_argument("--client-secret", default="")
    p.add_argument("--token", default="")
    p.add_argument("--service-account", default="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # compare
    p = sub.add_parser("compare", help="diff two cuts' shot lists (perceptual hash + duration)")
    p.add_argument("--a", required=True, help="cut A output dir (older cut; has thumbs/ + scenes/)")
    p.add_argument("--b", required=True, help="cut B output dir (newer cut)")
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--start-min", type=float, default=None)
    p.add_argument("--end-min", type=float, default=None)
    p.add_argument("--strong", type=int, default=12, help="pHash hamming <= -> SAME tier")
    p.add_argument("--near", type=int, default=40, help="pHash hamming <= -> NEAR/CHANGED tier")
    p.add_argument("--cstrong", type=float, default=0.0, help="colour dist <= -> SAME tier (0=off)")
    p.add_argument("--cnear", type=float, default=0.0, help="colour dist <= -> NEAR tier (0=off)")
    p.add_argument("--retime-tol", type=int, default=2)
    p.add_argument("--out", default=None)
    p.add_argument("--report-out", default=None,
                   help="tidy change-report CSV path (old_code,new_code,verdict,old_dur,new_dur,"
                        "dur_delta); default: cut_compare_report_<A>_vs_<B>.csv next to --b")
    p.add_argument("--hash-only", action="store_true")
    p.add_argument("--full-frame", action="store_true", help="hash the whole frame (both cuts subtitle-free)")
    p.add_argument("--top-keep", type=float, default=None)
    p.add_argument("--crop-top", type=float, default=None)
    p.set_defaults(fn=cmd_compare)

    # assign
    p = sub.add_parser("assign", help="1:1 tiered cross-cut shot assignment (the differentiator)")
    p.add_argument("--new", default=None, help="new-cut shots CSV (tcid, code[, order])")
    p.add_argument("--master", default=None, help="master shots CSV (row, code[, revision])")
    p.add_argument("--new-sheet-id", dest="new_sheet_id", default=None)
    p.add_argument("--new-tab", dest="new_tab", default="Sheet1")
    p.add_argument("--master-sheet-id", dest="master_sheet_id", default=None)
    p.add_argument("--client-secret", default="")
    p.add_argument("--token", default="")
    p.add_argument("--service-account", default="")
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--visual", action="store_true", help="enable the VISUAL tier (CLIP, or hash fallback)")
    p.add_argument("--new-thumbs", default=None, help="new-cut thumbs dir (for --visual)")
    p.add_argument("--master-thumbs", default=None, help="master reference thumbs dir (for --visual)")
    p.add_argument("--protected", default=None, help="JSON file with {\"protected_rows\": [...]} ")
    p.add_argument("--out", default=None)
    p.add_argument("--omit-out", default=None)
    p.add_argument("--tab", default="Sheet1", help="tab name used by --new-sheet-id / --write-sheet")
    p.add_argument("--write-sheet", dest="write_sheet", default=None, metavar="SPREADSHEET_ID",
                   help="STAGE this proposal into a review sheet's Match Tier / Proposed Master "
                        "Code / Match Note columns (created if absent) + install a duplicate-value "
                        "guard on Proposed Master Code. THE GATE: this is a staging write ONLY -- "
                        "it never touches any other column and NEVER applies anything to a master "
                        "breakdown. An operator must review the staged proposal and apply it by "
                        "hand. Idempotent: safe to re-run (headers/CF rule are not duplicated).")
    p.add_argument("--write-sheet-tab", dest="write_sheet_tab", default=None,
                   help="tab within --write-sheet to stage into (default: --tab)")
    p.set_defaults(fn=cmd_assign)

    # audit
    p = sub.add_parser("audit", help="invisible-VFX audit: OCR ground truth vs operator sheet flags")
    p.add_argument("--ocr", required=True, help="OCR export CSV (tcid, vfx_note, is_vfx)")
    p.add_argument("--sheet", required=True, help="sheet-export CSV (tcid + a flag/status/match/new column)")
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_audit)

    # fpscheck
    p = sub.add_parser("fpscheck", help="warn if two cuts were exported at different FPS")
    p.add_argument("a", help="cut A movie file")
    p.add_argument("b", help="cut B movie file")
    p.add_argument("--tol", type=float, default=0.05, help="fps delta tolerance (default 0.05)")
    p.add_argument("--ffprobe", default=None)
    p.set_defaults(fn=cmd_fpscheck)

    args = ap.parse_args()
    result = args.fn(args)
    if args.cmd == "fpscheck":
        sys.exit(result)


if __name__ == "__main__":
    main()
