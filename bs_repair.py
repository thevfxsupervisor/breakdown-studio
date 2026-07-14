#!/usr/bin/env python
"""bs_repair.py - detection-repair module for Breakdown Studio (operator fixes for over-splits
and missed cuts), generalized from the studio's proven split_shot.py / merge_shots.py /
rethumb_shot.py / apply_boundary_corrections.py scripts.

Self-contained like bs_worker.py: depends only on Pillow + an ffmpeg/ffprobe binary for the
filesystem-only path. Google Sheets interaction is OPTIONAL - every function works purely on the
local scenes CSV + frames/thumbs/cuts directories if no sheet is configured, and only calls into
Sheets when a `sheet` config dict (spreadsheet id + tab) is supplied. Never touches a master sheet,
only the cut breakdown sheet passed in.

Stages (mirrors the proven project tools 1:1, generalized):
  split         split one detected shot into two at a clip-relative frame (or N-1 frames for an
                N-way split); re-extracts frames/thumbs for every resulting piece, rewrites the
                scenes CSV, and (if a sheet is configured) updates the first row + inserts the rest.
  merge         merge adjacent over-split shots into one; recomputes duration/frames/thumbs,
                deletes the absorbed sheet rows. Cut-clip re-cut is best-effort/non-fatal.
  rethumb       regenerate a shot's start/mid/end frames + thumbnails frame-accurately after any
                boundary change (used internally by split/merge, also usable standalone).
  apply-ledger  replay a persistent ledger of operator-confirmed missed-cut corrections (see
                known_corrections.example.json) bottom-to-top so row inserts never shift a
                still-pending target's row number.

ALL operations are DRY-RUN by default; pass --apply (or apply=True) to write anything.

  python bs_repair.py split  --shot SHW_00091413 --at-frame 14 [--apply]
  python bs_repair.py split  --shot SHW_00091413 --at-frame 14 40 [--apply]     # split into 3
  python bs_repair.py merge  --keep SHW_00064316 --absorb SHW_00064412 SHW_00064809 [--apply]
  python bs_repair.py rethumb --shot SHW_00091413 [--positions start mid end] [--apply]
  python bs_repair.py apply-ledger --ledger corrections.json [--apply]

Config keys consumed (see config.example.json): output_base, prefix, ffmpeg, ffprobe, encoder,
show_tc_offset, spreadsheet_id, sheet_tab. Sheet keys are optional; omit them to run filesystem-only.
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FFMPEG = os.environ.get("BS_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("BS_FFPROBE", "ffprobe")
THUMB_W, THUMB_H = 480, 270
THUMB_QUALITY = 88
DEFAULT_PREFIX = "SHW"

# Columns the sheet layer manages directly. Resolved BY HEADER NAME at call time (never by
# letter), so a reordered/renamed sheet keeps working. Extend via config if a project's sheet
# uses different header text - this module never hardcodes a client-specific header set.
SHEET_COLUMNS = ["Status", "Scene", "Shot Code", "VFX Notes", "Thumbnail", "Frame",
                 "TC In", "TC Out", "Dur (f)", "TC_ID", "Seq",
                 "url_start", "url_mid", "url_end"]
# Alias groups: header text may drift between cuts (renames). Same pattern as the project's
# shot_breakdown_pipeline.header_map - kept local here so this module has zero coupling to it.
COLUMN_ALIASES = [
    {"Note", "Status"},
    {"Scene", "Suggested Scene"},
    {"File TC In", "TC In"},
    {"File TC Out", "TC Out"},
    {"File TC_ID", "TC_ID"},
]


# =============================================================================================
# small pure-logic helpers (unit-tested in tests/test_repair.py)
# =============================================================================================

def norm_tcid(x):
    """Normalize a TC_ID cell that may be stored zero-padded text ('00400813') or a plain
    number ('400813', leading zeros dropped by Sheets number formatting). Format-agnostic key."""
    s = str(x).strip()
    return s.lstrip("0") or "0"


def tc_to_seconds(tc):
    """'HH:MM:SS.mmm' or 'HH:MM:SS:FF'-as-decimal -> float seconds."""
    parts = str(tc).strip().lstrip("'").split(":")
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def sec_to_tc(sec):
    """float seconds -> 'HH:MM:SS.mmm' (scenes-CSV timecode format)."""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def sec_to_smpte(sec, fps):
    """float seconds -> 'HH:MM:SS:FF' (sheet TC In/Out display format)."""
    fps_i = round(fps)
    total = round(sec * fps)
    f = int(total % fps_i)
    t = total // fps_i
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}:{f:02d}"


def smpte_to_frames(tc, fps):
    """'HH:MM:SS:FF' -> absolute frame count at fps."""
    parts = str(tc).strip().lstrip("'").split(":")
    h, m, s, f = (int(p) for p in parts[:4])
    return ((h * 3600 + m * 60 + s) * round(fps)) + f


def frames_to_smpte(frames, fps):
    """absolute frame count -> 'HH:MM:SS:FF'."""
    fps_i = round(fps)
    frames = max(0, int(frames))
    f = frames % fps_i
    t = frames // fps_i
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}:{f:02d}"


def tcid_of(code_or_tcid):
    """Pull the 8-digit tcid out of a shot code like 'SHW_00091413', or pass through a bare
    tcid/number string unchanged if no 8-digit run is found."""
    m = re.search(r"(\d{8})", str(code_or_tcid))
    return m.group(1) if m else str(code_or_tcid)


def burnin_to_file_tc(burnin_tc, show_tc_offset, fps):
    """Convert an operator-read BURN-IN (show) timecode to file-relative TC, given the
    configured constant offset (show_TC = file_TC + offset  =>  file_TC = show_TC - offset).

    burnin_tc / show_tc_offset: 'HH:MM:SS:FF' strings. Returns 'HH:MM:SS:FF'.
    Raises ValueError if the subtraction would go negative (offset misconfigured or burn-in
    predates the file - almost always an operator-input mistake worth surfacing, not clamping).
    """
    show_f = smpte_to_frames(burnin_tc, fps)
    off_f = smpte_to_frames(show_tc_offset, fps)
    file_f = show_f - off_f
    if file_f < 0:
        raise ValueError(f"burnin_to_file_tc: {burnin_tc} - offset {show_tc_offset} = negative "
                          f"file TC ({file_f} frames) - check show_tc_offset / burn-in reading")
    return frames_to_smpte(file_f, fps)


def header_map(hdr):
    """{header text -> 0-based column index}, augmented with COLUMN_ALIASES so a renamed header
    still resolves under its old name. Mirrors the project convention: ALWAYS resolve columns by
    header name, never by fixed letter - the sheet gets reordered/renamed between cuts."""
    H = {str(name).strip(): i for i, name in enumerate(hdr)}
    for grp in COLUMN_ALIASES:
        present = next((n for n in grp if n in H), None)
        if present is not None:
            for n in grp:
                H.setdefault(n, H[present])
    return H


def col_letter(i):
    """0-based column index -> A1 letter(s)."""
    s = ""
    n = i + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def split_frames_decreasing(shot_len, at_frames):
    """Validate + sort split points for an N-way split (N-1 cut points inside a shot of
    shot_len frames), returning them in DECREASING order.

    Splitting must proceed at decreasing clip-relative frames: split_shot mutates the ORIGINAL
    scene's row in place for the 'keep' (first) piece and only ever inserts new rows AFTER it, so
    once the highest cut point is applied, all frame numbers below it are still valid indices into
    the (as yet unsplit) remainder. Applying low-to-high would invalidate every subsequent
    at_frame as soon as the first split shrinks the keep piece.
    """
    pts = sorted(set(int(x) for x in at_frames), reverse=True)
    if not pts:
        raise ValueError("split_frames_decreasing: at_frames is empty")
    for p in pts:
        if not (0 < p < shot_len):
            raise ValueError(f"split_frames_decreasing: at_frame {p} must be 0 < at < shot "
                              f"length {shot_len}")
    return pts


def split_piece_lengths(shot_len, at_frames):
    """N-1 sorted increasing cut points -> N piece lengths, in original (start..end) order."""
    pts = sorted(set(int(x) for x in at_frames))
    bounds = [0] + pts + [shot_len]
    return [b - a for a, b in zip(bounds, bounds[1:])]


def extract_positions(shot_len):
    """0-based (start, mid, end) frame indices within a shot of shot_len frames (mirrors
    bs_worker.shot_frame_indices, restated here for a clip-relative shot rather than CSV rows)."""
    return 0, (shot_len - 1) // 2, shot_len - 1


def frame_center_time(start_sec, k, fps):
    """Frame-accurate extract time for clip-relative 0-based frame k: aim at (k-0.5)/fps so an
    output-seek (which returns the first frame with PTS >= target) lands squarely on frame k
    without overshooting into the neighbour - the fix behind the studio's off-by-one lesson."""
    return start_sec + (k - 0.5) / fps


# =============================================================================================
# scenes CSV I/O (shared by split/merge/rethumb)
# =============================================================================================

def find_scenes_csv(scenes_dir):
    scenes_dir = Path(scenes_dir)
    if not scenes_dir.exists():
        return None
    cands = sorted(scenes_dir.glob("*Scenes*.csv"))
    return cands[0] if cands else None


def parse_scenes_csv(csv_path):
    scenes = []
    with open(csv_path, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if row and row[0].strip() == "Scene Number":
                header = [c.strip() for c in row]
                break
        if header is None:
            raise ValueError(f"No 'Scene Number' header row in {csv_path}")
        for row in reader:
            if not row or not row[0].strip().isdigit():
                continue
            d = dict(zip(header, [c.strip() for c in row]))
            scenes.append({
                "scene": int(d["Scene Number"]),
                "start_frame": int(d["Start Frame"]),
                "start_tc": d["Start Timecode"],
                "start_time": d["Start Time (seconds)"],
                "end_frame": int(d["End Frame"]),
                "end_tc": d["End Timecode"],
                "end_time": d["End Time (seconds)"],
                "len_frames": int(d["Length (frames)"]),
                "len_tc": d["Length (timecode)"],
                "duration_s": float(d["Length (seconds)"]),
            })
    return scenes


def tc_to_id(tc, fps):
    """'HH:MM:SS.mmm' -> HHMMSSFF tcid (matches bs_worker.tc_to_id)."""
    parts = str(tc).split(":")
    h, m = int(parts[0]), int(parts[1])
    sec_frac = float(parts[2])
    s = int(sec_frac)
    ff = int(round((sec_frac - s) * fps))
    if ff >= round(fps):
        ff = int(round(fps)) - 1
    return f"{h:02d}{m:02d}{s:02d}{ff:02d}"


def get_fps(movie):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(movie)],
                         capture_output=True, text=True).stdout.strip()
    try:
        num, den = out.split("/")
        return float(num) / float(den)
    except Exception:
        return 24.0


def _csv_rewrite_lines(csv_path):
    """Split a scenes CSV into (preamble/header-through-cols-line, data-lines). Backs up first."""
    shutil.copyfile(csv_path, str(csv_path) + ".bak")
    lines = Path(csv_path).read_text(encoding="utf-8", errors="replace").splitlines()
    hi = next(i for i, l in enumerate(lines) if l.startswith("Scene Number"))
    return lines[:hi + 1], lines[hi + 1:]


def rewrite_csv_split(csv_path, target_scene, pieces, fps):
    """Replace `target_scene`'s CSV row with len(pieces) rows (in order), renumbering 1..N.

    pieces: list of dicts with start_frame/start_tc/start_time/end_frame/end_tc/end_time/
    len_frames/len_tc/len_time (strings/ints as they'd appear in the CSV columns), in
    chronological order. Returns the new head+rows text that was written (for testing).
    """
    head, data = _csv_rewrite_lines(csv_path)
    out_rows = []
    for l in data:
        if not l.strip():
            continue
        c = next(csv.reader([l]))
        if not c[0].strip().isdigit():
            out_rows.append(c)
            continue
        if int(c[0]) != target_scene:
            out_rows.append(c)
            continue
        for p in pieces:
            row = list(c)
            row[1] = str(p["start_frame"]); row[2] = p["start_tc"]; row[3] = str(p["start_time"])
            row[4] = str(p["end_frame"]); row[5] = p["end_tc"]; row[6] = str(p["end_time"])
            row[7] = str(p["len_frames"]); row[8] = p["len_tc"]; row[9] = str(p["len_time"])
            out_rows.append(row)
    for i, c in enumerate(out_rows, start=1):
        c[0] = str(i)
    text = "\n".join(head + [",".join(c) for c in out_rows]) + "\n"
    Path(csv_path).write_text(text, encoding="utf-8")
    return text


def rewrite_csv_merge(csv_path, keep_scene, absorb_scenes, merged_end, merged_len, merged_dur):
    """Extend keep_scene's End.. to merged_end, drop absorb_scenes rows. No renumbering
    (mirrors merge_shots.py - a merge never inserts, so scene numbers can be left with gaps
    until the next split/rebuild touches them)."""
    head, data = _csv_rewrite_lines(csv_path)
    out = list(head)
    for l in data:
        if not l.strip():
            continue
        c = next(csv.reader([l]))
        if not c[0].strip().isdigit():
            out.append(l)
            continue
        sn = int(c[0])
        if sn in absorb_scenes:
            continue
        if sn == keep_scene:
            c[4] = str(merged_end["end_frame"])
            c[5] = merged_end["end_tc"]
            c[6] = str(merged_end["end_time"])
            c[7] = str(merged_len)
            c[8] = sec_to_tc(merged_dur)
            c[9] = f"{merged_dur:.3f}"
            out.append(",".join(c))
            continue
        out.append(l)
    text = "\n".join(out) + "\n"
    Path(csv_path).write_text(text, encoding="utf-8")
    return text


# =============================================================================================
# ffmpeg helpers (frame-accurate extract, same math as bs_worker._seek_args / merge_shots)
# =============================================================================================

def extract_fullres(movie, seconds, out):
    """Frame-accurate single-frame grab: fast pre-seek ~2s before, then accurate output-seek
    to the exact time. Caller must pass the CENTRE time of the wanted frame (see
    frame_center_time) so boundary frames don't round into the neighbouring shot."""
    pre = max(0.0, seconds - 2.0)
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{pre:.6f}", "-i", str(movie), "-ss", f"{seconds - pre:.6f}",
                    "-frames:v", "1", "-q:v", "2", str(out)], check=True)


def downscale(src, dst):
    from PIL import Image
    im = Image.open(src).convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS)
    im.save(dst, quality=THUMB_QUALITY)


def detect_encoder(movie, encoder_pref="libx264"):
    return encoder_pref  # config-driven; NVENC probing lives in bs_worker._nvenc_works if wired up


def extract_cut_mp4(movie, start_tc, duration_s, out_path, encoder="libx264"):
    """Best-effort per-shot reference clip. Returns True/False; caller decides fatal-or-not
    (merge/split treat this as optional, matching the source scripts)."""
    venc = ["-c:v", "libx264", "-crf", "18", "-preset", "medium"] if encoder != "nvenc" \
        else ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20"]
    try:
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", str(start_tc).replace(",", "."), "-i", str(movie),
                            "-t", f"{duration_s:.4f}"] + venc +
                           ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(out_path)],
                           capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


# =============================================================================================
# sheet layer (OPTIONAL - only imported/used if a sheet config is supplied)
# =============================================================================================

class SheetHandle:
    """Thin wrapper around a Sheets values() service bound to one spreadsheet+tab. Constructed
    only when a sheet is configured; every bs_repair function accepts `sheet=None` to skip all
    Sheets calls entirely (filesystem-only mode)."""

    def __init__(self, service, spreadsheet_id, tab, sheet_gid=None):
        self.svc = service
        self.spreadsheet_id = spreadsheet_id
        self.tab = tab
        self.sheet_gid = sheet_gid

    @classmethod
    def connect(cls, spreadsheet_id, tab, client_secret=None, token=None, service_account=None):
        """Build a Sheets service using the same auth helper as bs_gsheets.py, then resolve the
        tab's sheetId (needed for insert/delete row requests)."""
        import bs_gsheets as G
        creds = G.get_credentials(client_secret, token, service_account)
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        meta = svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))").execute()
        gid = None
        for sh in meta.get("sheets", []):
            if sh["properties"]["title"] == tab:
                gid = sh["properties"]["sheetId"]
                break
        if gid is None:
            raise SystemExit(f"ERROR: tab '{tab}' not found in spreadsheet {spreadsheet_id}")
        return cls(svc, spreadsheet_id, tab, gid)

    def read_wide(self, last_col="AE", last_row=3000):
        """Read the WIDE range (A1:{last_col}{last_row}) - never a narrow fixed range, since
        columns move between cuts and a narrow read silently truncates resolve-by-header lookups."""
        grid = self.svc.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.tab}'!A1:{last_col}{last_row}").execute().get("values", [])
        return grid

    def row_index(self, grid, tcid_header="TC_ID"):
        """{normalized tcid -> (row_number, row_values)} for every data row."""
        H = header_map(grid[0])
        out = {}
        for i, r in enumerate(grid[1:], start=2):
            v = r[H[tcid_header]] if len(r) > H[tcid_header] else ""
            if str(v).strip():
                out[norm_tcid(v)] = (i, r)
        return out

    def batch_update_values(self, updates):
        """updates: list of (a1_range_without_tab, value). Adds the tab prefix here."""
        if not updates:
            return
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={
                "valueInputOption": "USER_ENTERED",
                "data": [{"range": f"'{self.tab}'!{rng}", "values": [[val]]}
                         for rng, val in updates]}).execute()

    def insert_row_after(self, row):
        """Insert one blank row directly below `row`, inheriting its formatting (so
        ARRAYFORMULA/format spill columns keep working, same as split_shot.py)."""
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [
            {"insertDimension": {"range": {"sheetId": self.sheet_gid, "dimension": "ROWS",
             "startIndex": row, "endIndex": row + 1}, "inheritFromBefore": True}}]}).execute()

    def delete_rows(self, start_row, count):
        """Delete `count` rows starting at 1-based `start_row` (inclusive)."""
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [
            {"deleteDimension": {"range": {"sheetId": self.sheet_gid, "dimension": "ROWS",
             "startIndex": start_row - 1, "endIndex": start_row - 1 + count}}}]}).execute()


def make_sheet_handle(cfg, args):
    """Build a SheetHandle from config/CLI, or return None (filesystem-only) if not configured.
    Sheet use is opt-in: no --sheet-id (and no config spreadsheet_id) => skip entirely."""
    sid = getattr(args, "sheet_id", "") or cfg.get("spreadsheet_id", "")
    if not sid:
        return None
    tab = getattr(args, "sheet_tab", "") or cfg.get("sheet_tab", "Shots_Breakdown")
    return SheetHandle.connect(
        sid, tab,
        client_secret=cfg.get("google_client_secret") or None,
        token=cfg.get("google_token") or None,
        service_account=cfg.get("google_service_account") or None)


# =============================================================================================
# config
# =============================================================================================

def load_config(path=None):
    p = Path(path) if path else Path(__file__).parent / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_paths(cfg, args):
    movie = getattr(args, "movie", "") or cfg.get("last_movie", "")
    output_base = getattr(args, "output_base", "") or cfg.get("output_base", "./output")
    prefix = getattr(args, "prefix", "") or cfg.get("prefix", DEFAULT_PREFIX)
    if not movie:
        raise SystemExit("ERROR: no --movie given and no last_movie in config.json")
    stem = Path(movie).stem
    base = Path(output_base) / stem
    return {
        "movie": movie, "stem": stem, "base": base, "prefix": prefix,
        "scenes_dir": base / "scenes", "frames_dir": base / "frames",
        "thumbs_dir": base / "thumbs", "cuts_dir": base / "cuts",
    }


# =============================================================================================
# split
# =============================================================================================

def plan_split(shot_len, at_frames, fps):
    """Pure planning logic for an N-way split: returns list of piece dicts (clip-relative
    frame_start/frame_end/len) in chronological order, using the decreasing-order invariant for
    HOW they'd be applied but returning them forward-ordered for reporting/CSV writing."""
    cut_points = split_frames_decreasing(shot_len, at_frames)   # validates + decreasing
    increasing = sorted(cut_points)
    bounds = [0] + increasing + [shot_len]
    pieces = []
    for a, b in zip(bounds, bounds[1:]):
        pieces.append({"frame_start": a, "frame_end": b - 1, "len": b - a})
    return pieces, cut_points  # cut_points kept decreasing for the apply loop


def split_shot(shot, at_frames, cfg, args, apply=False, reocr=False, sheet=None):
    """Split one shot into len(at_frames)+1 pieces at clip-relative 0-based frame(s) at_frames.

    shot: shot code or bare tcid, e.g. 'SHW_00091413' or '00091413'.
    at_frames: single int, or a list for an N-way split (validated + applied decreasing, see
               split_frames_decreasing).
    Returns a plan dict always; only writes anything when apply=True.
    """
    paths = resolve_paths(cfg, args)
    fps = getattr(args, "fps", 0) or get_fps(paths["movie"])
    csv_path = find_scenes_csv(paths["scenes_dir"])
    if not csv_path:
        raise SystemExit(f"ERROR: no scenes CSV in {paths['scenes_dir']}")
    scenes = parse_scenes_csv(csv_path)
    by_tc = {tc_to_id(s["start_tc"], fps): s for s in scenes}
    keep_tc = tcid_of(shot)
    if keep_tc not in by_tc:
        raise SystemExit(f"ERROR: {keep_tc} not in scenes CSV")
    sc = by_tc[keep_tc]
    n = sc["len_frames"]

    at_list = at_frames if isinstance(at_frames, (list, tuple)) else [at_frames]
    pieces, cut_points_desc = plan_split(n, at_list, fps)

    start_sec = tc_to_seconds(sc["start_tc"])
    fps_i = fps
    # per-piece tcid = the file TC at that piece's first frame (bare keep_tc for piece 0)
    for i, p in enumerate(pieces):
        piece_start_sec = start_sec + p["frame_start"] / fps_i
        p["tcid"] = keep_tc if i == 0 else sec_to_smpte(piece_start_sec, fps_i).replace(":", "")
        p["start_sec"] = piece_start_sec
        p["start_tc_smpte"] = sec_to_smpte(piece_start_sec, fps_i)

    print("\n=== SPLIT PLAN ===")
    print(f"shot   {paths['prefix']}_{keep_tc}  scene {sc['scene']}  "
          f"{sc['start_tc']} -> {sc['end_tc']}  {n}f")
    print(f"cut points (clip-relative, applied decreasing): {cut_points_desc}")
    for i, p in enumerate(pieces):
        tag = "keep" if i == 0 else f"NEW[{i}]"
        print(f"  {tag:8s} {paths['prefix']}_{p['tcid']}: {p['start_tc_smpte']}  {p['len']}f")

    existing_tcids = {tcid_of(s) for s in [keep_tc]}
    for p in pieces[1:]:
        if p["tcid"] in by_tc:
            raise SystemExit(f"ERROR: new code {p['tcid']} already exists in scenes CSV")

    if sheet is not None:
        grid = sheet.read_wide()
        row_of = sheet.row_index(grid)
        if norm_tcid(keep_tc) not in row_of:
            raise SystemExit(f"ERROR: {keep_tc} not in sheet")
        for p in pieces[1:]:
            if norm_tcid(p["tcid"]) in row_of:
                raise SystemExit(f"ERROR: new code {p['tcid']} already exists in sheet")
        a_row = row_of[norm_tcid(keep_tc)][0]
        print(f"sheet: update row {a_row}; INSERT {len(pieces) - 1} new row(s) after it")

    if not apply:
        print("\n[dry-run] nothing changed. Re-run with apply=True / --apply.\n")
        return {"pieces": pieces, "applied": False}

    tmp = Path(tempfile.mkdtemp())
    enc = detect_encoder(paths["movie"], cfg.get("encoder", "libx264"))

    def regen(tcid, start_sec_piece, piece_len, positions):
        out = {}
        for pos in positions:
            s0, mid, end = extract_positions(piece_len)
            k = {"start": s0, "mid": mid, "end": end}[pos]
            sec = frame_center_time(start_sec_piece, k, fps)
            fr = tmp / f"{tcid}-{pos}.jpg"
            extract_fullres(paths["movie"], sec, fr)
            shutil.copyfile(fr, paths["frames_dir"] / f"{paths['prefix']}_{tcid}-{pos}.jpg")
            th = paths["thumbs_dir"] / f"{paths['prefix']}_{tcid}-{pos}.jpg"
            downscale(fr, th)
            out[pos] = th
        return out

    piece_thumbs = []
    for i, p in enumerate(pieces):
        positions = ("mid", "end") if i == 0 else ("start", "mid", "end")  # keep's start unchanged
        piece_thumbs.append(regen(p["tcid"], p["start_sec"], p["len"], positions))

    # cut clips (best-effort)
    paths["cuts_dir"].mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(pieces):
        out_mp4 = paths["cuts_dir"] / f"{paths['prefix']}_{p['tcid']}.mp4"
        extract_cut_mp4(paths["movie"], p["start_tc_smpte"], p["len"] / fps, out_mp4, enc)

    # scenes CSV rewrite
    csv_pieces = []
    running_start_frame = sc["start_frame"]
    for p in pieces:
        end_frame = running_start_frame + p["len"] - 1
        end_sec = p["start_sec"] + (p["len"] - 1) / fps + 1 / fps  # exclusive-end convention below
        csv_pieces.append({
            "start_frame": running_start_frame,
            "start_tc": sec_to_tc(p["start_sec"]),
            "start_time": f"{p['start_sec']:.3f}",
            "end_frame": end_frame,
            "end_tc": sec_to_tc(p["start_sec"] + p["len"] / fps),
            "end_time": f"{p['start_sec'] + p['len'] / fps:.3f}",
            "len_frames": p["len"],
            "len_tc": sec_to_tc(p["len"] / fps),
            "len_time": f"{p['len'] / fps:.3f}",
        })
        running_start_frame = end_frame + 1
    rewrite_csv_split(csv_path, sc["scene"], csv_pieces, fps)
    print(f"[ok] scenes CSV split into {len(pieces)} pieces ({csv_path.name}.bak saved)")

    if sheet is not None:
        grid = sheet.read_wide()
        H = header_map(grid[0])
        row_of = sheet.row_index(grid)
        a_row, a_vals = row_of[norm_tcid(keep_tc)]
        updates = [
            (f"{col_letter(H['TC Out'])}{a_row}", "'" + pieces[1]["start_tc_smpte"]
             if len(pieces) > 1 else a_vals[H["TC Out"]] if len(a_vals) > H["TC Out"] else ""),
            (f"{col_letter(H['Dur (f)'])}{a_row}", pieces[0]["len"]),
        ]
        if "mid" in piece_thumbs[0]:
            updates.append((f"{col_letter(H['url_mid'])}{a_row}", str(piece_thumbs[0].get("mid", ""))))
        if "end" in piece_thumbs[0]:
            updates.append((f"{col_letter(H['url_end'])}{a_row}", str(piece_thumbs[0].get("end", ""))))
        sheet.batch_update_values(updates)
        insert_at = a_row
        for i, p in enumerate(pieces[1:], start=1):
            sheet.insert_row_after(insert_at)
            insert_at += 1
            new_row = insert_at
            val = {
                "TC In": "'" + p["start_tc_smpte"],
                "TC_ID": "'" + p["tcid"],
                "Dur (f)": p["len"],
                "Seq": paths["prefix"],
            }
            data = [(f"{col_letter(H[k])}{new_row}", v) for k, v in val.items() if k in H]
            sheet.batch_update_values(data)
        print(f"[ok] sheet: updated row {a_row}, inserted {len(pieces) - 1} row(s)")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n[done] split {paths['prefix']}_{keep_tc} into {len(pieces)} piece(s).\n")
    return {"pieces": pieces, "applied": True}


# =============================================================================================
# merge
# =============================================================================================

def merge_shots(keep, absorb_list, cfg, args, apply=False, reocr=False, sheet=None):
    """Merge `keep` + one or more contiguous following shots (`absorb_list`) into one shot under
    keep's code. Mirrors merge_shots.py: recompute duration/frames/thumbs for keep, delete
    absorbed rows/CSV entries. Cut-clip re-cut is best-effort/non-fatal."""
    paths = resolve_paths(cfg, args)
    fps = getattr(args, "fps", 0) or get_fps(paths["movie"])
    csv_path = find_scenes_csv(paths["scenes_dir"])
    if not csv_path:
        raise SystemExit(f"ERROR: no scenes CSV in {paths['scenes_dir']}")
    scenes = parse_scenes_csv(csv_path)
    by_tc = {tc_to_id(s["start_tc"], fps): s for s in scenes}

    keep_tc = tcid_of(keep)
    absorb_tc = [tcid_of(c) for c in absorb_list]
    for tc in [keep_tc] + absorb_tc:
        if tc not in by_tc:
            raise SystemExit(f"ERROR: shot {tc} not found in scenes CSV")
    chain = [by_tc[keep_tc]] + [by_tc[t] for t in absorb_tc]
    order = [s["scene"] for s in chain]
    if order != list(range(order[0], order[0] + len(order))):
        raise SystemExit(f"ERROR: shots are not consecutive scenes (scene #s {order})")
    for a, b in zip(chain, chain[1:]):
        if a["end_frame"] + 1 != b["start_frame"]:
            raise SystemExit(f"ERROR: scene {a['scene']} end_frame {a['end_frame']} +1 != next "
                              f"start {b['start_frame']} - not contiguous")

    keep_sc, last = chain[0], chain[-1]
    start_sec = tc_to_seconds(keep_sc["start_tc"])
    end_sec = tc_to_seconds(last["end_tc"])
    merged_len = last["end_frame"] - keep_sc["start_frame"] + 1
    merged_dur = end_sec - start_sec

    print("\n=== MERGE PLAN ===")
    print(f"keep   {paths['prefix']}_{keep_tc}  scene {keep_sc['scene']}  start {keep_sc['start_tc']}")
    for t in absorb_tc:
        print(f"absorb {paths['prefix']}_{t}  scene {by_tc[t]['scene']}  "
              f"({by_tc[t]['start_tc']} -> {by_tc[t]['end_tc']})")
    print(f"MERGED -> {paths['prefix']}_{keep_tc}: {keep_sc['start_tc']} -> {last['end_tc']}  "
          f"{merged_len} frames ({merged_dur:.2f}s)")

    keep_row = None
    if sheet is not None:
        grid = sheet.read_wide()
        row_of = sheet.row_index(grid)
        for tc in [keep_tc] + absorb_tc:
            if norm_tcid(tc) not in row_of:
                raise SystemExit(f"ERROR: shot {tc} not found in sheet")
        keep_row = row_of[norm_tcid(keep_tc)][0]
        absorb_rows = [row_of[norm_tcid(t)][0] for t in absorb_tc]
        if absorb_rows != list(range(keep_row + 1, keep_row + 1 + len(absorb_rows))):
            raise SystemExit(f"ERROR: absorbed rows {absorb_rows} are not directly below keep "
                              f"row {keep_row}")
        print(f"sheet: update row {keep_row}; DELETE rows {absorb_rows}")

    if not apply:
        print("\n[dry-run] nothing changed. Re-run with apply=True / --apply.\n")
        return {"merged_len": merged_len, "merged_dur": merged_dur, "applied": False}

    tmp = Path(tempfile.mkdtemp())
    s0, mid, end = extract_positions(merged_len)
    new_thumbs = {}
    for pos, k in (("mid", mid), ("end", end)):
        sec = frame_center_time(start_sec, k, fps)
        fr = tmp / f"{keep_tc}-{pos}.jpg"
        extract_fullres(paths["movie"], sec, fr)
        th = paths["thumbs_dir"] / f"{paths['prefix']}_{keep_tc}-{pos}.jpg"
        downscale(fr, th)
        new_thumbs[pos] = th
    for t in absorb_tc:
        for pos in ("start", "mid", "end"):
            (paths["thumbs_dir"] / f"{paths['prefix']}_{t}-{pos}.jpg").unlink(missing_ok=True)
            (paths["frames_dir"] / f"{paths['prefix']}_{t}-{pos}.jpg").unlink(missing_ok=True)

    # cut clip re-cut: OPTIONAL, non-fatal if the cuts dir or ffmpeg is unavailable
    if paths["cuts_dir"].exists():
        out_mp4 = paths["cuts_dir"] / f"{paths['prefix']}_{keep_tc}.mp4"
        enc = detect_encoder(paths["movie"], cfg.get("encoder", "libx264"))
        if extract_cut_mp4(paths["movie"], keep_sc["start_tc"], merged_dur, out_mp4, enc):
            for t in absorb_tc:
                (paths["cuts_dir"] / f"{paths['prefix']}_{t}.mp4").unlink(missing_ok=True)
            print(f"[ok] re-cut {out_mp4.name}; deleted {len(absorb_tc)} absorbed MP4s")
        else:
            print("  [warn] cut-clip re-cut failed - not needed for the sheet, continuing")
    else:
        print("  [warn] no cuts/ dir - skipping cut-clip re-cut (non-fatal)")

    if sheet is not None:
        grid = sheet.read_wide()
        H = header_map(grid[0])
        row_of = sheet.row_index(grid)
        _, keep_vals = row_of[norm_tcid(keep_tc)]
        updates = [
            (f"{col_letter(H['TC Out'])}{keep_row}", "'" + sec_to_smpte(end_sec, fps)),
            (f"{col_letter(H['Dur (f)'])}{keep_row}", merged_len),
        ]
        if "url_mid" in H:
            updates.append((f"{col_letter(H['url_mid'])}{keep_row}", str(new_thumbs["mid"])))
        if "url_end" in H:
            updates.append((f"{col_letter(H['url_end'])}{keep_row}", str(new_thumbs["end"])))
        sheet.batch_update_values(updates)
        absorb_rows = [row_of[norm_tcid(t)][0] for t in absorb_tc]
        sheet.delete_rows(keep_row + 1, len(absorb_rows))
        print(f"[ok] sheet: updated row {keep_row}, deleted {len(absorb_rows)} absorbed rows")

    rewrite_csv_merge(csv_path, keep_sc["scene"], [by_tc[t]["scene"] for t in absorb_tc],
                       last, merged_len, merged_dur)
    print(f"[ok] scenes CSV updated ({csv_path.name}.bak saved)")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n[done] merged {len(chain)} shots into {paths['prefix']}_{keep_tc}.\n")
    return {"merged_len": merged_len, "merged_dur": merged_dur, "applied": True}


# =============================================================================================
# rethumb
# =============================================================================================

def rethumb(shot, cfg, args, positions=("start", "mid", "end"), apply=False, sheet=None):
    """Regenerate a shot's frames/thumbs frame-accurately after a boundary change."""
    paths = resolve_paths(cfg, args)
    fps = getattr(args, "fps", 0) or get_fps(paths["movie"])
    csv_path = find_scenes_csv(paths["scenes_dir"])
    if not csv_path:
        raise SystemExit(f"ERROR: no scenes CSV in {paths['scenes_dir']}")
    scenes = parse_scenes_csv(csv_path)
    by_tc = {tc_to_id(s["start_tc"], fps): s for s in scenes}
    tc = tcid_of(shot)
    if tc not in by_tc:
        raise SystemExit(f"ERROR: {tc} not in scenes CSV")
    sc = by_tc[tc]
    start_sec = tc_to_seconds(sc["start_tc"])
    n = sc["len_frames"]
    s0, mid, end = extract_positions(n)
    frame_of = {"start": s0, "mid": mid, "end": end}

    print(f"shot {paths['prefix']}_{tc}  scene {sc['scene']}  {n}f  -> regen {list(positions)}")
    if not apply:
        for p in positions:
            k = frame_of[p]
            print(f"  {p}: frame {k} @ {frame_center_time(start_sec, k, fps):.3f}s")
        print("[dry-run] re-run with apply=True / --apply")
        return {"applied": False}

    tmp = Path(tempfile.mkdtemp())
    paths["frames_dir"].mkdir(parents=True, exist_ok=True)
    paths["thumbs_dir"].mkdir(parents=True, exist_ok=True)
    thumbs = {}
    for pos in positions:
        k = frame_of[pos]
        sec = frame_center_time(start_sec, k, fps)
        fr = tmp / f"{tc}-{pos}.jpg"
        extract_fullres(paths["movie"], sec, fr)
        shutil.copyfile(fr, paths["frames_dir"] / f"{paths['prefix']}_{tc}-{pos}.jpg")
        th = paths["thumbs_dir"] / f"{paths['prefix']}_{tc}-{pos}.jpg"
        downscale(fr, th)
        thumbs[pos] = th

    if sheet is not None:
        grid = sheet.read_wide()
        H = header_map(grid[0])
        row_of = sheet.row_index(grid)
        if norm_tcid(tc) not in row_of:
            raise SystemExit(f"ERROR: {tc} not in sheet")
        row, _ = row_of[norm_tcid(tc)]
        url_col = {"start": "url_start", "mid": "url_mid", "end": "url_end"}
        updates = [(f"{col_letter(H[url_col[p]])}{row}", str(thumbs[p]))
                   for p in positions if url_col[p] in H]
        sheet.batch_update_values(updates)
        print(f"[ok] sheet row {row}: updated {[url_col[p] for p in positions]}")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[done] {paths['prefix']}_{tc}: regenerated {list(positions)}.")
    return {"applied": True, "thumbs": {k: str(v) for k, v in thumbs.items()}}


# =============================================================================================
# apply-ledger
# =============================================================================================

def load_ledger(ledger_path):
    data = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    return data.get("corrections", data if isinstance(data, list) else [])


def order_ledger_bottom_to_top(corrections, row_key="sheet_row"):
    """Sort corrections by descending sheet row (or descending merged_tcid as a fallback when no
    row number is recorded yet) so applying one split's row-insert never shifts a still-pending
    correction's target row. This is the reason the ledger must be replayed bottom-to-top."""
    def key(c):
        row = c.get(row_key)
        if row is not None:
            return (0, -int(row))
        # fallback: sort by merged_tcid descending (later-in-file shots sort first)
        tcid = c.get("merged_tcid") or c.get("cut7", {}).get("merged_tcid") or "0"
        return (1, -int(str(tcid).lstrip("0") or "0"))
    return sorted(corrections, key=key)


def apply_ledger(ledger_path, cfg, args, apply=False, sheet=None, case_ids=None):
    """Replay a persistent ledger of operator-confirmed missed-cut corrections. Each correction
    supplies a merged tcid/shot + cut frame(s); applied via split_shot, bottom-to-top so earlier
    (higher-row) inserts never invalidate a later (lower-row) pending target."""
    corrections = load_ledger(ledger_path)
    if case_ids:
        corrections = [c for c in corrections if c.get("id") in set(case_ids)]
    ordered = order_ledger_bottom_to_top(corrections)

    print(f"\n=== LEDGER PLAN ({len(ordered)} correction(s), bottom-to-top) ===")
    results = []
    for c in ordered:
        cid = c.get("id", "?")
        merged = c.get("merged_tcid") or (c.get("cut7") or {}).get("merged_tcid")
        cut_frames = c.get("cut_frame")
        if cut_frames is None:
            cf = (c.get("cut7") or {}).get("cut_frames")
            cut_frames = cf if cf else None
        if merged is None or cut_frames is None:
            print(f"  SKIP {cid}: missing merged_tcid/cut_frame(s)")
            continue
        row = c.get("sheet_row") or (c.get("cut7") or {}).get("sheet_row")
        print(f"  [{cid}] split {merged} @ frame(s) {cut_frames}"
              + (f"  (sheet row {row})" if row else ""))
        res = split_shot(merged, cut_frames, cfg, args, apply=apply, sheet=sheet)
        results.append({"id": cid, **res})
    if not apply:
        print("\n[dry-run] nothing changed. Re-run with apply=True / --apply.\n")
    return results


# =============================================================================================
# CLI
# =============================================================================================

def _add_common_args(p):
    p.add_argument("--movie", default="")
    p.add_argument("--output-base", default="")
    p.add_argument("--prefix", default="")
    p.add_argument("--fps", type=float, default=0.0)
    p.add_argument("--config", default="")
    p.add_argument("--sheet-id", default="", help="omit to run filesystem-only (no Sheets calls)")
    p.add_argument("--sheet-tab", default="")
    p.add_argument("--apply", action="store_true")


def _burnin_arg_to_file_tc(args, cfg, fps):
    if not getattr(args, "burnin_tc", None):
        return None
    offset = cfg.get("show_tc_offset", "")
    if not offset:
        raise SystemExit("ERROR: --burnin-tc given but config has no show_tc_offset")
    return burnin_to_file_tc(args.burnin_tc, offset, fps)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("split")
    _add_common_args(p)
    p.add_argument("--shot", required=True)
    p.add_argument("--at-frame", type=int, nargs="+", default=None,
                   help="clip-relative 0-based frame(s) where later pieces begin; give N-1 "
                        "values for an N-way split")
    p.add_argument("--burnin-tc", default="",
                   help="alternative to --at-frame: a burn-in HH:MM:SS:FF to convert via "
                        "show_tc_offset, then resolve to a clip-relative frame")

    p = sub.add_parser("merge")
    _add_common_args(p)
    p.add_argument("--keep", required=True)
    p.add_argument("--absorb", required=True, nargs="+")

    p = sub.add_parser("rethumb")
    _add_common_args(p)
    p.add_argument("--shot", required=True)
    p.add_argument("--positions", nargs="+", default=["start", "mid", "end"],
                   choices=["start", "mid", "end"])

    p = sub.add_parser("apply-ledger")
    _add_common_args(p)
    p.add_argument("--ledger", required=True)
    p.add_argument("--id", nargs="+", default=None, help="restrict to specific case ids")

    args = ap.parse_args()
    cfg = load_config(args.config or None)
    sheet = make_sheet_handle(cfg, args)

    if args.cmd == "split":
        fps = args.fps or (get_fps(args.movie) if args.movie else 24.0)
        if args.burnin_tc:
            file_tc = _burnin_arg_to_file_tc(args, cfg, fps)
            print(f"[burnin] {args.burnin_tc} - offset {cfg.get('show_tc_offset')} -> file TC "
                  f"{file_tc} (convert to a clip-relative frame yourself before --at-frame; "
                  f"this prints the resolved file TC for that step)")
            if not args.at_frame:
                raise SystemExit("ERROR: --burnin-tc resolves the file TC only; pass the "
                                  "resulting clip-relative --at-frame explicitly")
        if not args.at_frame:
            raise SystemExit("ERROR: --at-frame (or --burnin-tc + --at-frame) is required")
        at = args.at_frame[0] if len(args.at_frame) == 1 else list(args.at_frame)
        split_shot(args.shot, at, cfg, args, apply=args.apply, sheet=sheet)
    elif args.cmd == "merge":
        merge_shots(args.keep, args.absorb, cfg, args, apply=args.apply, sheet=sheet)
    elif args.cmd == "rethumb":
        rethumb(args.shot, cfg, args, positions=args.positions, apply=args.apply, sheet=sheet)
    elif args.cmd == "apply-ledger":
        apply_ledger(args.ledger, cfg, args, apply=args.apply, sheet=sheet, case_ids=args.id)


if __name__ == "__main__":
    main()
