# Breakdown Studio - Quickstart (10 minutes)

Get from zero to a breakdown sheet in 10 minutes on your own footage.

## Step 1: Install (3 minutes)

**Windows:**
1. Download or clone the `breakdown_studio` folder.
2. Double-click `install.bat` and follow the prompts. It creates one virtual environment, `bs_env`, in this folder, then asks one question: **"Install AI features (shot detection + burn-in OCR)? ~2 GB download, recommended. [Y/n]"**. Say **Y** (the default) so Detect and OCR work in this quickstart; say N only if you want a smaller, faster install and don't need those stages yet.
3. When it finishes, it has already written `config.json` with the interpreter and ffmpeg paths it found. There is nothing to copy by hand.

**macOS / Linux:**
1. Download or clone the `breakdown_studio` folder.
2. Open a terminal and run:
   ```bash
   cd breakdown_studio
   bash install.sh
   ```
3. Same one question, same recommendation (Y). `config.json` is written for you.

## Step 2: Configure the app (2 minutes)

1. **Windows:** Double-click `Breakdown Studio.lnk` (or `Breakdown Studio.bat`).
   **macOS:** Double-click `breakdown_studio.command`.
   **Linux:** Run `python3 breakdown_studio.py`.

2. Click **Settings…** and check what's already filled in:
   - **Worker Python**, **TransNetV2 Python** (if you answered Y in Step 1), **ffmpeg**, and **ffprobe** should all be pre-filled by the installer. You should not need to type paths here; just glance to confirm they point at real files. If something looks off, click **Run doctor** for a PASS/WARN/FAIL breakdown of exactly what's wrong.
   - Set **FPS** to your video frame rate (e.g., 24.0) for frame-accurate reads.
   - Set **Show TC offset** if your burned-in timecode is offset from the file start (e.g., `00:59:50:00`). Leave as `00:00:00:00` if in sync.

3. For Google sheet integration (optional for now):
   - Leave **Google OAuth client secret** blank for now. See Step 4 below when you're ready.

4. Click **Save** and close Settings.

## Step 3: Run your first breakdown (5 minutes)

1. **Pick a movie file:**
   - Click **Movie file** and select an MP4, ProRes, or other ffmpeg-compatible video.
   - (Use a short clip first, not a whole feature, to test. A 30-second clip is enough.)

2. **Pick an output folder:**
   - Click **Output folder** and select or create a folder where the results will go.
   - Results will be organized in subfolders: `scenes_transnet/` (detection), `frames/`, `thumbs/`, `cuts/`, etc.

3. **Run:**
   - The defaults should work: Detect, Frames, Thumbnails, QC, Cut clips, Contact sheet, and Sheet build are checked. (Uncheck Sheet build for now if you skipped Google setup. If you answered N to the AI-features question in Step 1, Detect will auto-skip itself with a message in the log rather than fail.)
   - The optional OCR stages (Slate OCR, VFX-note OCR, Boundary QC) are **off by default**, even though EasyOCR is already installed if you answered Y in Step 1. Leave them unticked for this first pass; see "Try OCR" below once you're ready.
   - Click **Run all**.
   - Watch the log. Each stage will print progress.

   This will:
   - Detect shots in the movie.
   - Extract 3 frames (start, middle, end) per shot.
   - Generate 480x270 thumbnails.
   - Flag any suspect shots (over-splits, missed cuts).
   - Extract each shot as a standalone MP4.
   - Assemble all thumbnails into an 8K contact sheet.

4. **Check the output:**
   - Click **Open output folder** and look at:
     - `scenes_transnet/<movie>-Scenes.csv`: the shot list
     - `thumbs/`: all the shot thumbnails (flip through to spot-check)
     - `_<movie>_contact_sheet.jpg`: entire film at a glance
     - `cuts/`: individual shot clips
     - `qc_flags.csv`: any flagged shots to review

That's it. You now have a shot breakdown, frame-accurate and ready for further work.

5. **Try OCR (optional, a few extra minutes):**
   - OCR reads the burned-in slate and VFX notes, but it needs crop boxes that match your footage first. Verify them before trusting any OCR output:
     ```bash
     bs_env\Scripts\python bs_ocr.py probecrops --movie <your movie file> --frame 1
     ```
     (macOS/Linux: `bs_env/bin/python3 bs_ocr.py probecrops --movie <your movie file> --frame 1`)
   - Open the `_crop_check.jpg` it writes. It shows the slate/note/show-TC boxes drawn on a real frame. If a box doesn't hug its burn-in, adjust **OCR crops** in Settings and re-run `probecrops` until it does.
   - Back in the app, tick **Slate OCR**, **VFX-note OCR**, and **Boundary QC**, and set **OCR upscale** (1-4x; start with 2) to improve reads on small burn-ins.
   - Click **Run selected** to run just the OCR stages against the frames you already extracted.

## Step 4 (optional): Build a Google sheet

If you want to upload this breakdown to a live Google sheet:

1. **One-time setup (per Google account, about 10 minutes):**
   - Follow `SETUP_GOOGLE.md` start to finish. It walks through the Google Cloud Console steps click by click and ends with a downloaded OAuth client JSON. Do this once; you won't need to repeat it for future films on the same account.

2. **In the app:**
   - Click **Settings…** and set **Google OAuth client secret JSON** to that file.
   - Click **Save**.

3. **Back in the main window:**
   - Tick **Build / update Google breakdown sheet**.
   - Under the Google section, pick **New film (from template)**, type a title, and click **Create sheet**.
   - The app will copy a blank template into your Drive and select it.
   - Click **Run all** (or just the **Build / update Google breakdown sheet** stage).
   - The app will write shots into your sheet with live thumbnail links.

4. **In the browser:**
   - Open the Google sheet (the app will print the link, or find it in Google Drive).
   - You'll see your shots, shot codes, and thumbnails.
   - The thumbnails may take a moment to appear (lazy loading). Scroll around to render them.

## What's next

Now that you have a breakdown:

- **QC the shot list**: Look at the contact sheet and the QC flags. Are any shots merged when they should be split, or vice versa? Edit `scenes_transnet/<movie>-Scenes.csv` if needed (it is a plain CSV) and re-run the Frames and later stages. They'll skip work that exists and pick up the new scene list.

- **Match to a master** (if this is a new cut of an existing show): Use the `ai_kit/` skills to match this cut's shots to your master breakdown. See `ai_kit/README.md`.

- **Reconcile notes** (if notes changed from the previous cut): The reconcile tool appends new notes without destroying producer annotations.

- **Gate the master update** (only after you're confident in the match): The `/update-master` skill writes the master in a gated, verifiable way.

- **Share with a vendor**: Run the Reference clips stage to burn shot codes into clips, then send a vendor a playlist.

For the judgment work (matching, QC, reconciling), see `ai_kit/CLAUDE.md` and the skills in `ai_kit/skills/` if you're working with an AI assistant.

## Troubleshooting this quickstart

**Run the doctor first.**

```bash
bs_env\Scripts\python bs_launcher.py doctor
```

(macOS/Linux: `bs_env/bin/python3 bs_launcher.py doctor`), or click **Run doctor** in Settings. It prints a PASS/WARN/FAIL table for the interpreter, `bs_env`, the AI extras, ffmpeg, config.json, and your Google credential, which covers most of what's below faster than reading it.

**`Breakdown Studio` does not launch:**
- Make sure Python 3.9+ is installed: open a terminal and run `python --version`.
- Check that Tkinter is included: `python -c "import tkinter; print('ok')"`. If it fails on Linux, install `python3-tk` via your package manager.

**The Detect stage was skipped, or `Run all` fails on it:**
- If you answered N to the AI-features question during install, this is expected: re-run `install.bat` / `install.sh` and answer Y, or use the advanced `install-transnet.bat`/`.sh` for a GPU/CUDA-specific install (see README).
- If you answered Y but it still fails, check **TransNetV2 Python** in Settings; it should already be pre-filled by the installer. Run doctor to confirm the AI extras actually installed.
- Or uncheck **Detect shots** and provide a scene list manually: create a `scenes/` folder in the output directory, put a CSV in it (format: see the README), and re-run.

**`Run all` fails on frames or later stages:**
- Check ffmpeg: open a terminal and run `ffmpeg -version`. If not found, set the full path in Settings, or re-run `install.bat` on Windows and let it offer to download a portable copy.

**Thumbnails don't appear in the Google sheet:**
- In the browser, open the sheet and scroll down slowly. `=IMAGE()` cells load lazily.
- If they still don't appear after scrolling, check the app log for upload errors.

**Google sheet fails with "auth error":**
- Make sure you have a valid **Google OAuth client secret JSON** set in Settings. If you haven't done the one-time setup yet, see `SETUP_GOOGLE.md`.
- Click **Connect Google…** in the app and sign in to grant consent.

---

That's the quickstart. For more details, see `README.md`.
