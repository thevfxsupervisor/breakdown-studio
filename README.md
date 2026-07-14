# Breakdown Studio

A portable, open-source GUI for building a VFX shot breakdown from an evolving film cut. It automates the mechanical work (detect shots, extract frames and thumbnails, OCR burned-in slate and VFX notes, assemble reference clips, build a live Google breakdown sheet) so you can focus on the judgment work that actually requires expertise: matching cuts, QCing boundaries, reconciling notes, and gating master updates.

Built and battle-tested on a real feature-film production. MIT licensed.

```
Detect (TransNetV2) → Frames → Thumbnails → OCR → Cut clips → Contact sheet → Reference clips → Sheet build
```

## The problem

You are a VFX supervisor or producer. A new cut arrived. You need a complete shot breakdown, including all VFX shots with their notes, thumbnails, and a whole-film budget estimate. You have no facility pipeline department behind you. The old way took weeks: manual shot detection, frame grabs, spreadsheet hand-entry, repeated passes to catch mistakes, relinking to a master breakdown when cut-to-cut data drifts. The tool does the repeatable half in hours, so you can spend your time on the judgment half: is this shot VFX, or just editorial? Does it match a master shot, or is it new? Are the boundaries right?

## What Breakdown Studio does

```
┌─────────────────┐
│ Movie file      │
│ (ProRes, MP4)   │
└────────┬────────┘
         │
    ┌────▼────────────────────────────────┐
    │ Detect shots (TransNetV2)           │ → Scenes.csv
    │ Extract frames (start/mid/end)      │ → frames/*.jpg
    │ Make thumbnails (480x270)           │ → thumbs/*.jpg
    │ OCR slate burn-in (top-left)        │ → official codes
    │ OCR VFX notes (bottom-left)         │ → is_vfx flag
    │ QC: flag suspect shots              │ → qc_flags.csv
    │ Cut per-shot MP4 clips              │ → cuts/*.mp4
    │ Burn-in reference clips (ProRes 422)│ → refclips/*.mov
    │ Build 8K contact sheet              │ → contact_sheet.jpg
    │ Build / update Google breakdown     │ → live sheet + Drive
    └────────┬────────────────────────────┘
             │
        ┌────▼──────────────────┐
        │ You + your AI        │
        │ assistant do this:   │
        ├──────────────────────┤
        │ Match to master      │
        │ QC boundaries        │
        │ Reconcile notes      │
        │ Gate master update   │
        │ Decide keep/omit     │
        └──────────────────────┘
```

Each shot is keyed by its timecode ID (`tcid`: `HHMMSSFF` of the start) so all artifacts interoperate: a frame grab, a thumbnail, the corresponding row in the sheet, and a reference clip all share the same name and can be tracked together.

## Validated on a real production

Breakdown Studio has been tested against a real feature-film production: 9,188 operator-verified slate reads achieved 98.0% agreement with OCR; the 1:1 matching algorithm reproduces the operator-approved outcome at approximately 95% accuracy on the achievable subset, with zero uniqueness collisions (residual variation being operator picture-judgment, by design). 232 unit tests pass across 4 test suites. The tool ships battle-tested.

## What you do (judgment work)

The tool automates detection and assembly. You (and an AI assistant, if you pair with one) make the judgment calls:

- **Matching**: When a new cut arrives, does a shot match a master code, or is it new? The tool stages proposals using slate identity, visual similarity, and ordinal position; you accept or correct them.
- **Boundary QC**: Did the detector over-split or miss a cut? The slate oracle (burned-in slate read at each frame transition) tells you when slates change, so you know where real cuts are. You can merge shots or split them.
- **Note reconcile**: VFX notes changed; did you lose important producer annotations? You reconcile non-destructively: append new notes, preserve old ones.
- **Protected shots**: The master breakdown often includes hand-added invisible-VFX (DMP backgrounds, screen replacements, online transitions, muzzle flashes). These have no on-set slate, so they never auto-match a new cut. You protect them so they never get auto-omitted.
- **Master gate**: Any change to the master breakdown is staged first and requires explicit approval before it writes.

If you work with an AI assistant, the `ai_kit/` folder includes a brief, skills, and memory seeds so the assistant can help with all of this safely and reproducibly.

## Install

### Before you install (prerequisites)

Check these before running the installer. The installer validates most of this for you and will tell you if something is missing, but it is faster to have them ready first.

- [ ] **Python 3.9+ from [python.org](https://www.python.org/downloads/)** (tick "Add python.exe to PATH" during setup on Windows). This includes Tkinter, which the desktop GUI needs.
      **Windows note:** the `python` command that ships with Windows by default is often a Microsoft Store stub, not a real interpreter. It looks like Python on the command line but fails (or opens the Store) when a script tries to use it. The installer detects this and will tell you, but installing from python.org first avoids the problem entirely.
- [ ] **ffmpeg and ffprobe**: nice to have on PATH already, but not required before you start. On Windows, the installer can download a portable copy for you if it does not find one. On macOS/Linux, it will print the exact `brew install ffmpeg` / `apt install ffmpeg` line if it is missing.
- [ ] **Disk space**: plan for roughly 50% of the movie file size (frames, thumbnails, cut clips can be large), plus about 2 GB if you install the AI features below.

Everything else (Pillow, numpy, the Google API libraries, and the optional AI features) is installed by the installer into one virtual environment, so you do not need to install those yourself.

### Windows

1. Download or clone this folder.
2. Double-click `install.bat`.
   - It first checks that a real Python 3.9+ interpreter is available (it prefers the `py -3` launcher, falls back to `python`, and detects the Microsoft Store stub). If it cannot find one, it stops and tells you to install Python from python.org.
   - It creates one virtual environment, `bs_env`, in this folder, and installs the core dependencies (Pillow, numpy, Google libraries) into it.
   - It asks one question: **"Install AI features (shot detection + burn-in OCR)? ~2 GB download, recommended. [Y/n]"**. Answering yes (the default) installs torch, TransNetV2, and EasyOCR into the same `bs_env`, so Detect and the OCR stages work out of the box. Answering no skips that download; the app will simply skip those stages later with a clear message instead of failing.
   - If ffmpeg is not found on PATH, it offers to download a portable copy into `tools/ffmpeg/` for you.
   - It writes `config.json` itself with the interpreter and ffmpeg paths it just set up, so Settings is pre-filled when you first open the app.
   - It creates `Breakdown Studio.lnk` (the recommended launcher).
3. Double-click `Breakdown Studio.lnk` to run the app. Settings should already show the right paths; just verify them.

If `install.bat` does not work (Windows Defender or network sandbox), run it in a PowerShell terminal:
```powershell
cd <breakdown_studio_folder>
.\install.bat
```

### macOS / Linux

1. Download or clone this folder.
2. Run the installer:
   ```bash
   bash install.sh
   ```
   It creates the same `bs_env`, asks the same one AI-features question, and writes `config.json` with the paths it set up.
3. Launch the app:
   ```bash
   python3 breakdown_studio.py
   ```
   Or on macOS, double-click `breakdown_studio.command`.

### Advanced: separate detection env (GPU/CUDA)

Most people should skip this: the default installer puts everything (including TransNetV2 and EasyOCR) into `bs_env`, and that is enough for GPU acceleration on most setups. Use a separate env only if you need a specific CUDA build of torch that the default install does not give you.

- Windows: run `install-transnet.bat`. It creates `transnet_env` with your chosen torch wheel.
- macOS/Linux: run `install-transnet.sh` (same idea).
- In Settings, set **TransNetV2 Python** to that environment's interpreter. Everything else keeps using `bs_env`.

### First run: Settings

1. **Open Settings…** from the app.
2. **Verify the paths.** The installer already filled in Worker Python, TransNetV2 Python (if you installed the AI features), ffmpeg, and ffprobe. You should not need to type anything here; just confirm they point at real files. If a path looks wrong or empty, click **Run doctor** (see Troubleshooting) to see exactly what is missing.
3. **For the breakdown sheet (optional):** if you want to build Google Sheets, see `SETUP_GOOGLE.md` for the one-time Google Cloud Console setup (about 10 minutes), then set **Google OAuth client secret JSON** in Settings to the downloaded file. Leave **Google token cache** blank (it defaults to `.gtoken.json` beside the app).

All settings save to `config.json` next to the app, so each machine keeps its own paths.

## Use

### Basic workflow

1. **Pick a movie file and an output folder.** The output folder will hold all results (scenes CSV, frames, thumbnails, cut clips, etc.) organized in subfolders.
2. **Tick the stages you want.** Defaults cover a full first pass: detect, frames, thumbnails, QC, cuts, contact sheet, and sheet build. Reference clips are off by default (they take longer).
3. **Click Run all** (or **Run selected** for a subset).
4. **Watch the log.** Each stage prints progress as it goes. Click **Stop** to cancel the running stage.
5. **Open the output folder** to grab results. Save the app log for your records.

### Stages explained

| Stage | What it does | Output | Engine |
|---|---|---|---|
| **Detect** | Find shot boundaries in the movie using TransNetV2 neural net. Adjustable threshold (0.25 is the default sweet spot; lower finds more cuts). If you skipped the AI features during install, this stage is auto-skipped with a clear log message instead of failing. | `scenes_transnet/<movie>-Scenes.csv` | torch / TransNetV2 |
| **Frames** | Extract the first, middle, and last frame of each shot at full resolution. Accurate to the frame (the 3-frame extraction is used to OCR VFX notes and detect boundaries). | `frames/SHW_<tcid>-{start,mid,end}.jpg` | ffmpeg |
| **Thumbnails** | Downscale the frames to 480x270 for the contact sheet and Google sheet. Happens fast. | `thumbs/SHW_<tcid>-{start,mid,end}.jpg` | Pillow |
| **Slate OCR** (optional) | Read the burned-in slate (clapper number, top-left) to derive official shot codes. Tolerant to OCR noise and multi-take slates. Installed automatically if you answered Y to the AI-features question; otherwise auto-skipped. Verify your crop boxes with `probecrops` (see below) before trusting the output. | `_<movie>_ocr_slate.csv` | EasyOCR |
| **VFX-note OCR** (optional) | Read burned-in VFX notes (bottom-left), flag which shots are VFX, and reconcile across the 3 frames (start/mid/end) to spot note bleed or incomplete reads. Same install/skip behavior as Slate OCR. Verify crops with `probecrops` first. | `_<movie>_ocr_notes.csv` | EasyOCR |
| **Boundary QC** (optional) | Use the slate oracle (frame-by-frame OCR) to find likely detector over-splits (same slate across a potential cut boundary) and missed cuts (slate changes mid-shot). Suggests splits/merges for review. | `qc_boundary_suggestions.csv` | EasyOCR + logic |
| **QC** | Read the frames and thumbnails, flag shots that look suspicious: duration outliers (often a missed cut or an over-split), perceptual-hash jumps across start/mid/end (the shot spans a real cut), ultra-short shots. Writes `qc_flags.csv` for you to review. | `qc_flags.csv` | Pillow + numpy |
| **Cut clips** | Extract each shot as a standalone MP4 (or other format). Useful for sending individual clips to a vendor or for pre-viz review. Hardware encoding (NVIDIA NVENC) is auto-detected and speeds up encoding. | `cuts/SHW_<tcid>.mp4` | ffmpeg |
| **Contact sheet** | Assemble all thumbnails into one large grid (8K, 7680x4320). Letterbox-cropped (dark frames don't shrink the crop). Useful for a quick visual scan of the entire film. | `_<movie>_contact_sheet.jpg` | Pillow |
| **Reference clips** | Burn official shot codes and timecode into each clip (ProRes 422 HQ format for archival quality). Concatenate them into a timeline MP4 so you can scrub through the entire film shot-by-shot with codes visible. | `refclips/SHW_<tcid>.mov` + `_timeline.mov` | ffmpeg + Pillow |
| **Build sheet** | Write shots into your own Google breakdown sheet (or create a new one from a blank template). Resolves columns by header, so it survives operator reordering. Thumbnails are linked via `=IMAGE()` formulas, so they load lazily in the browser. Your own account signs in; the app never uses a shared bot. | Live Google Sheet | Sheets API |

### Verify OCR crop boxes with `probecrops`

The OCR stages read three pixel boxes from `config.json` (`ocr_crops.slate`, `.note`, `.showtc`), and they are show-specific: you must confirm they line up with your footage before you trust any OCR output. Instead of typing pixel coordinates blind and hoping, render them onto a real frame:

```bash
bs_env\Scripts\python bs_ocr.py probecrops --movie path\to\your_movie.mov --frame 1
```

(macOS/Linux: `bs_env/bin/python3 bs_ocr.py probecrops --movie path/to/your_movie.mov --frame 1`)

This writes `_crop_check.jpg` next to your output, with the slate/note/show-TC boxes drawn on top of the actual frame. Open it and adjust `ocr_crops` in Settings until each box hugs its burn-in, then re-run `probecrops` to confirm.

### Important options

- **Prefix**: the shot-code prefix (default `SHW`, but you can change it to your show's scheme).
- **Encoder**: `libx264` (software, portable) or `nvenc` (NVIDIA GPU, much faster). Auto-detection: if NVENC is not available, the app falls back to `libx264`.
- **Workers**: number of parallel jobs (default is half your CPU count, usually safe). More workers = faster if your disk is not the bottleneck.
- **Detect threshold**: TransNetV2 confidence threshold (0.12 to 0.25 is the review band; 0.25 is the sweet spot for most footage). Lower threshold finds more cuts; higher is more conservative.

### Google sheet modes

The app has two ways to use Google Sheets:

**New film (from template)**
- Type a title and click **Create sheet**.
- The app copies a blank template (structure, formatting, budget framework, no shot data) into your Drive.
- You get a live sheet with all the machinery pre-wired; you just need to fill in your costs and crew.

**Existing project**
- Paste the Sheet ID or URL of your current breakdown.
- Tick **Build / update Google breakdown sheet** and run.
- The app matches each shot to the master by its `Temp Cut Shot Code` column (the file-relative code at the time the cut arrived). Existing shots update in place; new shots append.
- **Cost and producer columns are never touched** (so you keep your budget estimates and notes intact).

**Preview before writing (recommended):**
- Tick **Preview only: show adds/updates, write nothing**.
- Run it against your live sheet. It will report how many shots would update in place vs. append, with examples, and write nothing.
- Untick Preview and run again to apply.

**Make a fresh template:**
- In Settings, set **Master spreadsheet ID** to your master breakdown.
- Click **Make blank template from master…**. The app copies the master (read-only), strips all shot and cost data, keeps the structure and formatting, and saves a new template ID.
- Now a colleague can start from a blank that reflects the very latest master layout.

## Shot identity: slate vs. timecode

This is the most important concept in the tool. Read it carefully.

- **`tcid`** (timecode ID) is `HHMMSSFF` of the shot's start timecode. It is stable *within one cut*, but changes when the edit changes. It is NOT a cross-cut identity.
- **Slate** is the burned-in on-set clapper number (e.g., `023` in the top-left of the frame). It is a photographic property of the take, stable across cuts, re-grades, temp VFX, and subtitles. It IS the cross-cut identity.
- **Official shot code** is `SHW_<slate:04>_<counter:03>` (zero-padded), derived from the slate and the order in the edit (e.g., `SHW_0023_001` is the first shot cut from slate 23). A multi-take slate (e.g., `SHW_0023_II`) is meaningful: it signals a coverage or selects decision.

**When matching a new cut to a master:** never match by timecode. Timecode changes every cut. Match by slate, by counter position, or by visual similarity.

## Operator gotchas (things that will trip you up)

### The `=IMAGE()` `#REF` trap

When the Sheets API reads a sheet, it reports a **working** `=IMAGE()` cell as `#REF` or a 302 error. This is not a broken image and not a hard cap. It is browser render lag. In the browser, the image displays fine. The API is just wrong.

**What to do:**
- Always verify thumbnails in the browser, never in the API response.
- Never delete or "fix" a thumbnail because an API read says `#REF`.
- For large galleries (hundreds of shots), expect lazy loading: scroll the sheet to force the browser to render all images.
- Before sharing a copy of the sheet with a collaborator, paste-special-as-values into a new tab so the images are frozen and not dependent on API state.

### Columns move; resolve by header, never by letter

You or the operator often reorder columns in the sheet (moving VFX notes next to the code, moving QC flags together, etc.). If a script reads columns by fixed letter (`A:Z`), reordering breaks it.

**What to do:**
- Always read a wide range (e.g., `A:AE`, not just `A:Z`).
- Always resolve columns by header name (`get("VFX Notes", "")` not `column_F`).
- Never write a literal value into a cell in a column that is driven by an array formula; it shatters the formula's spill. Use a sidecar static column and coalesce.

### Copy a sibling row when inserting

When you add a new row to the sheet, copy an adjacent row over it first (so it inherits all the formulas and conditional formatting), then edit the shot-specific data. A blank insert leaves every per-row formula missing.

### Protection: unprotect by zone, not by carve-out

To let the operator edit certain cells, protect only the formula/structure cells and leave the editable zones unprotected and highlighted (e.g., a green background). The `unprotectedRanges` carve-out inside a protected range is unreliable.

### Dry-run always; verify after writing

Any tool that mutates the sheet, the master, or the cut list should default to a preview mode that writes nothing. Run it first to see what it *would* do, then run it again (with Preview unticked) to apply.

After any batch write, re-read the affected cells and confirm they match the intended target. Watch for silent side effects: column shifts, double-applied protections, shattered array formulas.

### Persist decisions across cuts

Every unique operator decision (a slate override, a boundary merge/split, a placeholder ID, a confirmed match, a "keep, do not omit") should be recorded in the sheet, keyed on stable identity (slate + take, not a timecode), and inherited into the next cut. This way, each new cut only surfaces deltas for review, not re-litigating the same shots every time.

### VFX shots: has-a-note vs. is-flagged vs. is-VFX

Three things are NOT the same:
- A row **has an OCR burn-in note** (ground truth of what the VFX OCR tool actually read).
- A row **is flagged by the operator** (e.g., marked as a match or flagged as new).
- A row **is a VFX shot** (one of the above, or operator judgment: muzzle flash, cam shake, morphcut, etc. with no note).

Only a row with an OCR note OR an operator flag is a VFX shot. Everything else is editorial (not in scope). The "invisible-VFX audit" diff the OCR export (ground truth) against the sheet's operator marks to find the hand-added shots.

## The AI suite (ai_kit/): the differentiator

Breakdown Studio was not just built to be AI-friendly; it was **operated by an operator + AI
assistant pair on a real feature**, and the kit is that working setup, generalized. This is the
part you will not find in other breakdown tools.

- **`ai_kit/CLAUDE.md`**: the project brief that loads into every assistant session: the pipeline,
  the slate grammar, matching tiers, protected shots, all the Sheets gotchas, and the producer
  deliverables. Your assistant starts each session already knowing the traps.
- **`ai_kit/skills/`** (7): the judgment workflows as invokable skills with guardrails baked in.
  The `/intake-cut` runbook drives a whole "new cut arrived" cycle end to end and stops hard at
  the operator gates; `/qc-boundaries`, `/match-cut`, `/reconcile-notes`, and `/update-master`
  (the only skill allowed to write to a master, behind an approval gate) do the judgment stages;
  `/budget-deltas` and `/vendor-refs` produce the producer deliverables: the renegotiation view
  and the per-vendor watermarked reference clips.
- **`ai_kit/memory_seed/`** (8): durable-memory examples of the "record a decision once, inherit
  it every future cut" pattern: protected shots, the IMAGE #REF trap, 1:1 uniqueness, the
  boundary-corrections ledger, the note typo filter, chosen-frame thumbnails, and more.
- **`ai_kit/PATTERNS.md`**: multi-assistant production patterns: when the show grows to a
  breakdown seat plus a bidding seat, this is the shared-channel, ownership, and
  ping-before-structural-change protocol that kept two assistants safe in one master.

Everything is generic (neutral `SHW_` codes, no client data) and optional: the app runs fine as a
standalone tool. But the honest pitch is that the judgment half of a breakdown never fully
automates, and a briefed assistant with the right guardrails is the most practical way one person
runs a whole film.

## Troubleshooting

**Run the doctor.**
- Before anything else, run:
  ```bash
  bs_env\Scripts\python bs_launcher.py doctor
  ```
  (macOS/Linux: `bs_env/bin/python3 bs_launcher.py doctor`). Or click **Run doctor** in Settings.
- It prints a PASS/WARN/FAIL table covering: the Python interpreter and version, `bs_env` and its dependencies, the AI extras (torch/TransNetV2/EasyOCR), ffmpeg/ffprobe, `config.json`, and your Google credential file. Most of the issues below show up here first, with a specific reason instead of a downstream crash.

**`install.bat` says Python was not found.**
- On Windows, the `python` command is often a Microsoft Store stub, not a real interpreter: it looks fine on the command line but fails silently (or opens the Store) when a script runs it. `install.bat` checks for this and will tell you plainly if it finds the stub.
- Fix: install Python 3.9+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then close the terminal, open a new one, and re-run `install.bat`.
- If you want to keep the Store shortcut from intercepting `python`/`python3` in the future, you can turn it off in Settings -> Apps -> Advanced app settings -> App execution aliases.

**`Breakdown Studio.bat` opens a console that flashes and closes.**
- The launcher is finding the wrong Python interpreter. Check Settings, Worker Python: it should be a real `python.exe` from your `bs_env` folder, not the Windows Store stub. Run doctor to confirm.
- Run **`Breakdown Studio (debug).bat`** to see the actual error.

**`Breakdown Studio.lnk` does nothing (on Dropbox-synced machines).**
- Windows may be blocking it as a "mark-of-the-web". Right-click → Properties → tick **Unblock**.
- If the `.lnk` is missing, recreate it: right-click `_make_shortcut.ps1` → **Run with PowerShell** (or re-run `install.bat`).

**Some thumbnails look missing far down a large sheet.**
- Google renders `=IMAGE()` cells lazily in the browser. Scroll to force rendering. This is not a hard cap.
- Never delete a thumbnail because an API read reports it as `#REF`.
- For very large cuts, use `--thumb-mode none` (or untick upload in Settings) to skip thumbnail upload entirely.

**Detect (or an OCR stage) is skipped with a message instead of running.**
- This is expected if you answered "n" to the AI-features question during install, or ticked Detect without a TransNetV2 Python set. The app auto-skips rather than failing, and the log line tells you why.
- Fix: re-run `install.bat` / `install.sh` and answer "y" to the AI-features question, or set up the advanced separate env (see "Advanced: separate detection env" above) and point **TransNetV2 Python** at it in Settings.

**`Build sheet` fails with a Google authentication error.**
- Make sure you have **Google OAuth client secret JSON** set in Settings. If you have not done the one-time Google Cloud Console setup yet, see `SETUP_GOOGLE.md`.
- Click **Connect Google…** to sign in and grant consent. A token is cached so you won't sign in every time.
- For headless / automated use, set the `BS_GOOGLE_SA` environment variable to a service-account JSON and share your sheets with that account's email.

**Cuts fail with an encoder error.**
- The app auto-detects NVIDIA NVENC against the real movie file. If NVENC is unavailable, it falls back to `libx264`.
- Set **Encoder** to `libx264` if you don't have an NVIDIA GPU.
- Check that ffmpeg is properly installed and can encode: `ffmpeg -encoders | grep h264`.

**Re-running after a re-detect or re-thumb gives stale images.**
- Fixed in the current version: thumbnail uploads are md5-aware and replace bytes in place, so re-running updates the image (same Drive link).

## Notes & scope

- **Re-runnable**: every stage skips work that already exists, so you can re-run safely or resume.
- **Detection edit**: if you want to QC or edit the shot list, edit/replace `scenes/<movie>-Scenes.csv` and later stages will use it (it is preferred over `scenes_transnet/`).
- **Local-only mode**: detection, frames, thumbnails, cut clips, contact sheet, and reference clips all run offline. No cloud account needed. Google sheet build is optional and uses your own account.
- **Qt GUI (preview)**: `breakdown_studio_qt.py` is a PySide6 port with async stages and progress/ETA. Same `config.json`. Run with `python breakdown_studio_qt.py` (requires `pip install PySide6`). The Tkinter app is the default.
- **Tests**: `python tests/smoke_test.py` runs the pure-logic smoke suite (frame math, timecode ID, parsing).
- **ShotGrid integration**: not wired in (it needs per-org credentials). Add it later if needed.

## Building from source / contributing

See `CONTRIBUTING.md`.

## Architecture

| Module | Role |
|---|---|
| `breakdown_studio.py` | Tkinter GUI orchestrator: 11 selectable stages (detection, frames, thumbnails, OCR, QC, cuts, contact sheet, reference clips, sheet build), subprocess-per-stage, log streaming, progress bar. |
| `bs_worker.py` | Local pipeline: frames, thumbnails, cut clips, reference clips, QC. No cloud dependencies. |
| `bs_ocr.py` | Slate OCR (top-left burn-in, shot identity), VFX-note OCR (bottom-left, is-vfx flag), show-TC read (for offset verification). Optional, requires EasyOCR (`pip install easyocr`). |
| `bs_repair.py` | Boundary QC and shot-list editing: split-into-N, merge adjacent shots, rethumb individual shots, apply a ledger of corrections. Optional, input-driven. |
| `bs_match.py` | 1:1 cross-cut shot matcher: assigns each new-cut shot to a master shot by exact code, slate + ordinal, visual similarity (CLIP), or marks as new/omit. Global uniqueness enforcement (no two shots claim one code). Optional, runs in-app or standalone. |
| `transnet_detect.py` | TransNetV2 shot detection. Runs under whichever Python is set as TransNetV2 Python: `bs_env` by default (installed alongside the worker deps), or a separate `transnet_env` if you used the advanced GPU/CUDA install. |
| `contact_sheet.py` | Assembles thumbnails into an 8K grid with auto-letterboxing. |
| `bs_gsheets.py` | Google Sheets connection: OAuth login, template copy, sheet build, row matching. |
| `make_blank_template.py` | Strips a master breakdown into a shareable template (structure, formatting, no shot/cost data). |
| `bs_launcher.py` | Single entry point (source or packaged build): dispatches to the GUI or any CLI stage, plus the `doctor` health check (`bs_launcher.py doctor`). |
| `tests/smoke_test.py`, `test_ocr.py`, `test_repair.py`, `test_match.py` | Unit tests: 232 total (16 + 90 + 72 + 54). Pure-logic and integration tests for frame math, timecode, CSV parsing, OCR grammar, boundary repair, and matching. |

## Files and folder structure

```
breakdown_studio/
  breakdown_studio.py              ← main app (Tkinter), 11-stage orchestrator
  breakdown_studio_qt.py           ← alt app (PySide6, optional)
  bs_worker.py                     ← local pipeline (frames/thumbs/cuts/refclips/qc)
  bs_ocr.py                        ← slate + VFX-note OCR (optional, EasyOCR)
  bs_repair.py                     ← boundary QC + split/merge/rethumb (optional)
  bs_match.py                      ← 1:1 cross-cut matching (optional)
  transnet_detect.py               ← TransNetV2 shot detection
  contact_sheet.py                 ← 8K contact sheet
  bs_gsheets.py                    ← Google Sheets connection
  make_blank_template.py           ← template creation
  bs_launcher.py                   ← single entry point (GUI + CLI stages + doctor)
  install.bat / install.sh         ← one-shot installers (create bs_env, write config.json)
  install-transnet.bat/.sh         ← advanced: separate transnet_env (GPU/CUDA)
  config.json                      ← per-machine settings (git-ignored, pre-filled by installer)
  config.example.json              ← template for config.json
  tests/
    smoke_test.py                  ← pure-logic tests (frame math, timecode, CSV parsing)
    test_ocr.py                    ← OCR grammar and consistency tests (82 tests)
    test_repair.py                 ← boundary repair and CSV rewriting tests (72 tests)
    test_match.py                  ← 1:1 matching algorithm and audit tests (54 tests)
  ai_kit/
    CLAUDE.md                      ← project brief for AI assistant
    README.md                       ← how to use the AI kit
    skills/
      match-cut.md                 ← skill: match new cut to master
      qc-boundaries.md             ← skill: boundary QC via slate oracle
      reconcile-notes.md           ← skill: append-only note merge
      update-master.md             ← skill: gated master write
    memory_seed/
      protected-shots.md           ← example memory
      image-ref-trap.md            ← example memory
      match-1to1-unique.md         ← example memory
      persist-decisions.md         ← example memory
  README.md                        ← this file
  QUICKSTART.md                    ← 10-minute install-to-sheet walkthrough
  SETUP_GOOGLE.md                  ← one-time Google OAuth setup
  UPGRADE_PLAN.md                  ← roadmap and technical notes
  CONTRIBUTING.md                  ← contribution guidelines
  DISTRIBUTION_CHECKLIST.md        ← pre-publish verification
  LICENSE                          ← MIT
```

## License

MIT. See `LICENSE` for terms.

## Credits

Built on production experience. If you use this tool or contribute improvements, we'd love to hear about it.
