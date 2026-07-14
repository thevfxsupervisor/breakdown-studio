#!/usr/bin/env python
"""make_blank_template.py - make a BLANK, reusable template from a master estimate workbook.

SAFETY: the master is only ever READ and COPIED. We Drive-copy it first, then strip the COPY.
The master spreadsheet is never modified.

Strip = clear data VALUES on the shot-keyed tabs (Google's values.batchClear keeps all formatting,
column widths, frozen rows and data validation/dropdowns), blank the project-title cells, and leave
the formula rollup tabs (Summary / Sequence_Summary / Budget* / Previz) — they recompute to empty
once the shot data is gone. Reusable config (Rates, Vendor_Config) is kept as starting defaults.

Auth is shared with the app (bs_gsheets): pass --client-secret / --token, or set the env vars.

    python make_blank_template.py --master-id <ID> [--title "BLANK TEMPLATE"] \
        [--client-secret CS] [--token T] [--dry-run]

Prints the new template's spreadsheet id + url. Put that id in config.json ("template_id").

--scrub mode (generalize an already-blanked template into a NEUTRAL, SHAREABLE template)
------------------------------------------------------------------------------------------
Run after the normal copy+clear pass (or against an existing template copy) to rewrite
show-specific STRING content -- vendor company names, shot-code prefix, sequence/asset/people
names -- into generic placeholders, while leaving formula structure, formatting, validation and
conditional formatting untouched. This is a text-substitution pass, not a values-clear: it edits
labels and formula *literals* (the string constants baked inside formulas), never cell references.

    python make_blank_template.py --scrub --spreadsheet-id <ID> \
        [--prefix-from PEF] [--prefix-to SHW] \
        [--terms path/to/show_terms.local.json] \
        [--rates-mult 2.0] \
        [--client-secret CS] [--token T] [--dry-run]

Vendor names are auto-detected from the target sheet's own Vendor_Config tab (col B, rows 2-10)
and mapped to "Vendor N" using the row's own existing number -- this matches the _V{N}_Import /
_V{N}_Asset tab numbering already in the sheet, so no separate vendor list needs to be hardcoded
here or supplied externally.

The shot-code prefix (--prefix-from) is auto-detected by scanning Shots_Breakdown column B
("Shot Code (Slate)") for a repeated leading alpha token if not given explicitly.

--terms points at a JSON file (NOT checked into this repo -- it is show-specific DATA; keep it
in a gitignored *.local.json beside your working copy) with the shape:
    {
      "sequence_name_to_generic": {"Real Sequence Name": "Sequence 01", ...},
      "asset_name_to_generic":    {"Real Asset Name": "Asset A", ...},
      "people_names":             {"Real Person Name": "[Name]", ...},
      "extra_terms":              {"Any other literal string": "generic replacement", ...}
    }
All four keys are optional; missing ones are simply skipped. This file supplies the DATA; this
script only knows the generic STRUCTURAL categories (Sequence_Summary/Budget Snapshots column B
and header rows, Asset_Breakdown column A, etc.) to know WHERE to look for terms supplied by the
terms file -- it never hardcodes a specific production's names.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bs_gsheets  # noqa: E402  (shared portable auth)

DEFAULT_MASTER = os.environ.get("BS_MASTER_ID", "")  # set via --master-id or the GUI (config master_id)

CLEAR_FROM = {            # tab -> first data row (header rows above are kept)
    "Shots_Breakdown": 3,
    "_Cut4_Import": 2,
    **{f"_V{i}_Import": 3 for i in range(1, 10)},
    **{f"_V{i}_Asset": 3 for i in range(1, 10)},
}
AUTODETECT_CLEAR = ["Asset_Breakdown"]
TITLE_CELLS = {
    "Shots_Breakdown": ["A1"], "Summary": ["B1"],
    "Sequence_Summary": ["B1", "E1", "E2"], "Previz": ["B1"],
    "Asset_Breakdown": ["A1", "A2"],
}

# --------------------------------------------------------------- --scrub mode
#
# Structural knowledge only (no show terms): which tabs/cells are KNOWN to carry
# show-specific literals baked into headers or formula constants, given this app's own
# fixed sheet layout. The actual show-specific STRINGS to hunt for and their replacements
# come from Vendor_Config (auto) and the --terms JSON file (supplied at runtime).

# Shots_Breakdown row-2 header cells whose ARRAYFORMULA bakes a vendor name as a literal
# string inside the formula (rather than pulling it live from Vendor_Config, like
# Sequence_Summary!K6:S6 and Asset_Breakdown!G7:O7 already do). One (cost_col, notes_col)
# pair per vendor slot 1-7 -- slots 8/9 in this sheet's own design already resolve
# dynamically via Vendor_Config and need no literal rewrite.
_SHOTS_BREAKDOWN_VENDOR_HEADER_COLS = [
    ("AS", "AT", 1), ("AU", "AV", 2), ("AW", "AX", 3), ("AY", "AZ", 4),
    ("BA", "BB", 5), ("BC", "BD", 6), ("BE", "BF", 7),
]

RATES_LEAF_CELLS = [  # only these get *= rates_mult; B/C/D columns are formulas derived
    "E3", "E4", "E5", "E6", "E7", "E8",                       # B/C/D columns are formulas
    "E10", "E11", "E12", "E13", "E14", "E15", "E16", "E17", "E18", "E19", "E20", "E21",
]


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def _get_values(sheets, sid, rng, render="FORMATTED_VALUE"):
    return sheets.spreadsheets().values().get(
        spreadsheetId=sid, range=rng, valueRenderOption=render).execute().get("values", [])


def _detect_vendor_map(sheets, sid):
    """Read Vendor_Config!B2:B10 -> {real_vendor_name: 'Vendor N'} using the row's own
    existing number (row 2 = Vendor 1 ... row 10 = Vendor 9), so replacement numbers always
    match the sheet's own _V{N}_Import / _V{N}_Asset tab numbering."""
    rows = _get_values(sheets, sid, "Vendor_Config!B2:B10")
    vmap = {}
    for i, row in enumerate(rows, start=1):
        name = _norm(row[0]) if row else ""
        if name and not name.lower().startswith("vendor "):
            vmap[name] = f"Vendor {i}"
    return vmap


def _detect_prefix(sheets, sid, col="B", first_row=3, sample=200):
    """Best-effort auto-detect of the shot-code prefix: the repeated leading alpha token
    in Shots_Breakdown's Shot Code (Slate) column, e.g. 'SHW_0123_010' -> 'SHW'."""
    rng = f"Shots_Breakdown!{col}{first_row}:{col}{first_row + sample}"
    rows = _get_values(sheets, sid, rng)
    counts = {}
    for row in rows:
        if not row or not row[0]:
            continue
        m = re.match(r"^([A-Za-z]{2,6})[_\-]", str(row[0]).strip())
        if m:
            tok = m.group(1).upper()
            counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _load_terms(path):
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: --terms file not found: {path}")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data


def cmd_scrub(args):
    sheets, drive = bs_gsheets.services(args)
    sid = args.spreadsheet_id
    if not sid:
        sys.exit("ERROR: --scrub requires --spreadsheet-id (the template COPY, never the master).")

    terms = _load_terms(args.terms)
    seq_map = terms.get("sequence_name_to_generic", {})
    asset_map = terms.get("asset_name_to_generic", {})
    people_map = terms.get("people_names", {})
    extra_map = terms.get("extra_terms", {})

    print("[scrub 1/5] detecting vendor names from Vendor_Config ...", flush=True)
    vendor_map = _detect_vendor_map(sheets, sid)
    for real, generic in vendor_map.items():
        print(f"   {real!r} -> {generic}")

    prefix_from = args.prefix_from or _detect_prefix(sheets, sid)
    prefix_to = args.prefix_to
    if prefix_from:
        print(f"[scrub 2/5] shot-code prefix: {prefix_from!r} -> {prefix_to!r}")
    else:
        print("[scrub 2/5] shot-code prefix: none detected, skipping prefix swap")

    updates = []   # list of {"range":..., "values":[[...]]}
    log = []       # (range, category, old, new) for the summary print

    def stage(rng, new_value, category, old_value=""):
        updates.append({"range": rng, "values": [[new_value]]})
        log.append((rng, category, old_value, new_value))

    # --- Vendor_Config: rename + clear real sheet URLs -------------------------------
    vrows = _get_values(sheets, sid, "Vendor_Config!B2:C10")
    for i, row in enumerate(vrows, start=2):
        name = _norm(row[0]) if row else ""
        if name and name in vendor_map:
            stage(f"Vendor_Config!B{i}", vendor_map[name], "vendor_name", name)
        if len(row) > 1 and row[1]:
            stage(f"Vendor_Config!C{i}", "", "vendor_sheet_url_cleared", row[1])

    # --- Shots_Breakdown: vendor names baked as literals inside formula text ---------
    if vendor_map:
        # invert: numeric slot -> generic label, using the _V{N}_Import mapping this app
        # itself owns (slot N always reads from '_V{N}_Import')
        name_by_slot = {}
        for real, generic in vendor_map.items():
            m = re.match(r"Vendor (\d+)", generic)
            if m:
                name_by_slot[int(m.group(1))] = (real, generic)
        cells = [f"{c}2" for c, _, _ in _SHOTS_BREAKDOWN_VENDOR_HEADER_COLS] + \
                [f"{n}2" for _, n, _ in _SHOTS_BREAKDOWN_VENDOR_HEADER_COLS]
        resp = sheets.spreadsheets().values().batchGet(
            spreadsheetId=sid,
            ranges=[f"Shots_Breakdown!{c}" for c in cells],
            valueRenderOption="FORMULA").execute()
        formula_by_cell = {}
        for vr in resp.get("valueRanges", []):
            cell = vr["range"].split("!")[-1]
            vals = vr.get("values", [])
            formula_by_cell[cell] = vals[0][0] if vals and vals[0] else ""
        for cost_col, notes_col, slot in _SHOTS_BREAKDOWN_VENDOR_HEADER_COLS:
            if slot not in name_by_slot:
                continue
            real, generic = name_by_slot[slot]
            for col in (cost_col, notes_col):
                cell = f"{col}2"
                old_formula = formula_by_cell.get(cell, "")
                if real and real in old_formula:
                    new_formula = old_formula.replace(f'"{real} ', f'"{generic} ')
                    if new_formula != old_formula:
                        stage(f"Shots_Breakdown!{cell}", new_formula,
                              "vendor_name_in_formula_literal", old_formula)

    # --- shot-code prefix: swap literal in any formula/value containing it -----------
    if prefix_from and prefix_from != prefix_to:
        # Known home: any formula concatenating the prefix literal, e.g. ="SHW_"&...
        # Scan Shots_Breakdown row 1-2 and Vendor_Config/List Assumptions header rows
        # (cheap, bounded ranges) for the literal; this is a best-effort sweep, not a
        # full-sheet scan (the caller can re-run --scrub with a narrower --terms extra_terms
        # entry for anything this misses).
        scan_ranges = ["Shots_Breakdown!A1:BL2"]
        for rng in scan_ranges:
            resp = sheets.spreadsheets().values().get(
                spreadsheetId=sid, range=rng, valueRenderOption="FORMULA").execute()
            rows = resp.get("values", [])
            tab = rng.split("!")[0]
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    if cell and prefix_from in str(cell):
                        col = colname(ci + 1)
                        new_val = str(cell).replace(prefix_from, prefix_to)
                        stage(f"{tab}!{col}{ri+1}", new_val, "shot_code_prefix", cell)

    # --- sequence / asset / people names + free-form extra terms ----------------------
    combined_terms = {}
    combined_terms.update(seq_map)
    combined_terms.update(asset_map)
    combined_terms.update(people_map)
    combined_terms.update(extra_map)
    if combined_terms:
        # Known structural homes for sequence/asset labels in this app's fixed layout.
        term_scan_ranges = [
            "Sequence_Summary!B1:B60", "Budget Snapshots!A1:AK1",
            "Asset_Breakdown!A1:A1004", "Previz!A1:V70", "Summary!A1:H30",
            "VFX Producer Work!A1:H5", "Rates!A1:F26", "Budget Versions!A1:L45",
            "List Assumptions!E1:E15",
        ]
        for rng in term_scan_ranges:
            resp = sheets.spreadsheets().values().get(
                spreadsheetId=sid, range=rng, valueRenderOption="FORMATTED_VALUE").execute()
            rows = resp.get("values", [])
            tab = rng.split("!")[0]
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    if not cell:
                        continue
                    text = str(cell)
                    replaced = text
                    hit_cat = None
                    for old, new in combined_terms.items():
                        if old and old in replaced:
                            replaced = replaced.replace(old, new)
                            hit_cat = "term_swap"
                    if hit_cat and replaced != text:
                        col = colname(ci + 1)
                        stage(f"{tab}!{col}{ri+1}", replaced, hit_cat, text)

    print(f"[scrub 3/5] staged {len(updates)} cell updates ...", flush=True)
    if args.dry_run:
        for rng, cat, old, new in log:
            print(f"   DRY RUN would write {rng} [{cat}]: {old!r} -> {new!r}")
    else:
        for i in range(0, len(updates), 200):
            chunk = updates[i:i + 200]
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "USER_ENTERED", "data": chunk}).execute()

    # --- Rates multiplier (leaf literals only, col E) ----------------------------------
    if args.rates_mult and args.rates_mult != 1.0:
        print(f"[scrub 4/5] applying Rates multiplier x{args.rates_mult} ...", flush=True)
        rate_cells = [f"Rates!{c}" for c in RATES_LEAF_CELLS]
        resp = sheets.spreadsheets().values().batchGet(
            spreadsheetId=sid, ranges=rate_cells, valueRenderOption="FORMULA").execute()
        rate_updates = []
        for vr in resp.get("valueRanges", []):
            cell = vr["range"].split("!")[-1]
            vals = vr.get("values", [])
            v = vals[0][0] if vals and vals[0] else None
            if isinstance(v, (int, float)):
                rate_updates.append({"range": f"Rates!{cell}", "values": [[v * args.rates_mult]]})
            elif isinstance(v, str) and not v.startswith("="):
                try:
                    num = float(v.replace(",", ""))
                    rate_updates.append({"range": f"Rates!{cell}", "values": [[num * args.rates_mult]]})
                except ValueError:
                    pass  # not a bare numeric leaf, skip rather than risk double-doubling a formula
        if not args.dry_run and rate_updates:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "USER_ENTERED", "data": rate_updates}).execute()
        print(f"   {'would update' if args.dry_run else 'updated'} {len(rate_updates)} Rates leaf cells")
    else:
        print("[scrub 4/5] --rates-mult is 1.0, no Rates change")

    print(f"[scrub 5/5] done. {len(log)} label/formula-literal edits "
          f"{'staged (dry run)' if args.dry_run else 'applied'}.")
    by_cat = {}
    for _, cat, _, _ in log:
        by_cat[cat] = by_cat.get(cat, 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"   {cat}: {n}")


def colname(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master-id", default=DEFAULT_MASTER)
    ap.add_argument("--title", default="BLANK TEMPLATE - Shot Breakdown & Estimate")
    ap.add_argument("--client-secret", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--service-account", default="")
    ap.add_argument("--dry-run", action="store_true")
    # --scrub mode
    ap.add_argument("--scrub", action="store_true",
                     help="After the normal copy+clear pass, also generalize show-specific "
                          "labels/formula-literals (vendor names, shot-code prefix, sequence/"
                          "asset/people names) into neutral placeholders. Can also be run alone "
                          "against an existing template copy via --spreadsheet-id.")
    ap.add_argument("--spreadsheet-id", default="",
                     help="Run --scrub against this existing spreadsheet instead of a fresh "
                          "copy+clear pass (skips step 1/2/3, goes straight to scrubbing).")
    ap.add_argument("--prefix-from", default="",
                     help="Shot-code prefix literal to replace (auto-detected from "
                          "Shots_Breakdown column B if omitted).")
    ap.add_argument("--prefix-to", default="SHW",
                     help="Generic shot-code prefix to write in its place (default SHW).")
    ap.add_argument("--terms", default="",
                     help="Path to a JSON file (show-specific DATA, gitignored, NOT this repo's "
                          "code) supplying sequence/asset/people-name replacements and any extra "
                          "free-form term swaps. See template_scrub_terms.local.json for shape.")
    ap.add_argument("--rates-mult", type=float, default=1.0,
                     help="Multiply Rates tab leaf currency literals by this factor "
                          "(default 1.0 = no change; e.g. 2.0 to ship safely-inflated defaults).")
    args = ap.parse_args()

    if args.scrub and args.spreadsheet_id and not args.master_id:
        # scrub-only mode: operate directly on an existing copy, skip copy+clear entirely
        cmd_scrub(args)
        return

    sheets, drive = bs_gsheets.services(args)
    master = args.master_id

    print(f"[1/4] copying master (read-only) -> '{args.title}' ...", flush=True)
    if args.dry_run:
        sid = master
        print("  DRY RUN: would Drive-copy", master)
    else:
        new = drive.files().copy(fileId=master, body={"name": args.title},
                                 fields="id,webViewLink").execute()
        sid = new["id"]
        print(f"  copied -> {sid}", flush=True)

    meta = sheets.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets.properties(title,gridProperties(rowCount,columnCount))").execute()
    dims = {s["properties"]["title"]:
            (s["properties"]["gridProperties"]["rowCount"],
             s["properties"]["gridProperties"]["columnCount"]) for s in meta["sheets"]}

    ranges = []
    for tab, first in CLEAR_FROM.items():
        if tab in dims:
            rows, cols = dims[tab]
            ranges.append(f"'{tab}'!A{first}:{colname(cols)}{rows}")
    for tab in AUTODETECT_CLEAR:
        if tab not in dims:
            continue
        rows, cols = dims[tab]
        top = sheets.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{tab}'!A1:{colname(cols)}8").execute().get("values", [])
        best_i, best_n = 0, -1
        for i, r in enumerate(top):
            n = sum(1 for c in r if str(c).strip())
            if n > best_n:
                best_i, best_n = i, n
        ranges.append(f"'{tab}'!A{best_i + 2}:{colname(cols)}{rows}")
        print(f"  [autodetect] {tab}: header row {best_i + 1} -> clear from row {best_i + 2}")

    print(f"[2/4] clearing {len(ranges)} data ranges (values only; formatting kept) ...", flush=True)
    if not args.dry_run:
        sheets.spreadsheets().values().batchClear(
            spreadsheetId=sid, body={"ranges": ranges}).execute()

    print("[3/4] blanking project-title cells ...", flush=True)
    title_ranges = [f"'{t}'!{c}" for t, cells in TITLE_CELLS.items() if t in dims for c in cells]
    if not args.dry_run:
        sheets.spreadsheets().values().batchClear(
            spreadsheetId=sid, body={"ranges": title_ranges}).execute()

    print("[4/4] verifying ...", flush=True)
    if args.dry_run:
        for r in ranges + title_ranges:
            print("   would clear", r)
        print("  DRY RUN complete (master untouched).")
        return
    hdr = sheets.spreadsheets().values().get(
        spreadsheetId=sid, range="'Shots_Breakdown'!A2:D2").execute().get("values", [[]])[0]
    codes = sheets.spreadsheets().values().get(
        spreadsheetId=sid, range="'Shots_Breakdown'!G3:G").execute().get("values", [])
    nonempty = sum(1 for r in codes if r and r[0].strip())
    print(f"  Shots_Breakdown header={hdr}  remaining shot codes={nonempty}")
    print(f"\nTEMPLATE_ID {sid}\nURL https://docs.google.com/spreadsheets/d/{sid}/edit")
    print('\nNext: put this id in config.json as "template_id".')

    if args.scrub:
        print("\n[scrub] --scrub given, generalizing the fresh copy ...", flush=True)
        args.spreadsheet_id = sid
        cmd_scrub(args)


if __name__ == "__main__":
    main()
