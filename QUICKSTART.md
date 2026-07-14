# Breakdown Studio - Quickstart (10 minutes)

Get from zero to a breakdown sheet in 10 minutes on your own footage.

## Step 1: Install (3 minutes)

**Windows:**
1. Download or clone the `breakdown_studio` folder.
2. Double-click `install.bat` and follow the prompts. (The installer will ask which environments to create; say yes to `worker_env`, optionally yes to `transnet_env` if you want shot detection in the app.)
3. When it finishes, you'll see paths printed. Copy the **Worker Python** path.

**macOS / Linux:**
1. Download or clone the `breakdown_studio` folder.
2. Open a terminal and run:
   ```bash
   cd breakdown_studio
   bash install.sh
   ```
3. Copy the **Worker Python** path from the output.

## Step 2: Configure the app (2 minutes)

1. **Windows:** Double-click `Breakdown Studio.lnk` (or `Breakdown Studio.bat`).
   **macOS:** Double-click `breakdown_studio.command`.
   **Linux:** Run `python3 breakdown_studio.py`.

2. Click **Settings…** and fill in:
   - **Worker Python**: paste the path from the installer.
   - **TransNetV2 Python**: (optional; only if you installed it and want to run shot detection in the app).
   - **ffmpeg** and **ffprobe**: usually auto-detected. Leave as-is if they are filled in.

3. For OCR stages (optional, off by default):
   - If you want to run Slate OCR, VFX-note OCR, or Boundary QC, first install EasyOCR: `pip install easyocr` (into the worker environment).
   - Set **FPS** to your video frame rate (e.g., 24.0) for frame-accurate reads.
   - Set **Show TC offset** if your burned-in timecode is offset from the file start (e.g., `00:59:50:00`). Leave as `00:00:00:00` if in sync.
   - Set **OCR crops** (top-left/bottom-left burn-in box pixel coordinates). These are show-specific and must be verified against a real frame from your footage. See `config.example.json` for the format.
   - Set **OCR upscale** (1-4x) to improve OCR accuracy on small or low-res burn-ins. Start with 2.

4. For Google sheet integration (optional for now):
   - Leave **Google OAuth client secret** blank for now. You can add it later if you want to build Google sheets.

5. Click **Save** and close Settings.

## Step 3: Run your first breakdown (5 minutes)

1. **Pick a movie file:**
   - Click **Movie file** and select an MP4, ProRes, or other ffmpeg-compatible video.
   - (Use a short clip first, not a whole feature, to test. A 30-second clip is enough.)

2. **Pick an output folder:**
   - Click **Output folder** and select or create a folder where the results will go.
   - Results will be organized in subfolders: `scenes_transnet/` (detection), `frames/`, `thumbs/`, `cuts/`, etc.

3. **Run:**
   - The defaults should work: Detect, Frames, Thumbnails, QC, Cut clips, Contact sheet, and Sheet build are checked. (Uncheck Sheet build for now if you skipped Google setup.)
   - The optional OCR stages (Slate OCR, VFX-note OCR, Boundary QC) are **off by default**. Tick them if you have EasyOCR installed and you want to extract shot codes and VFX flags from burn-in.
   - Click **Run all**.
   - Watch the log. Each stage will print progress.

   This will:
   - Detect shots in the movie.
   - Extract 3 frames (start, middle, end) per shot.
   - Generate 480x270 thumbnails.
   - Optionally OCR slate and VFX notes (if enabled).
   - Flag any suspect shots (over-splits, missed cuts, or OCR-detected boundary issues).
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

## Step 4 (optional): Build a Google sheet

If you want to upload this breakdown to a live Google sheet:

1. **One-time setup (per Google account):**
   - Go to [Google Cloud Console](https://console.cloud.google.com/).
   - Create a new project (or use an existing one).
   - Enable the **Google Sheets API** and **Google Drive API**.
   - Create an **OAuth client** of type **Desktop app** and download the JSON.

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

**`Breakdown Studio` does not launch:**
- Make sure Python 3.9+ is installed: open a terminal and run `python --version`.
- Check that Tkinter is included: `python -c "import tkinter; print('ok')"`. If it fails on Linux, install `python3-tk` via your package manager.

**`Run all` fails on the Detect stage:**
- TransNetV2 Python may not be configured. Either:
  1. Set **TransNetV2 Python** in Settings (from the installer output), or
  2. Uncheck **Detect shots** and provide a scene list manually: create `scenes/` folder in the output directory, put a CSV in it (format: see the README), and re-run.

**`Run all` fails on frames or later stages:**
- Check ffmpeg: open a terminal and run `ffmpeg -version`. If not found, set the full path in Settings.

**Thumbnails don't appear in the Google sheet:**
- In the browser, open the sheet and scroll down slowly. `=IMAGE()` cells load lazily.
- If they still don't appear after scrolling, check the app log for upload errors.

**Google sheet fails with "auth error":**
- Make sure you have a valid **Google OAuth client secret JSON** set in Settings.
- Click **Connect Google…** in the app and sign in to grant consent.

---

That's the quickstart. For more details, see `README.md`.
