#!/usr/bin/env python
"""bs_gsheets.py - portable Google Sheets / Drive layer for Breakdown Studio.

Lets any user connect THEIR OWN Google account (desktop OAuth), start a new project from a blank
template, or update an existing project sheet when a new cut arrives. No studio-specific imports.

Auth (in priority order):
  1. --service-account JSON  (or BS_GOOGLE_SA env)   -> headless / shared-bot use
  2. OAuth desktop flow with a client-secret JSON     -> "connect with your own Google account"
     - client secret:  --client-secret / BS_GOOGLE_CLIENT_SECRET / config
     - token cached at: --token / BS_GOOGLE_TOKEN (default: <this folder>/.gtoken.json)

Commands
  whoami         --client-secret CS [--token T]
                     -> opens a browser the first time, prints the signed-in account
  copy-template  --template-id ID --title "New Film Breakdown"
                     -> Drive-copies the blank template into the user's Drive; prints new id + url
  build          --spreadsheet-id ID --movie M --output-base B
                 [--tab Shots_Breakdown] [--first-row 3] [--thumbs] [--status VFX]
                     -> writes shot identity (Temp Cut4 Shot Code / TC In / Duration) and, with
                        --thumbs, an =IMAGE() mid-thumbnail, resolved BY HEADER NAME. Keyed on the
                        Temp Cut4 Shot Code: existing shots are updated in place, new shots appended.
                        Never touches cost/producer columns -> safe for "new cut on existing project".

                        If slate_ocr.csv / vfxnote_ocr.csv exist in the movie's output dir (written
                        by bs_ocr.py), build ALSO enriches identity columns from OCR (Scene, Slate,
                        Take, Slate Takes, Shot Code, VFX Notes) -- see ENRICHMENT below. If
                        tc_offset.txt exists (or config supplies a show_tc_offset), build also
                        populates Real TC In / Real TC Out. Every enrichment column is optional:
                        missing headers are skipped silently with one summary log line.

ENRICHMENT (non-clobbering)
  A cell is only ever written by OCR enrichment when it is EMPTY, or already holds the exact value
  OCR would write. If an operator has corrected a cell to something else, it is left alone and
  counted as "operator-kept" in the summary -- enrichment never overwrites a human correction.
  VFX Notes/VFX Note is populated only for rows the note OCR itself marked is_vfx.

Columns are matched by header text (whitespace/newlines collapsed), so a reordered sheet still works.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
IMAGE_RENDER_CAP = 400  # Google Sheets renders only ~400-500 =IMAGE() formulas per spreadsheet
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

# header text -> the local field that fills it (collapse whitespace when matching)
FIELD_HEADERS = {
    "temp_code": ["Temp Cut4 Shot Code"],
    "tc_in":     ["TC In"],
    "duration":  ["Duration"],
    "thumbnail": ["Thumbnail"],
    "status":    ["Status"],
}

# OCR-enrichment header candidates (FEATURE 1 / FEATURE 2). Same "resolve by header name, skip if
# absent" contract as FIELD_HEADERS -- a sheet without one of these columns just doesn't get that
# piece of enrichment; nothing errors.
ENRICH_HEADERS = {
    "scene":        ["Scene"],
    "slate":        ["Slate"],
    "take":         ["Take"],
    "slate_takes":  ["Slate Takes"],
    "shot_code":    ["Shot Code"],
    "vfx_note":     ["VFX Notes", "VFX Note"],
    "real_tc_in":   ["Real TC In"],
    "real_tc_out":  ["Real TC Out"],
    # bs_enrich.py descriptions.csv / dialogue.csv (dialogue + AI description enrichment). Same
    # non-clobber contract as everything else in this map.
    "description":  ["Description", "Short Description"],
    "dialogue":     ["Dialogue"],
}


# --------------------------------------------------------------- auth

def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _retry(fn, tries=5, base=1.0):
    """Run fn() with exponential backoff on transient HttpError (429/500/503)."""
    from googleapiclient.errors import HttpError
    for attempt in range(tries):
        try:
            return fn()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status not in (429, 500, 503) or attempt == tries - 1:
                raise
            wait = base * (2 ** attempt)
            print(f"[retry] HTTP {status}, backing off {wait:.1f}s "
                  f"(attempt {attempt + 1}/{tries})", flush=True)
            time.sleep(wait)
    return None


def get_credentials(client_secret=None, token=None, service_account=None):
    sa = service_account or os.environ.get("BS_GOOGLE_SA")
    if sa:
        from google.oauth2 import service_account as svc_acct
        return svc_acct.Credentials.from_service_account_file(sa, scopes=SCOPES)

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token = Path(token or os.environ.get("BS_GOOGLE_TOKEN") or (APP_DIR / ".gtoken.json"))
    creds = None
    if token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except Exception:
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json(), encoding="utf-8")
        return creds
    # need a fresh consent
    cs = client_secret or os.environ.get("BS_GOOGLE_CLIENT_SECRET")
    if not cs or not Path(cs).exists():
        sys.exit("ERROR: no valid token and no client-secret JSON. Provide --client-secret "
                 "(download an OAuth 'Desktop app' client from Google Cloud Console).")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(cs, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent",
                                  authorization_prompt_message="Opening browser to sign in to Google…")
    token.write_text(creds.to_json(), encoding="utf-8")
    print(f"[auth] token saved -> {token}", flush=True)
    return creds


def services(args):
    from googleapiclient.discovery import build
    creds = _creds_for(args)
    return (build("sheets", "v4", credentials=creds, cache_discovery=False),
            build("drive", "v3", credentials=creds, cache_discovery=False))


def _creds_for(args):
    return get_credentials(getattr(args, "client_secret", None),
                           getattr(args, "token", None),
                           getattr(args, "service_account", None))


def _drive_service(args):
    """Build a fresh Drive client (google api clients are NOT thread-safe -> one per thread)."""
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_creds_for(args), cache_discovery=False)


def account_email(drive):
    try:
        info = drive.about().get(fields="user(emailAddress,displayName)").execute()
        u = info.get("user", {})
        return u.get("emailAddress") or u.get("displayName") or "(unknown)"
    except Exception as e:
        return f"(could not read account: {e})"


# --------------------------------------------------------------- OCR enrichment (pure logic)
#
# FEATURE 1 (OCR -> sheet) and FEATURE 2 (real show TC) live here as pure functions so they are
# independently unit-testable (tests/test_gsheets_enrich.py) without a live Sheets connection --
# the caller (cmd_build) only does I/O: read slate_ocr.csv/vfxnote_ocr.csv/tc_offset.txt, call
# these, then turn the resulting per-tcid field dicts into cell writes.

def load_slate_ocr_csv(path):
    """slate_ocr.csv (written by bs_ocr.ocr_slate_frames) -> {tcid: row_dict}."""
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tcid = (row.get("tcid") or "").strip()
            if tcid:
                out[tcid] = row
    return out


def load_vfxnote_ocr_csv(path):
    """vfxnote_ocr.csv (written by bs_ocr.ocr_vfx_notes) -> {tcid: row_dict}."""
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tcid = (row.get("tcid") or "").strip()
            if tcid:
                out[tcid] = row
    return out


def load_descriptions_csv(path):
    """descriptions.csv (written by bs_enrich.py's describe stage) -> {tcid: row_dict}."""
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tcid = (row.get("tcid") or "").strip()
            if tcid:
                out[tcid] = row
    return out


def load_dialogue_csv(path):
    """dialogue.csv (written by bs_enrich.py's transcribe stage) -> {tcid: row_dict}."""
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tcid = (row.get("tcid") or "").strip()
            if tcid:
                out[tcid] = row
    return out


def build_desc_dialogue_enrichment_for_shot(tcid, desc_row, dialogue_row):
    """Pure join: one shot's descriptions.csv row + dialogue.csv row (either may be None/missing)
    -> {field: value} using the ENRICH_HEADERS field names ('description' from revised_description,
    falling back to visual_caption if no revision exists yet; 'dialogue' from dialogue.csv).
    Only fields with a usable non-empty value are included -- an absent/blank source value is
    simply omitted, never written as a blank-overwrite. Non-clobbering itself is applied by
    plan_enrichment_writes(), same as the OCR fields."""
    out = {}
    if desc_row:
        desc = (desc_row.get("revised_description") or "").strip() or \
               (desc_row.get("visual_caption") or "").strip()
        if desc:
            out["description"] = desc
    if dialogue_row:
        dlg = (dialogue_row.get("dialogue") or "").strip()
        if dlg:
            out["dialogue"] = dlg
    return out


def _row_is_vfx(row):
    v = str(row.get("is_vfx", "")).strip().lower()
    return v in ("1", "true", "yes", "y")


def slate_takes_label(slate, take_roman):
    """'Slate Takes' display value: slate number plus roman take, e.g. slate=125 take=I -> '125 I'.
    Blank if slate is missing (nothing to key the takes label on)."""
    slate = (slate or "").strip()
    take_roman = (take_roman or "").strip()
    if not slate:
        return ""
    return f"{slate} {take_roman}".strip()


def build_enrichment_for_shot(tcid, slate_row, note_row):
    """Pure join: one shot's slate_ocr.csv row + vfxnote_ocr.csv row (either may be None/missing)
    -> {field: value} using the ENRICH_HEADERS field names. Only fields with a usable OCR value
    are included (an empty/absent OCR value is simply omitted, never written as blank-overwrite).
    Does NOT know about existing sheet values -- non-clobbering is applied by
    plan_enrichment_writes() below, which is what actually decides whether to write a cell.
    """
    out = {}
    if slate_row:
        scene = (slate_row.get("scene") or "").strip()
        slate = (slate_row.get("slate") or "").strip()
        take_roman = (slate_row.get("take_roman") or "").strip()
        code = (slate_row.get("official_code") or "").strip()
        if scene:
            out["scene"] = scene
        if slate:
            out["slate"] = slate
        if take_roman:
            out["take"] = take_roman
        st = slate_takes_label(slate, take_roman)
        if st:
            out["slate_takes"] = st
        if code:
            out["shot_code"] = code
    if note_row and _row_is_vfx(note_row):
        note = (note_row.get("vfx_note") or "").strip()
        if note:
            out["vfx_note"] = note
    return out


def plan_enrichment_writes(shots, slate_by_tcid, note_by_tcid, existing_row_values, enrich_fields,
                           desc_by_tcid=None, dialogue_by_tcid=None):
    """Decide what to write for OCR (+ optional dialogue/description) enrichment, applying the
    non-clobber rule.

    shots:               ordered list of {"tcid":..., ...} (as produced by _load_local_shots)
    slate_by_tcid:        {tcid: slate_ocr.csv row}   (output of load_slate_ocr_csv), or {}
    note_by_tcid:         {tcid: vfxnote_ocr.csv row}  (output of load_vfxnote_ocr_csv), or {}
    existing_row_values:  {tcid: {field: current_cell_text}} for shots already in the sheet
                           (fields are the ENRICH_HEADERS keys that are actually present as
                           columns -- callers only pass fields whose header exists in the sheet;
                           fields absent from the sheet should not appear in any inner dict)
    enrich_fields:        set of field names to consider (usually enrich_fields present in the
                           sheet's header row -- callers filter this way so the summary log can
                           report which headers were skipped)
    desc_by_tcid:         optional {tcid: descriptions.csv row} (output of load_descriptions_csv),
                           feeds the 'description' field (FEATURE 3 / bs_enrich.py)
    dialogue_by_tcid:      optional {tcid: dialogue.csv row} (output of load_dialogue_csv), feeds
                           the 'dialogue' field (FEATURE 3 / bs_enrich.py)

    Returns (writes, summary):
      writes:   {tcid: {field: value}}          only cells that are safe to write (empty target,
                                                  or target already equals the OCR value)
      summary:  {"enriched": n, "operator_kept": n, "fields_written": {field: count}}
                "operator_kept" counts cells where the existing value differed from the OCR value
                and was therefore left untouched.
    """
    desc_by_tcid = desc_by_tcid or {}
    dialogue_by_tcid = dialogue_by_tcid or {}
    writes = {}
    enriched = 0
    operator_kept = 0
    fields_written = Counter()
    for s in shots:
        tcid = s["tcid"]
        fields = build_enrichment_for_shot(tcid, slate_by_tcid.get(tcid), note_by_tcid.get(tcid))
        fields.update(build_desc_dialogue_enrichment_for_shot(
            tcid, desc_by_tcid.get(tcid), dialogue_by_tcid.get(tcid)))
        if not fields:
            continue
        existing = existing_row_values.get(tcid, {})
        row_writes = {}
        for field, value in fields.items():
            if field not in enrich_fields:
                continue
            cur = (existing.get(field) or "").strip()
            if cur and cur != value:
                operator_kept += 1
                continue
            if cur == value:
                continue  # already correct -- nothing to write, not a clobber either way
            row_writes[field] = value
            fields_written[field] += 1
        if row_writes:
            writes[tcid] = row_writes
            enriched += 1
    return writes, {"enriched": enriched, "operator_kept": operator_kept,
                     "fields_written": dict(fields_written)}


# --------------------------------------------------------------- real show TC (FEATURE 2)

def parse_tc_offset_file(text):
    """Pull the median offset (in frames, and its 'HH:MM:SS:FF' form) out of a tc_offset.txt
    written by bs_ocr.probe_tc_offset(). Returns None if the file has no constant/usable offset
    (e.g. probe found no readable show-TC burn-ins, or offsets didn't converge).

    tc_offset.txt lines look like (see bs_ocr.probe_tc_offset):
        => CONSTANT
        median offset = 00:59:50:00 (86160 frames @ 24.0fps)
    """
    m = re.search(r"median offset\s*=\s*([\d:]+)\s*\((-?\d+) frames", text or "")
    if not m:
        return None
    return {"median_offset_tc": m.group(1), "median_offset_frames": int(m.group(2)),
            "constant": "=> CONSTANT" in text}


def _smpte_to_frames(tc, fps):
    p = str(tc).strip().lstrip("'").split(":")
    h, m, s, f = (int(x) for x in p[:4])
    return ((h * 3600 + m * 60 + s) * round(fps)) + f


def _frames_to_smpte(frames, fps):
    fps_i = round(fps)
    frames = max(0, int(frames))
    f = frames % fps_i
    t = frames // fps_i
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}:{f:02d}"


def real_tc(file_tc, offset_frames, fps):
    """file-relative 'HH:MM:SS:FF' + a constant offset (in frames) -> real show 'HH:MM:SS:FF'.
    real_TC = file_TC + offset (per CLAUDE.md's documented show-TC convention)."""
    return _frames_to_smpte(_smpte_to_frames(file_tc, fps) + int(offset_frames), fps)


def plan_real_tc_writes(shots, offset_frames, fps, existing_row_values, enrich_fields):
    """Same non-clobber contract as plan_enrichment_writes, for Real TC In / Real TC Out.

    shots: list of {"tcid":..., "tc_in": 'HH:MM:SS:FF', "duration": frames_str_or_int, ...}
    existing_row_values / enrich_fields: same shape as plan_enrichment_writes.
    """
    writes = {}
    enriched = 0
    operator_kept = 0
    fields_written = Counter()
    for s in shots:
        tcid = s["tcid"]
        tc_in = s.get("tc_in")
        if not tc_in:
            continue
        try:
            dur = int(float(s.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        fields = {}
        if "real_tc_in" in enrich_fields:
            fields["real_tc_in"] = real_tc(tc_in, offset_frames, fps)
        if "real_tc_out" in enrich_fields and dur > 0:
            tc_out_file = _frames_to_smpte(_smpte_to_frames(tc_in, fps) + dur - 1, fps)
            fields["real_tc_out"] = real_tc(tc_out_file, offset_frames, fps)
        if not fields:
            continue
        existing = existing_row_values.get(tcid, {})
        row_writes = {}
        for field, value in fields.items():
            cur = (existing.get(field) or "").strip().lstrip("'")
            if cur and cur != value:
                operator_kept += 1
                continue
            if cur == value:
                continue
            row_writes[field] = value
            fields_written[field] += 1
        if row_writes:
            writes.setdefault(tcid, {}).update(row_writes)
            enriched += 1
    return writes, {"enriched": enriched, "operator_kept": operator_kept,
                     "fields_written": dict(fields_written)}


# --------------------------------------------------------------- commands

def cmd_whoami(args):
    _, drive = services(args)
    print("CONNECTED " + account_email(drive))


def cmd_copy_template(args):
    sheets, drive = services(args)
    body = {"name": args.title}
    new = drive.files().copy(fileId=args.template_id, body=body,
                             fields="id,name,webViewLink").execute()
    sid = new["id"]
    url = new.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    print(f"NEW_SPREADSHEET_ID {sid}")
    print(f"URL {url}")
    return sid


# ---- build / update ----

def _load_local_shots(movie, output_base, prefix, fps_default=24.0):
    """Read the local Scenes.csv for the movie and return ordered shot dicts."""
    stem = Path(movie).stem
    base = Path(output_base) / stem
    csv_file = None
    for sub in ("scenes", "scenes_transnet"):
        p = base / sub / f"{stem}-Scenes.csv"
        if p.exists():
            csv_file = p
            break
    if not csv_file:
        cands = list(base.glob("scenes*/**/*Scenes*.csv"))
        csv_file = cands[0] if cands else None
    if not csv_file:
        sys.exit(f"ERROR: no Scenes.csv under {base} (run detection first).")

    # fps from ffprobe if available, else default
    fps = fps_default
    ff = os.environ.get("BS_FFPROBE")
    if ff and Path(movie).exists():
        import subprocess
        out = subprocess.run([ff, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=r_frame_rate", "-of", "csv=p=0", str(movie)],
                             capture_output=True, text=True).stdout.strip()
        try:
            n, d = out.split("/")
            fps = float(n) / float(d)
        except Exception:
            pass

    shots = []
    with open(csv_file, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if row and row[0].strip() == "Scene Number":
                header = [c.strip() for c in row]
                break
        for row in reader:
            if not row or not row[0].strip().isdigit():
                continue
            d = dict(zip(header, [c.strip() for c in row]))
            stc = d["Start Timecode"]
            shots.append({
                "tcid": _tc_to_id(stc, fps),
                "temp_code": f"{prefix}_{_tc_to_id(stc, fps)}",
                "tc_in": _tc_to_smpte(stc, fps),
                "duration": d["Length (frames)"],
            })
    print(f"[build] {len(shots)} shots from {csv_file.name} @ {fps:.3f}fps", flush=True)
    return shots, stem, base


def _tc_to_id(tc, fps):
    p = tc.split(":")
    s = float(p[2]); ff = int(round((s - int(s)) * fps))
    if ff >= round(fps): ff = int(round(fps)) - 1
    return f"{int(p[0]):02d}{int(p[1]):02d}{int(s):02d}{ff:02d}"


def _tc_to_smpte(tc, fps):
    p = tc.split(":")
    s = float(p[2]); ff = int(round((s - int(s)) * fps))
    if ff >= round(fps): ff = int(round(fps)) - 1
    return f"{int(p[0]):02d}:{int(p[1]):02d}:{int(s):02d}:{ff:02d}"


def _col_a1(idx):
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _ensure_public(drive, file_id):
    try:
        drive.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"},
                                   fields="id").execute()
    except Exception:
        pass


def _thumb_drive_folder(drive, name):
    q = ("mimeType='application/vnd.google-apps.folder' and name='%s' and trashed=false" % name)
    r = _retry(lambda: drive.files().list(q=q, fields="files(id)").execute()).get("files", [])
    if r:
        return r[0]["id"]
    f = _retry(lambda: drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute())
    return f["id"]


def _list_folder_files(drive, folder):
    """One paginated listing of a folder -> {name: {"id":..., "md5":...}} (md5 may be None)."""
    out, page = {}, None
    while True:
        resp = _retry(lambda: drive.files().list(
            q=f"'{folder}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,md5Checksum)",
            pageSize=1000, pageToken=page).execute())
        for f in resp.get("files", []):
            out[f["name"]] = {"id": f["id"], "md5": f.get("md5Checksum")}
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def _upload_thumb(drive, folder, path, name, existing):
    """Reuse by id ONLY when md5 matches; otherwise update bytes in place (stable id/URL)
    or create a new public file. `existing` is the {name:{id,md5}} dict from _list_folder_files."""
    from googleapiclient.http import MediaFileUpload
    local_md5 = _md5_file(path)
    ex = existing.get(name)
    if ex:
        fid = ex["id"]
        if ex.get("md5") == local_md5:
            return fid  # identical content already public -> reuse as-is
        media = MediaFileUpload(str(path), mimetype="image/jpeg")
        _retry(lambda: drive.files().update(fileId=fid, media_body=media).execute())
        _ensure_public(drive, fid)
        return fid
    media = MediaFileUpload(str(path), mimetype="image/jpeg")
    fid = _retry(lambda: drive.files().create(
        body={"name": name, "parents": [folder]},
        media_body=media, fields="id").execute())["id"]
    _ensure_public(drive, fid)
    return fid


def _tab_grid(sheets, spreadsheet_id, tab):
    """Return (sheetId, rowCount) for a tab by title."""
    meta = _retry(lambda: sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount)))").execute())
    for sh in meta.get("sheets", []):
        p = sh["properties"]
        if p["title"] == tab:
            return p["sheetId"], p.get("gridProperties", {}).get("rowCount", 0)
    sys.exit(f"ERROR: tab '{tab}' not found in spreadsheet.")


def _ensure_rows(sheets, spreadsheet_id, sheet_id, row_count, max_row):
    """Expand the grid so it can hold max_row (appends rows past current rowCount)."""
    if max_row <= row_count:
        return
    add = max_row - row_count
    _retry(lambda: sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"appendDimension": {
            "sheetId": sheet_id, "dimension": "ROWS", "length": add}}]}).execute())
    print(f"[build] expanded grid {row_count} -> {max_row} rows", flush=True)


def _find_ocr_artifacts(base):
    """base = the movie's output dir (parent of thumbs/frames/scenes). Returns
    (slate_csv_path_or_None, vfxnote_csv_path_or_None, tc_offset_txt_path_or_None)."""
    slate = base / "slate_ocr.csv"
    notes = base / "vfxnote_ocr.csv"
    offset = base / "tc_offset.txt"
    return (slate if slate.exists() else None,
            notes if notes.exists() else None,
            offset if offset.exists() else None)


def _find_enrich_artifacts(base):
    """base = the movie's output dir. Returns (descriptions_csv_path_or_None,
    dialogue_csv_path_or_None) -- the bs_enrich.py outputs for FEATURE 3 (dialogue + description
    auto-detect). Same "missing = skipped silently" contract as _find_ocr_artifacts."""
    desc = base / "descriptions.csv"
    dialogue = base / "dialogue.csv"
    return (desc if desc.exists() else None, dialogue if dialogue.exists() else None)


def _enrich_col_map(hmap):
    """{ENRICH_HEADERS field -> 0-based col idx}, only for fields whose header is present."""
    out = {}
    for field, cands in ENRICH_HEADERS.items():
        for cand in cands:
            if _norm(cand) in hmap:
                out[field] = hmap[_norm(cand)]
                break
    return out


def cmd_build(args):
    sheets, drive = services(args)
    thumb_mode = getattr(args, "thumb_mode", "image")
    do_thumbs = args.thumbs and thumb_mode != "none"
    shots, stem, base = _load_local_shots(args.movie, args.output_base, args.prefix)
    tab = args.tab

    # header row + existing temp-code column
    meta = _retry(lambda: sheets.spreadsheets().values().get(
        spreadsheetId=args.spreadsheet_id, range=f"'{tab}'!{args.first_row - 1}:{args.first_row - 1}"
    ).execute()).get("values", [[]])
    header = meta[0] if meta else []
    if not header:
        sys.exit(f"ERROR: no header row at row {args.first_row - 1} of '{tab}'.")
    hmap = {_norm(h): i for i, h in enumerate(header)}

    def col_for(field):
        for cand in FIELD_HEADERS[field]:
            if _norm(cand) in hmap:
                return hmap[_norm(cand)]
        return None

    cidx = {f: col_for(f) for f in FIELD_HEADERS}
    if cidx["temp_code"] is None:
        sys.exit("ERROR: sheet has no 'Temp Cut4 Shot Code' column to key on.")
    print(f"[build] columns -> " +
          ", ".join(f"{f}={_col_a1(i) if i is not None else '-'}" for f, i in cidx.items()),
          flush=True)

    # FEATURE 1 / FEATURE 2: OCR + real-TC enrichment column resolution (auto-detected, all
    # optional). Missing headers are simply not in ecidx -> that piece of enrichment is skipped.
    ecidx = _enrich_col_map(hmap)
    skipped_enrich = sorted(set(ENRICH_HEADERS) - set(ecidx))
    slate_csv, notes_csv, offset_txt = _find_ocr_artifacts(base)
    desc_csv, dialogue_csv = _find_enrich_artifacts(base)
    if ecidx:
        present = ", ".join(f"{f}={_col_a1(i)}" for f, i in sorted(ecidx.items()))
        print(f"[build] enrichment columns present -> {present}"
              + (f"  (skipped, not in sheet: {', '.join(skipped_enrich)})" if skipped_enrich else ""),
              flush=True)

    # existing rows: map temp_code -> row number
    tc_col = _col_a1(cidx["temp_code"])
    existing = _retry(lambda: sheets.spreadsheets().values().get(
        spreadsheetId=args.spreadsheet_id,
        range=f"'{tab}'!{tc_col}{args.first_row}:{tc_col}").execute()).get("values", [])
    code_row = {}
    last_row = args.first_row - 1
    for n, row in enumerate(existing):
        r = args.first_row + n
        last_row = r
        if row and row[0].strip():
            code_row[row[0].strip()] = r

    matched = [s for s in shots if s["temp_code"] in code_row]
    new = [s for s in shots if s["temp_code"] not in code_row]

    # read current cell text for matched rows in every enrichment column, so the non-clobber rule
    # (plan_enrichment_writes / plan_real_tc_writes) can tell "empty" from "operator-corrected".
    # Keyed on tcid (not temp_code) to match slate_ocr.csv / vfxnote_ocr.csv / shots' own key.
    existing_row_values = {}
    if ecidx and matched:
        cols_sorted = sorted(ecidx.items(), key=lambda kv: kv[1])
        lo = min(i for _, i in cols_sorted)
        hi = max(i for _, i in cols_sorted)
        rows_needed = sorted({code_row[s["temp_code"]] for s in matched})
        top, bot = rows_needed[0], rows_needed[-1]
        grid = _retry(lambda: sheets.spreadsheets().values().get(
            spreadsheetId=args.spreadsheet_id,
            range=f"'{tab}'!{_col_a1(lo)}{top}:{_col_a1(hi)}{bot}").execute()).get("values", [])
        row_text = {}
        for n, row in enumerate(grid):
            row_text[top + n] = row
        for s in matched:
            r = code_row[s["temp_code"]]
            row = row_text.get(r, [])
            vals = {}
            for field, i in ecidx.items():
                off = i - lo
                vals[field] = row[off] if off < len(row) else ""
            existing_row_values[s["tcid"]] = vals

    slate_by_tcid = load_slate_ocr_csv(slate_csv) if slate_csv else {}
    note_by_tcid = load_vfxnote_ocr_csv(notes_csv) if notes_csv else {}
    # FEATURE 3: bs_enrich.py descriptions.csv / dialogue.csv (auto-detected, optional -- same
    # "missing column or missing CSV = silently skipped" contract as the OCR fields above).
    desc_by_tcid = load_descriptions_csv(desc_csv) if desc_csv else {}
    dialogue_by_tcid = load_dialogue_csv(dialogue_csv) if dialogue_csv else {}
    ocr_fields = set(ecidx) & {"scene", "slate", "take", "slate_takes", "shot_code", "vfx_note",
                               "description", "dialogue"}
    enrich_writes, enrich_summary = ({}, {"enriched": 0, "operator_kept": 0, "fields_written": {}})
    if (slate_by_tcid or note_by_tcid or desc_by_tcid or dialogue_by_tcid) and ocr_fields:
        enrich_writes, enrich_summary = plan_enrichment_writes(
            shots, slate_by_tcid, note_by_tcid, existing_row_values, ocr_fields,
            desc_by_tcid=desc_by_tcid, dialogue_by_tcid=dialogue_by_tcid)

    tc_fields = set(ecidx) & {"real_tc_in", "real_tc_out"}
    offset_info = None
    if offset_txt:
        offset_info = parse_tc_offset_file(offset_txt.read_text(encoding="utf-8"))
    cfg_offset = getattr(args, "show_tc_offset", "") or ""
    fps_for_offset = getattr(args, "fps", 0) or 24.0
    real_tc_writes, real_tc_summary = ({}, {"enriched": 0, "operator_kept": 0, "fields_written": {}})
    offset_frames_used = None
    if tc_fields:
        if offset_info and offset_info.get("constant"):
            offset_frames_used = offset_info["median_offset_frames"]
        elif cfg_offset:
            try:
                offset_frames_used = _smpte_to_frames(cfg_offset, fps_for_offset)
            except Exception:
                offset_frames_used = None
        if offset_frames_used is not None:
            real_tc_writes, real_tc_summary = plan_real_tc_writes(
                shots, offset_frames_used, fps_for_offset, existing_row_values, tc_fields)
        else:
            print("[build] Real TC In/Out columns present but no usable offset "
                  "(no constant tc_offset.txt and no --show-tc-offset/config); skipped.", flush=True)

    if slate_csv or notes_csv or desc_csv or dialogue_csv:
        print(f"[build] enrichment artifacts: slate_ocr.csv={'yes' if slate_csv else 'no'} "
              f"vfxnote_ocr.csv={'yes' if notes_csv else 'no'} "
              f"descriptions.csv={'yes' if desc_csv else 'no'} "
              f"dialogue.csv={'yes' if dialogue_csv else 'no'} -> "
              f"enriched={enrich_summary['enriched']} rows, "
              f"operator_kept={enrich_summary['operator_kept']} cells "
              f"(fields: {enrich_summary['fields_written']})", flush=True)
    if tc_fields and offset_frames_used is not None:
        print(f"[build] real TC offset {_frames_to_smpte(offset_frames_used, fps_for_offset)} "
              f"({offset_frames_used} frames @ {fps_for_offset:.3f}fps) -> "
              f"enriched={real_tc_summary['enriched']} rows, "
              f"operator_kept={real_tc_summary['operator_kept']} cells", flush=True)

    if getattr(args, "dry_run", False):
        print("\n=== DRY RUN — no changes will be written ===", flush=True)
        print(f"  target: '{tab}' in {args.spreadsheet_id}", flush=True)
        print(f"  shots in local cut: {len(shots)}", flush=True)
        print(f"  already in sheet (update in place): {len(matched)}", flush=True)
        print(f"  not in sheet (would append as new rows {last_row + 1}"
              f"..{last_row + len(new)}): {len(new)}", flush=True)
        wrote = [f for f in ("temp_code", "tc_in", "duration") if cidx[f] is not None]
        if args.status:
            wrote.append("status")
        if do_thumbs:
            have = sum(1 for s in shots if (base / "thumbs" /
                       f"{args.prefix}_{s['tcid']}-mid.jpg").exists())
            wrote.append(f"thumbnail({have} imgs would upload)")
            if have > IMAGE_RENDER_CAP:
                print(f"WARNING: {have} thumbnail rows exceed the ~{IMAGE_RENDER_CAP} "
                      f"=IMAGE() render cap; only ~{IMAGE_RENDER_CAP} will display in Sheets "
                      f"(write fewer rows or paste thumbnails as values for vendor sheets).",
                      flush=True)
        elif args.thumbs and thumb_mode == "none":
            print("  (--thumb-mode none: thumbnails skipped entirely)", flush=True)
        print(f"  columns that would be written: {', '.join(wrote)}", flush=True)
        print("  (cost / producer columns are never written)", flush=True)
        if enrich_writes:
            print(f"  OCR enrichment would touch {len(enrich_writes)} row(s) "
                  f"(fields: {enrich_summary['fields_written']}); "
                  f"{enrich_summary['operator_kept']} cell(s) would be left as operator-kept",
                  flush=True)
        if real_tc_writes:
            print(f"  Real TC would touch {len(real_tc_writes)} row(s); "
                  f"{real_tc_summary['operator_kept']} cell(s) would be left as operator-kept",
                  flush=True)
        for s in new[:8]:
            print(f"    NEW   {s['temp_code']}  TC {s['tc_in']}  {s['duration']}f", flush=True)
        for s in matched[:5]:
            print(f"    UPD   {s['temp_code']} -> row {code_row[s['temp_code']]}", flush=True)
        print(f"DONE dry-run: {len(matched)} update, {len(new)} append (nothing written)", flush=True)
        return

    # thumbnails -> Drive (optional, parallel)
    thumb_url = {}
    if do_thumbs:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading
        folder = _thumb_drive_folder(drive, f"BreakdownStudio_{stem}_thumbs")
        existing = _list_folder_files(drive, folder)  # md5-aware dict, listed ONCE
        tdir = base / "thumbs"
        jobs = [(s["temp_code"], tdir / f"{args.prefix}_{s['tcid']}-mid.jpg")
                for s in shots]
        jobs = [(code, p) for code, p in jobs if p.exists()]
        total = len(jobs)
        if total > IMAGE_RENDER_CAP and thumb_mode == "image":
            print(f"WARNING: {total} thumbnail rows exceed the ~{IMAGE_RENDER_CAP} "
                  f"=IMAGE() render cap; only ~{IMAGE_RENDER_CAP} will display in Sheets "
                  f"(write fewer rows or paste thumbnails as values for vendor sheets).",
                  flush=True)
        _local = threading.local()

        def _svc():
            d = getattr(_local, "drive", None)
            if d is None:
                d = _local.drive = _drive_service(args)  # own client per thread
            return d

        def _do(code, p):
            fid = _upload_thumb(_svc(), folder, p, p.name, existing)
            return code, f"https://drive.google.com/uc?export=view&id={fid}"

        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_do, code, p) for code, p in jobs]
            for fut in as_completed(futs):
                code, url = fut.result()
                thumb_url[code] = url
                done += 1
                if done % 25 == 0 or done == total:
                    print(f"PROGRESS thumbs {done}/{total}", flush=True)
        print(f"[build] uploaded {len(thumb_url)} thumbnails", flush=True)

    # assemble cell updates (only our identity columns; never cost/producer columns)
    data = []
    appended = 0
    next_row = last_row + 1
    max_row = last_row
    for s in shots:
        code = s["temp_code"]
        if code in code_row:
            row = code_row[code]
        else:
            row = next_row
            next_row += 1
            appended += 1
        max_row = max(max_row, row)
        def put(field, value):
            i = cidx[field]
            if i is None or value in (None, ""):
                return
            data.append({"range": f"'{tab}'!{_col_a1(i)}{row}",
                         "values": [[value]]})
        put("temp_code", code)
        # leading apostrophe forces Sheets to keep TC as text (not a date/number)
        put("tc_in", "'" + s["tc_in"] if s["tc_in"] else s["tc_in"])
        put("duration", s["duration"])
        if args.status:
            put("status", args.status)
        if do_thumbs and code in thumb_url:
            put("thumbnail", f'=IMAGE("{thumb_url[code]}")')

        # FEATURE 1: OCR enrichment (Scene/Slate/Take/Slate Takes/Shot Code/VFX Notes)
        for field, value in enrich_writes.get(s["tcid"], {}).items():
            i = ecidx.get(field)
            if i is None or not value:
                continue
            data.append({"range": f"'{tab}'!{_col_a1(i)}{row}", "values": [[value]]})
        # FEATURE 2: real show TC (text values, leading apostrophe like the existing TC write)
        for field, value in real_tc_writes.get(s["tcid"], {}).items():
            i = ecidx.get(field)
            if i is None or not value:
                continue
            data.append({"range": f"'{tab}'!{_col_a1(i)}{row}", "values": [["'" + value]]})

    # grow the grid first if we are appending past the current rowCount
    sheet_id, row_count = _tab_grid(sheets, args.spreadsheet_id, tab)
    _ensure_rows(sheets, args.spreadsheet_id, sheet_id, row_count, max_row)

    # batch in chunks (USER_ENTERED so '=IMAGE() formulas and leading-' text behave)
    BATCH = 500
    for i in range(0, len(data), BATCH):
        chunk = data[i:i + BATCH]
        _retry(lambda: sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=args.spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": chunk}).execute())
        print(f"PROGRESS write {min(i + BATCH, len(data))}/{len(data)}", flush=True)
        time.sleep(0.2)

    extra = []
    if enrich_writes:
        extra.append(f"ocr_enriched={enrich_summary['enriched']} rows "
                     f"(operator_kept={enrich_summary['operator_kept']} cells)")
    if real_tc_writes:
        extra.append(f"real_tc={real_tc_summary['enriched']} rows "
                     f"(operator_kept={real_tc_summary['operator_kept']} cells)")
    print(f"DONE build: {len(shots)} shots -> updated_in_place={len(shots) - appended}, "
          f"appended={appended} (tab '{tab}')" + (f" | {', '.join(extra)}" if extra else ""),
          flush=True)


# --------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def auth_args(p):
        p.add_argument("--client-secret", default="")
        p.add_argument("--token", default="")
        p.add_argument("--service-account", default="")

    p = sub.add_parser("whoami"); auth_args(p); p.set_defaults(fn=cmd_whoami)

    p = sub.add_parser("copy-template"); auth_args(p)
    p.add_argument("--template-id", required=True)
    p.add_argument("--title", required=True)
    p.set_defaults(fn=cmd_copy_template)

    p = sub.add_parser("build"); auth_args(p)
    p.add_argument("--spreadsheet-id", required=True)
    p.add_argument("--movie", required=True)
    p.add_argument("--output-base", required=True)
    p.add_argument("--prefix", default="SHW")
    p.add_argument("--tab", default="Shots_Breakdown")
    p.add_argument("--first-row", type=int, default=3)
    p.add_argument("--thumbs", action="store_true")
    p.add_argument("--thumb-mode", choices=["image", "none"], default="image",
                   help="image: write =IMAGE() formulas (default); none: skip thumbnails entirely")
    p.add_argument("--status", default="")
    p.add_argument("--fps", type=float, default=0.0,
                   help="fps for real-TC math (default: probed from --movie, else 24.0)")
    p.add_argument("--show-tc-offset", default="",
                   help="fallback 'HH:MM:SS:FF' show-TC offset for Real TC In/Out when no "
                        "tc_offset.txt is found next to the movie's output (FEATURE 2)")
    p.add_argument("--dry-run", action="store_true",
                   help="preview adds/updates without writing anything")
    p.set_defaults(fn=cmd_build)

    args = ap.parse_args()
    args.fn(args)


def _demo_enrichment(demo_dir=None):
    """Proves FEATURE 1's enrichment join on the checked-in SYNTHETIC demo OCR CSVs
    (demo/output/demo_cut/{slate_ocr,vfxnote_ocr}.csv, prefix DEM_ -- zero client data) without
    touching any real Google Sheet: `python bs_gsheets.py --demo-enrichment`."""
    demo_dir = Path(demo_dir) if demo_dir else (APP_DIR / "demo" / "output" / "demo_cut")
    slate_csv, notes_csv = demo_dir / "slate_ocr.csv", demo_dir / "vfxnote_ocr.csv"
    if not slate_csv.exists():
        sys.exit(f"ERROR: no slate_ocr.csv under {demo_dir} -- run bs_ocr slate/notes on the "
                 f"demo cut first.")
    slate_by_tcid = load_slate_ocr_csv(slate_csv)
    note_by_tcid = load_vfxnote_ocr_csv(notes_csv) if notes_csv.exists() else {}
    shots = [{"tcid": t} for t in slate_by_tcid]
    fields = {"scene", "slate", "take", "slate_takes", "shot_code", "vfx_note"}
    writes, summary = plan_enrichment_writes(shots, slate_by_tcid, note_by_tcid, {}, fields)
    print(f"[demo] {len(shots)} shots from {slate_csv} -> enriched={summary['enriched']}, "
          f"operator_kept={summary['operator_kept']}, fields={summary['fields_written']}")
    for tcid in sorted(writes)[:5]:
        print(f"  {tcid}  {writes[tcid]}")
    print("[demo] no Google Sheet was contacted -- this only exercises the pure enrichment join.")
    return writes, summary


if __name__ == "__main__":
    if "--demo-enrichment" in sys.argv:
        _demo_enrichment()
    else:
        main()
