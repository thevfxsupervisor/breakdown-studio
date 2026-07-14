# Breakdown Studio: project brief for your AI coding assistant

Drop this file at the root of your breakdown project so your assistant (Claude Code, or any
AI pair) starts every session already knowing how the pipeline works, what it must never do, and
where its judgment is needed. Breakdown Studio automates the **mechanical** half of a VFX shot
breakdown. The **judgment** half is human-in-the-loop, and this brief is what lets an assistant
help with it safely.

Rename the shot-code prefix (`SHW_` below) to your show's, fill in your sheet IDs where marked
`<...>`, and delete anything that does not apply. Everything here is generic; no client data.

---

## What the tool does vs. what you do

**The app does the mechanical work** (repeatable, no judgment):

```
Detect (TransNetV2) -> Frames (start/mid/end) -> Thumbnails -> Cut clips
    -> Contact sheet -> Reference clips -> Build/update Google breakdown sheet
```

**You + your assistant do the judgment work** (no button does these well):

- Deciding whether a shot that vanished from the new cut is a genuine drop or a protected
  invisible-VFX shot that must be kept.
- Matching this cut's shots to the master breakdown when codes have drifted.
- Merging detector over-splits and catching missed cuts.
- Reconciling changed editorial notes without destroying producer annotations.
- Updating the master breakdown behind an approval gate.

Your assistant's job is to prepare these decisions (stage proposals, flag conflicts, explain
its reasoning) and never to apply an irreversible change to the master without an explicit gate.

---

## Shot identity (read this before touching any matching logic)

- **`tcid`** = `HHMMSSFF` of a shot's start timecode. It is the file-relative id used to name all
  artifacts (`SHW_<tcid>`). It is stable *within one cut* but changes when the edit changes, so it
  is **not** a cross-cut identity.
- **Slate** (the on-set clapper burn-in) is a **photographic property of the take**. It is stable
  across cuts, re-grades, temp VFX, and subtitles. It is the real cross-cut identity anchor.
- **Timecode is per-edit. Never match shots across cuts by timecode.** Every new cut re-times
  everything; TC alignment between two cuts is coincidence, not identity.
- **Official shot code** scheme: `SHW_<slate:04>_<counter:03>` (zero-padded), derived from the
  slate. The counter orders the pieces cut from one slate in edit order. A multi-take slate is a
  meaningful signal (often a coverage/selects decision), not noise.

## Slate burn-in grammar (so OCR + parsing is reproducible)

The clapper burn-in encodes **scene / slate / extra**. Parse rules learned in production:

- `pt` = part; `+` joins two scenes on one slate; a standalone numeric = the slate number.
- Take is a **roman numeral**; multiple takes of one slate is a real distinction to preserve.
- A **5000-series** number is a real slate.
- **Non-slate tokens** (do not treat as a slate identity): `stock`, camera-roll numbers,
  `placeholder`, `SLUTSCEN` (end-of-scene marker), and other free-text. When the burn-in is one of
  these, the shot has no slate identity and must fall back to visual matching.
- The **bottom-left** burn-in is the **VFX editorial note** (what work the shot needs). Its presence
  is a strong prior that the shot is a VFX shot, but it is **not exhaustive**: invisible-VFX shots
  often carry no note (see Protected shots).

## What counts as a VFX shot (identity for one cut)

We only ever deal with **VFX shots**. A detected cut row is a VFX shot **iff** one of:
(1) it has an **OCR'd burn-in editorial note**, (2) the operator **flagged it as a match** (a proposed
master code), or (3) the operator **flagged it as new**. No note and no flag → **not a shot**; ignore it.

The operator **annotates existing rows** (match/new marks + notes); do not expect them to add rows.

**Invisible-VFX audit**: the shots that are VFX *only* by operator judgment (cam shake, muzzle flash,
morph/track/swish transitions, set extension, stabilize, timewarp, DMP, screen replace) carry **no
burn-in note**. Find them by **diffing the OCR-note export (ground truth of what OCR actually read) against
the sheet's operator marks**: a row that is flagged but whose tcid has no OCR note is a manual/invisible-VFX
ID. Do **not** judge "has an OCR note" from the sheet's note cell: that cell can hold operator-typed or
tool-written text; only the OCR export distinguishes OCR from manual.

---

## Matching a new cut to the master: the 1:1 rule

A master shot code is **unique** (one editorial shot). Cross-cut matching is therefore a **global
1:1 assignment**: at most one new-cut shot may claim any master code, and a code is used at most
once. Do **not** do per-shot argmax; it produces collisions (two shots claiming one code).

Assign in priority tiers, highest first:

1. **Exact code**: the new shot's own official code equals a master code. Prefer this above all
   else. (`SHW_0879_01_080` should match `SHW_0879_01_080`, not a neighbour.)
2. **Ordinal within a (slate, take) group**: within one slate, pair the pieces in **counter
   order**. This absorbs counter drift between cuts. Never reorder a slate group by picture.
3. **Slate / take-relaxed**: same slate, relaxed on take when takes are ambiguous.
4. **Visual (CLIP)**: only for **slate-less** shots. A slate match always beats a visual match;
   visual never overrides a slate.

Leftovers on the new-cut side after assignment are **NEW** shots; leftovers on the master side are
**candidate omits** (but see Protected shots before omitting anything).

**Before trusting a NEW:** cross-check the shot's own code against the master code column. If it is an
exact, unclaimed master code, the matcher missed it (often a protected shot reappearing): match it.
Otherwise classify by the master: slate present with free counters = counter-drift; slate present but
counters all claimed = extra piece; slate absent = genuinely new.

**Hidden merges** hide behind postviz/placeholder slates the boundary oracle can't read. Find them by a
**start-slate vs end-slate change** (frame divergence alone over-flags swish-pans and VFX plates). Notes
`SWISH PAN` / `TRACK WIPE` / `MORPH CUT` / `HIDDEN SPLIT` mark intentional transition VFX: keep as one shot.

**Uniqueness guardrail:** put a conditional-formatting rule on the proposed-code column so any
duplicate code turns red. The column should be fully unique before you trust the match.

**Visual matching** uses CLIP image-to-image cosine similarity (ViT-B/32). Calibrate the threshold
on known-good matches for your footage; do not hardcode a number across shows. Slate-less shots
only.

---

## Protected shots: never auto-omit these

A large fraction of a mature master breakdown is **hand-added invisible-VFX** shots: DMP
backgrounds, screen replacements, clean-ups, online transitions/shakes, muzzle flashes, look
treatments, CG additions. These have **no on-set slate**, so they never match a new cut by slate or
by picture, and a naive cut-update will keep proposing to drop them every single cut.

**Rule:** a master shot flagged as manually added (e.g. `NEW Cut<N>` / `manual` / `added` in the
revision column) is **protected** and must **never be auto-omitted**. Only clean editorial shots
with no such flag are legitimate omit candidates. Maintain the protected list and exclude it from
every omit proposal. If an earlier cut wrongly omitted a protected shot, restoring it is part of the
next gated update.

## Persist per-shot decisions across cuts

Every unique operator decision (a slate override, a boundary merge/split call, a placeholder id, a
VFX/partial scope, a confirmed match, a "keep, do not omit") must be **recorded in the sheet, keyed
on stable identity** (slate + take, not a counter or timecode) and **inherited into the next cut**.
The point: each new cut should only surface *deltas* for review, not re-litigate settled shots.
Without this, the same protected shots get re-flagged for omit every cut.

---

## Google Sheets: gotchas that will bite you

- **The `=IMAGE()` `#REF` trap.** Reading a sheet via the API (or `curl`) reports a **working**
  `=IMAGE()` cell as `#REF` or a 302. This is **browser render/activation lag, not a broken or
  deprecated image, and there is no reliable hard cap you can detect this way.** Verify thumbnails
  **in the browser**, never delete or "fix" a thumbnail on the strength of an API `#REF`, and expect
  large galleries to fill in lazily as you scroll. Before sharing a copy, paste-special-as-values
  into a **copy** (never the source) so the images are frozen.
- **Resolve columns by header name, never by fixed letter.** Operators reorder columns. And never
  write a literal value into a column that is driven by an array formula (it shatters the spill);
  use a sidecar manual column and coalesce.
- **Insert rows by copying a sibling row over the new row** first (inheriting its formulas and
  formatting), then edit the specifics. A blank insert leaves every per-row formula missing.
- **Copy through Drive to preserve formatting.** A Drive file-copy keeps formatting, validation, and
  conditional formatting; rebuilding from raw values via the API loses all of it. Grow the grid
  (append dimensions) before writing past the current row count.
- **Protection:** to make a zone editable for a collaborator, protect only the formula/structure
  cells and leave the editable zone **unprotected + highlighted**. Carve-out "unprotected ranges"
  inside a protected range are unreliable.
- **Never touch cost/producer columns on a cut update.** Match on the shot code, update identity and
  media columns in place, append new shots, and leave the budget framework alone.

## Note reconcile: append, never overwrite

When editorial notes change between cuts, reconcile **non-destructively**: append `old ## new`,
mark removals as `## Blank`, tag the status, and **hold** on OCR noise rather than trusting a shaky
read. Producer annotations in the notes must survive. Do not silently overwrite a note.

---

## Working principles (apply to everything)

- **Dry-run by default.** Any tool that mutates a sheet, the master, or the cut list defaults to a
  preview that writes nothing and reports what it *would* do. Apply only on an explicit second step.
- **Verify after writing.** After any batch write, re-read the affected cells/rows and confirm each
  matches the intended target before reporting done. Watch for silent side effects: column-shift,
  double-applied protections, duplicate uploads, shattered array formulas.
- **The master breakdown is precious.** Never pollute it with false-new duplicates and never falsely
  omit a shot that is present in the cut. Structural changes to it are gated and, if others depend on
  it, coordinated with them first.
- **Confidentiality stays local.** If you enrich footage (auto shot descriptions, etc.), run it on a
  **local** model. Do not send frames of unreleased footage to a cloud service, and do not inject
  real names into generated descriptions.

---

## The scripts (what to reach for)

Mechanical pipeline (shipped in the app): `transnet_detect.py` (detect) · `bs_worker.py`
(frames/thumbs/cuts/refclips/qc) · `contact_sheet.py` · `bs_gsheets.py` (Google connect / template
copy / sheet build) · `make_blank_template.py` (strip a master into a shareable template).

Judgment-support modules (shipped, each a CLI + library, all dry-run by default):
- `bs_ocr.py`: slate OCR (the grammar above, OCR-noise tolerant), VFX-note OCR (3-frame consistency
  gate), show-TC offset probe, slate-oracle boundary QC. Subcommands `slate | notes | tcoffset | boundaryqc`.
- `bs_repair.py`: `split | merge | rethumb | apply-ledger` (split-into-N at decreasing frames; the
  persistent missed-cut ledger applied bottom-to-top; burn-in TCs accepted via the show-TC offset).
- `bs_match.py`: `compare | assign | audit | fpscheck`: the tiered global 1:1 matcher (exact code →
  ordinal → slate/take → visual-for-slate-less, CLIP optional with hash fallback), uniqueness check that
  fails loudly, the invisible-VFX audit, cut-diff, and FPS-mismatch guard.
These do the *mechanics* of the judgment work; the decisions stay with the operator + you. Regression-
tested against four cuts of a real feature (9k+ verified slate reads; the matcher reproduces the
production outcome to ~95% with the remainder being operator picture-judgment, by design; code
identity held stable across the full four-cut chain at 90.8% with every drift traced to a real
editorial cause).

## Producer deliverables you can drive

- **Duration-negotiation view** (skill `/budget-deltas`): three adjacent master columns = bid-era
  duration, current duration, delta; row-2 header array formulas keyed on the master code; red
  delta = shot got shorter (negotiate down), green = longer (re-price). A view only - never wire
  costs to it.
- **Vendor reference clips** (skill `/vendor-refs`): one clip per bid-scope VFX shot named by the
  master code, filtered by the master Status column (VFX ships; Online/Omitted/Practical do not),
  a separately watermarked folder per vendor (PIL text overlay, not drawtext - it segfaults on
  many builds). Clips, never the whole film, and delivery is the operator's call.

## Your sheet IDs (fill in)

- Master breakdown: `<master-sheet-id>` (tab `Shots_Breakdown`)
- Current cut breakdown: `<cut-sheet-id>`
- Blank template: `<template-id>`
