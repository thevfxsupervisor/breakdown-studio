#!/usr/bin/env python
"""bs_miro.py - push a built breakdown Google Sheet to a Miro board as a shot-wall.

Each shot becomes a FRAME (title = shot label, fill = type colour) with its thumbnail IMAGE
nested inside, and the frame+image bound in a native Miro GROUP so the tile moves as one unit.
Tiles are laid out in a chronological grid, colour-coded by a status/type column, with a legend.
Everything is tracked in a board-state JSON so re-syncs rename/recolour tiles IN PLACE and append
new shots without disturbing a human's manual arrangement.

Columns are resolved BY HEADER NAME (operators reorder/rename columns), so a reordered sheet works.

Subcommands
  push      Create the wall (frames + thumbnails + groups) from the sheet. Idempotent: skips shots
            already in the board-state; appends genuinely new ones in a staging strip.
  resync    Re-read the sheet and rename frame titles + recolour IN PLACE (positions preserved);
            append new shots. Never repositions existing tiles.
  verify    Count frames/images/groups and confirm each tile is a real frame+image group
            (uses GET /groups/{id} - the /groups LIST endpoint under-reports membership).
  cluster   Optional. Arrange a COPY of the shots off to the side, grouped into columns by CLIP
            visual similarity (needs torch+transformers; the base subcommands do not).

Auth / config (all overridable on the CLI; the GUI passes them from config.json):
  Google (to read the sheet): --client-secret / --token / --service-account (same as bs_gsheets).
  Miro:  --miro-token  (a boards:write REST token string, or a path to a file holding it; the
                         /mcp OAuth token does NOT work against api.miro.com - use a real REST token)
         --miro-board  (board id, e.g. the token after /board/ in the Miro URL)
  --state PATH  board-state JSON (default: ./miro_board_state.json)

Run:  <worker_python> bs_miro.py push --spreadsheet-id ID --miro-token TOK --miro-board BID
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
API = "https://api.miro.com/v2"

# --- header resolution (resolve BY HEADER NAME; first present candidate wins) ------------------
CODE_HEADERS = ["Shot Code (Slate)", "Shot Code", "Code", "Shot", "Shotname"]
LABEL_HEADERS = ["Shotname (Plate)", "Shot Code (Slate)", "Shot Code", "Code"]
THUMB_HEADERS = ["Thumbnail", "Thumb URL (chosen)", "Thumbnail URL", "Thumb", "Chosen Thumb"]
STATUS_HEADERS = ["Status", "Type", "VFX/Online", "Shot Type"]
TC_HEADERS = ["Real TC In", "TC In", "Timecode In", "TC", "Order"]

# default type -> fill colour (normalised UPPER status); anything else gets FALLBACK_COLOR
DEFAULT_COLORS = {"VFX": "#D6E4FF", "ONLINE": "#FFE0B2", "OMIT": "#ECECEC",
                  "PRACTICAL": "#ECECEC", "OMITTED OR PRACTICAL": "#ECECEC"}
FALLBACK_COLOR = "#E9E9E9"

# grid geometry (board-absolute, frame centre origin)
COLS = 11
FW, FH = 300, 200
PITCH_X, PITCH_Y = 340, 310
OX, OY = -1700, -400
IMG_W = 270  # thumbnail width inside the frame


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def _extract_url(cell_formula_or_value):
    """Pull an http(s) URL out of an =IMAGE("...") formula or a raw URL cell."""
    if not cell_formula_or_value:
        return ""
    m = re.search(r'https?://[^\s"\')]+', str(cell_formula_or_value))
    return m.group(0) if m else ""


def _tc_to_frames(tc, fps=24.0):
    m = re.match(r'^\s*(\d+):(\d+):(\d+)[:;](\d+)\s*$', str(tc or ""))
    if not m:
        return None
    h, mi, s, f = map(int, m.groups())
    return int(((h * 60 + mi) * 60 + s) * fps + f)


# ---------------------------------------------------------------------------------------------
# Miro REST client (frames / images / groups) with retry+backoff
# ---------------------------------------------------------------------------------------------
class Miro:
    def __init__(self, token, board_id):
        self.tok = token
        self.board = urllib.parse.quote(board_id, safe="")

    def _req(self, method, path, body=None, tries=6):
        url = API + path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", "Bearer " + self.tok)
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                r = urllib.request.urlopen(req, timeout=60)
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                code = e.code
                txt = e.read().decode()[:300]
                if code == 429 or code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return code, txt
            except Exception as e:  # transient network
                time.sleep(1.5 * (attempt + 1))
                if attempt == tries - 1:
                    return "ERR", str(e)[:200]
        return "RETRY_EXHAUSTED", ""

    def check(self):
        st, resp = self._req("GET", f"/boards/{self.board}")
        return st == 200, (resp.get("name") if isinstance(resp, dict) else resp)

    def create_frame(self, title, x, y, fill, w=FW, h=FH):
        body = {"data": {"title": title, "format": "custom", "type": "freeform"},
                "style": {"fillColor": fill},
                "position": {"x": x, "y": y, "origin": "center"},
                "geometry": {"width": w, "height": h}}
        return self._req("POST", f"/boards/{self.board}/frames", body)

    def create_image(self, url, frame_id, x=None, y=None, w=IMG_W):
        pos = {"x": FW / 2 if x is None else x, "y": FH / 2 if y is None else y}
        body = {"data": {"url": url}, "position": pos, "geometry": {"width": w},
                "parent": {"id": int(frame_id)}}
        return self._req("POST", f"/boards/{self.board}/images", body)

    def create_group(self, frame_id, image_id):
        return self._req("POST", f"/boards/{self.board}/groups",
                         {"data": {"items": [int(frame_id), int(image_id)]}})

    def update_frame(self, frame_id, title=None, fill=None, x=None, y=None):
        body = {}
        if title is not None:
            body["data"] = {"title": title}
        if fill is not None:
            body["style"] = {"fillColor": fill}
        if x is not None and y is not None:
            body["position"] = {"x": x, "y": y, "origin": "center"}
        return self._req("PATCH", f"/boards/{self.board}/frames/{frame_id}", body)

    def get_group(self, gid):
        return self._req("GET", f"/boards/{self.board}/groups/{gid}")

    def create_text(self, text, x, y, w=600, size=24):
        body = {"data": {"content": text}, "position": {"x": x, "y": y, "origin": "center"},
                "geometry": {"width": w}, "style": {"fontSize": str(size)}}
        return self._req("POST", f"/boards/{self.board}/texts", body)


# ---------------------------------------------------------------------------------------------
# sheet reading (resolve columns by header)
# ---------------------------------------------------------------------------------------------
def _svc(args):
    from bs_gsheets import get_credentials
    from googleapiclient.discovery import build
    creds = get_credentials(getattr(args, "client_secret", "") or None,
                            getattr(args, "token", "") or None,
                            getattr(args, "service_account", "") or None)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _pick(hmap, candidates, override=""):
    if override:
        return hmap.get(_norm(override))
    for c in candidates:
        if _norm(c) in hmap:
            return hmap[_norm(c)]
    return None


def read_sheet_shots(args):
    """Return [{code,label,thumb_url,status,tc,order}], sorted chronologically."""
    svc = _svc(args)
    tab = args.tab
    header_row = args.header_row
    # values (for text cols) + formulas (for the =IMAGE thumbnail)
    rng = f"'{tab}'!A{header_row}:BZ"
    vals = svc.spreadsheets().values().get(spreadsheetId=args.spreadsheet_id, range=rng).execute().get("values", [])
    forms = svc.spreadsheets().values().get(spreadsheetId=args.spreadsheet_id, range=rng,
                                            valueRenderOption="FORMULA").execute().get("values", [])
    if not vals:
        sys.exit(f"ERROR: no rows at '{tab}'!row{header_row}+")
    header = [_norm(h) for h in vals[0]]
    hmap = {h: i for i, h in enumerate(header) if h}
    ci_code = _pick(hmap, CODE_HEADERS, args.code_col)
    ci_label = _pick(hmap, LABEL_HEADERS, args.label_col)
    ci_thumb = _pick(hmap, THUMB_HEADERS, args.thumb_col)
    ci_status = _pick(hmap, STATUS_HEADERS, args.status_col)
    ci_tc = _pick(hmap, TC_HEADERS, args.tc_col)
    if ci_code is None:
        sys.exit(f"ERROR: no shot-code column found (looked for {CODE_HEADERS} or --code-col). "
                 f"Headers seen: {list(hmap)[:20]}")
    if ci_thumb is None:
        sys.exit(f"ERROR: no thumbnail column found (looked for {THUMB_HEADERS} or --thumb-col).")

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) and row[i] else ""

    shots = []
    for r in range(1, len(vals)):
        row = vals[r]
        frow = forms[r] if r < len(forms) else []
        code = cell(row, ci_code)
        if not code:
            continue
        url = _extract_url(frow[ci_thumb] if ci_thumb < len(frow) else "") or _extract_url(cell(row, ci_thumb))
        if not url:
            continue
        status = cell(row, ci_status) if ci_status is not None else ""
        tc = cell(row, ci_tc) if ci_tc is not None else ""
        label = cell(row, ci_label) if ci_label is not None else ""
        shots.append({"code": code, "label": label or code, "thumb_url": url,
                      "status": status, "tc": tc, "tcf": _tc_to_frames(tc, args.fps),
                      "row": r + header_row})
    only = set(x.strip().upper() for x in (args.only_status or "").split(",") if x.strip())
    if only:
        shots = [s for s in shots if s["status"].strip().upper() in only]
    shots.sort(key=lambda s: (s["tcf"] is None, s["tcf"] if s["tcf"] is not None else 0, s["code"]))
    for i, s in enumerate(shots):
        s["order"] = i
    return shots


def colour_for(status, colors):
    return colors.get(_norm(status).upper(), FALLBACK_COLOR)


def cell_xy(order):
    return OX + (order % COLS) * PITCH_X, OY + (order // COLS) * PITCH_Y


# ---------------------------------------------------------------------------------------------
# board-state
# ---------------------------------------------------------------------------------------------
def load_state(path):
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"board": "", "frames": {}}


def save_state(path, st):
    Path(path).write_text(json.dumps(st, indent=1), encoding="utf-8")


def resolve_token(tok):
    """--miro-token may be the literal token or a path to a file holding it."""
    tok = tok or os.environ.get("BS_MIRO_TOKEN", "")
    if tok and Path(tok).expanduser().exists():
        return Path(tok).expanduser().read_text(encoding="utf-8").strip()
    return tok.strip()


def _colors_from(args):
    colors = dict(DEFAULT_COLORS)
    if args.type_colors:
        for pair in args.type_colors.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                colors[_norm(k).upper()] = v.strip()
    return colors


def _need_miro(args):
    tok = resolve_token(args.miro_token)
    if not tok:
        sys.exit("ERROR: no Miro REST token. Pass --miro-token (a boards:write token or a file path). "
                 "The /mcp OAuth token does NOT work against api.miro.com.")
    if not args.miro_board:
        sys.exit("ERROR: no --miro-board id.")
    m = Miro(tok, args.miro_board)
    ok, name = m.check()
    if not ok:
        sys.exit(f"ERROR: Miro board not reachable ({name}). Check token scope (boards:write) and board id.")
    print(f"[miro] board OK: {name}", flush=True)
    return m


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
def cmd_push(args):
    m = _need_miro(args)
    colors = _colors_from(args)
    shots = read_sheet_shots(args)
    print(f"[push] {len(shots)} shots from sheet", flush=True)
    st = load_state(args.state)
    st["board"] = args.miro_board
    frames = st.setdefault("frames", {})
    # legend (once): a text header + one swatch per distinct status
    if not st.get("legend_done") and not args.no_legend:
        statuses = sorted(set(s["status"] for s in shots if s["status"]))
        m.create_text(args.title or "Shot wall", OX, OY - 340, w=1400, size=34)
        for i, sname in enumerate(statuses):
            m.create_frame(f"{sname}", OX - 20 + i * 360, OY - 250, colour_for(sname, colors), w=120, h=60)
        st["legend_done"] = True
    new = staged = 0
    stage_row = max([f["order"] for f in frames.values()] + [len(shots)]) // COLS + 3
    for s in shots:
        code = s["code"]
        fr = frames.get(code)
        if fr and fr.get("frame_id"):
            continue  # already on the board
        # new shot: place in-order if we are building fresh, else in a staging strip below
        building_fresh = not frames
        if building_fresh:
            cx, cy = cell_xy(s["order"])
        else:
            cx, cy = OX + (staged % COLS) * PITCH_X, OY + (stage_row + staged // COLS) * PITCH_Y
            staged += 1
        fill = colour_for(s["status"], colors)
        title = s["label"] + (f"  ({s['status']})" if s["status"] and args.status_suffix else "")
        code_st, resp = m.create_frame(title, cx, cy, fill)
        if code_st != 201:
            print(f"  ! frame {code}: {code_st} {resp}", flush=True)
            continue
        fid = resp["id"]
        img_st, iresp = m.create_image(s["thumb_url"], fid)
        iid = iresp["id"] if img_st == 201 else None
        if img_st != 201:
            print(f"  ! image {code}: {img_st} {iresp}", flush=True)
        gid = None
        if iid:
            g_st, gresp = m.create_group(fid, iid)
            gid = gresp["id"] if g_st == 201 else None
        frames[code] = {"frame_id": fid, "image_id": iid, "group_id": gid,
                        "status": s["status"], "tc": s["tc"], "order": s["order"],
                        "label": s["label"], "title_now": title}
        new += 1
        if new % 25 == 0:
            save_state(args.state, st)
            print(f"  .. {new} tiles", flush=True)
    save_state(args.state, st)
    print(f"[push] created {new} tiles ({staged} staged as new) -> {args.state}", flush=True)


def cmd_resync(args):
    m = _need_miro(args)
    colors = _colors_from(args)
    shots = {s["code"]: s for s in read_sheet_shots(args)}
    st = load_state(args.state)
    frames = st.get("frames", {})
    renamed = recolored = missing = 0
    for code, fr in frames.items():
        s = shots.get(code)
        if not s or not fr.get("frame_id"):
            missing += 1
            continue
        title = s["label"] + (f"  ({s['status']})" if s["status"] and args.status_suffix else "")
        fill = colour_for(s["status"], colors)
        need_title = fr.get("title_now") != title
        need_fill = fr.get("status") != s["status"]
        if not (need_title or need_fill):
            continue
        st_code, _ = m.update_frame(fr["frame_id"], title=title if need_title else None,
                                    fill=fill if need_fill else None)  # position omitted -> not moved
        if st_code == 200:
            if need_title:
                fr["title_now"] = title
                fr["label"] = s["label"]
                renamed += 1
            if need_fill:
                fr["status"] = s["status"]
                recolored += 1
    # append genuinely new shots (not repositioning existing ones)
    new_codes = [c for c in shots if c not in frames]
    if new_codes and not args.no_append:
        args._append = True
        cmd_push(args)  # push skips existing, stages new
        return
    save_state(args.state, st)
    print(f"[resync] renamed {renamed}, recoloured {recolored}, no-frame {missing}, "
          f"new(not appended) {len(new_codes)}", flush=True)


def cmd_verify(args):
    m = _need_miro(args)
    st = load_state(args.state)
    frames = st.get("frames", {})
    ok = bad = nogrp = 0
    bad_list = []
    for code, fr in frames.items():
        gid = fr.get("group_id")
        if not gid:
            nogrp += 1
            continue
        g_st, g = m.get_group(gid)
        items = set(str(x) for x in (g.get("data", {}) or {}).get("items", [])) if isinstance(g, dict) else set()
        need = {str(fr.get("frame_id")), str(fr.get("image_id"))}
        if g_st == 200 and need <= items:
            ok += 1
        else:
            bad += 1
            bad_list.append(code)
    print(f"[verify] tiles={len(frames)}  grouped-ok={ok}  bad={bad}  no-group={nogrp}", flush=True)
    if bad_list:
        print("  bad:", bad_list[:20], flush=True)
    return 0 if bad == 0 else 1


def cmd_cluster(args):
    """Arrange a COPY of the shots off to the side, grouped in columns by CLIP visual similarity.
    Chronological walk: consecutive shots whose cosine similarity to the running column centroid
    stays >= --sim-threshold share a column; a drop starts a new column. Online/excluded status
    shots go into their own trailing column group. Needs torch+transformers (optional)."""
    try:
        import numpy as np  # noqa
        import torch  # noqa
        from transformers import CLIPModel, CLIPProcessor  # noqa
    except Exception as e:
        sys.exit(f"ERROR: --cluster needs numpy+torch+transformers (CLIP). Not available: {e}. "
                 f"The push/resync/verify subcommands do not need them.")
    from clip_similarity import embed_urls, walk_columns  # thin optional helper (see file)
    m = _need_miro(args)
    colors = _colors_from(args)
    shots = read_sheet_shots(args)
    excl = set(x.strip().upper() for x in (args.cluster_exclude or "ONLINE").split(",") if x.strip())
    main_shots = [s for s in shots if s["status"].strip().upper() not in excl]
    excl_shots = [s for s in shots if s["status"].strip().upper() in excl]
    print(f"[cluster] embedding {len(main_shots)} shots ({len(excl_shots)} excluded to own group)", flush=True)
    embs = embed_urls([s["thumb_url"] for s in main_shots])
    columns = walk_columns(main_shots, embs, threshold=args.sim_threshold)
    if excl_shots:
        columns.append(excl_shots)  # excluded statuses = their own trailing column-group
    # lay out the COPY to the right of the main grid
    ox = args.copy_origin_x
    st = load_state(args.state)
    copy = st.setdefault("cluster_copy", {"columns": []})
    cw, ch, gap = 240, 135, 20
    for ci, col in enumerate(columns):
        x = ox + ci * (cw + 60)
        m.create_text(f"Col {ci + 1}", x + cw / 2, OY - 60, w=cw, size=18)
        for ri, s in enumerate(col):
            y = OY + ri * (ch + gap)
            st_i, resp = m.create_image(s["thumb_url"], None) if False else (None, None)
            # copy images are board-level (no frame parent): POST /images without parent
            body = {"data": {"url": s["thumb_url"]},
                    "position": {"x": x + cw / 2, "y": y + ch / 2, "origin": "center"},
                    "geometry": {"width": cw}}
            code_i, iresp = m._req("POST", f"/boards/{m.board}/images", body)
            if code_i != 201:
                print(f"  ! copy img {s['code']}: {code_i} {iresp}", flush=True)
        copy["columns"].append([s["code"] for s in col])
        print(f"  col {ci + 1}: {len(col)} shots", flush=True)
    save_state(args.state, st)
    print(f"[cluster] {len(columns)} columns placed (copy) -> {args.state}", flush=True)


# ---------------------------------------------------------------------------------------------
def _add_common(p):
    p.add_argument("--spreadsheet-id", dest="spreadsheet_id", required=True)
    p.add_argument("--tab", default="Shots_Breakdown")
    p.add_argument("--header-row", dest="header_row", type=int, default=2)
    p.add_argument("--fps", type=float, default=24.0)
    p.add_argument("--miro-token", dest="miro_token", default="")
    p.add_argument("--miro-board", dest="miro_board", default="")
    p.add_argument("--state", default="miro_board_state.json")
    p.add_argument("--title", default="Shot wall")
    p.add_argument("--type-colors", dest="type_colors", default="",
                   help="STATUS=hex,STATUS=hex overrides, e.g. 'VFX=#D6E4FF,Online=#FFE0B2'")
    p.add_argument("--status-suffix", dest="status_suffix", action="store_true",
                   help="append '(Status)' to the frame title")
    p.add_argument("--only-status", dest="only_status", default="",
                   help="comma statuses to include (e.g. 'VFX,Online'); blank = all")
    p.add_argument("--code-col", dest="code_col", default="")
    p.add_argument("--label-col", dest="label_col", default="")
    p.add_argument("--thumb-col", dest="thumb_col", default="")
    p.add_argument("--status-col", dest="status_col", default="")
    p.add_argument("--tc-col", dest="tc_col", default="")
    p.add_argument("--client-secret", dest="client_secret", default="")
    p.add_argument("--token", default="")
    p.add_argument("--service-account", dest="service_account", default="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push", help="create the shot-wall (frames+thumbnails+groups)")
    _add_common(p)
    p.add_argument("--no-legend", dest="no_legend", action="store_true")
    p.set_defaults(fn=cmd_push, _append=False)

    p = sub.add_parser("resync", help="rename+recolour tiles in place from the sheet; append new")
    _add_common(p)
    p.add_argument("--no-legend", dest="no_legend", action="store_true")
    p.add_argument("--no-append", dest="no_append", action="store_true")
    p.set_defaults(fn=cmd_resync, _append=False)

    p = sub.add_parser("verify", help="counts + per-tile group membership check")
    _add_common(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("cluster", help="[optional CLIP] copy shots into visual-similarity columns")
    _add_common(p)
    p.add_argument("--sim-threshold", dest="sim_threshold", type=float, default=0.75)
    p.add_argument("--cluster-exclude", dest="cluster_exclude", default="Online",
                   help="statuses kept out of the visual clustering, placed in their own group")
    p.add_argument("--copy-origin-x", dest="copy_origin_x", type=float, default=3200.0)
    p.set_defaults(fn=cmd_cluster)

    args = ap.parse_args()
    rc = args.fn(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
