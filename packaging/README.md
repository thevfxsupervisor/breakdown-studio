# Packaging Breakdown Studio

This turns the app into a folder you can hand to someone with **no Python installed at all**:
`BreakdownStudio.exe` plus a `_internal/` folder of bundled libraries. Double-click the exe,
the GUI opens.

## What a release zip contains

```
BreakdownStudio/
  BreakdownStudio.exe      <- run this (GUI, or CLI subcommands, see below)
  _internal/                <- bundled Python runtime + libraries (do not touch)
```

That's it. No separate Python, no `pip install`, no venv setup, for everything **except**
shot detection and OCR (see "What's NOT bundled" below).

## What's bundled

- The app itself: `bs_launcher.py` (entry point), both GUIs (`breakdown_studio_qt.py`,
  PySide6, and `breakdown_studio.py`, Tkinter, as a fallback if PySide6 ever fails to load),
  `bs_worker.py`, `bs_ocr.py`, `bs_gsheets.py`, `bs_repair.py`, `bs_match.py`,
  `contact_sheet.py`, `make_blank_template.py`.
- Runtime libraries: PySide6 (Qt), Pillow, numpy, google-api-python-client plus google-auth
  and google-auth-oauthlib (for the Google Sheets integration), tkinter (ships with the base
  CPython install the build uses, so keeping it as a fallback costs almost nothing).

Running `BreakdownStudio.exe` with no arguments launches the Qt GUI, same as
`python breakdown_studio_qt.py` from source. Every pipeline stage that doesn't need
detection or OCR (frames, thumbnails, QC, cut clips, contact sheet, reference clips,
Google Sheets build) works exactly as it does from source, because the frozen exe re-dispatches
to itself for those stages instead of shelling out to a system Python (see "How this works"
below). Verified end to end during this build: `worker qc` produced a real `qc_flags.csv`
from inside the frozen exe with zero errors.

The exe also works as a CLI multiplexer, useful for scripting or troubleshooting without
opening the GUI:

```
BreakdownStudio.exe worker frames --movie M --output-base B
BreakdownStudio.exe ocr slate --frames-dir F --scenes-csv C
BreakdownStudio.exe gsheets whoami
BreakdownStudio.exe repair split --shot SHW_00091413 --at-frame 14
BreakdownStudio.exe match compare --a cutA_dir --b cutB_dir
BreakdownStudio.exe contact --movie-stem NAME --output-base B
BreakdownStudio.exe tk              # Tkinter GUI explicitly
BreakdownStudio.exe worker --help   # help works normally, no console flash on double-click use
```

## What's NOT bundled (and why)

Two dependencies are deliberately left out of the frozen build:

- **TransNetV2 shot detection** (`torch` + `transnetv2-pytorch`). Torch's CUDA wheels are
  several hundred MB to multiple GB; bundling them would balloon the download for every user,
  including the majority who will run detection once per cut and don't need it baked into
  every launch. Detection has always run through a separate interpreter anyway (see
  `install-transnet.bat` / `requirements-transnet.txt` in the app root); that doesn't change.
  Point Settings, "TransNetV2 Python", at that separate venv, same as the source install.
- **EasyOCR** (slate / VFX-note / boundary-QC stages). Same reasoning: it pulls in torch too.
  `bs_ocr.py` already degrades gracefully when it's missing: instead of crashing, it prints
  `bs_ocr requires the 'easyocr' package for burn-in OCR. Install it in the worker
  environment with: pip install easyocr` and exits with a clear error. In the GUI this shows
  up as a stage failure with that message in the log; every other checked stage still runs.

Neither of these is a bug in the frozen build: they are optional extras layered on top of a
Python environment, same as the source install always required. See the root `README.md` and
`install-transnet.bat` for how to add them.

### Adding detection / OCR later

1. Install a separate Python (any 3.9+ works) the normal way, or point at one you already have.
2. Create a venv for it and install `requirements-transnet.txt` (detection) and/or `easyocr`
   (OCR): see the root `install-transnet.bat` / `install-worker.bat` for the exact commands.
3. Open `BreakdownStudio.exe`, go to Settings, and set "TransNetV2 Python" to that venv's
   `python.exe`. The Detect stage will use it. The three OCR stages (`bs_ocr`) run inside the
   frozen exe already and will start working automatically once `pip install easyocr` has been
   run against *some* interpreter the frozen exe can reach. Practically speaking this means
   using the source install's worker Python for the OCR stages rather than the frozen exe,
   since the frozen exe's bundled interpreter isn't pip-installable. Simplest path: keep
   running from source (`python breakdown_studio_qt.py`) if OCR is a hard requirement, and
   reserve the frozen build for detection-optional / OCR-optional workflows (frames/thumbs/
   cuts/contact/refclips/Sheets, all of which work fully frozen).

## How this works (for maintainers)

The GUIs used to shell out with `[worker_python, "bs_worker.py", "frames", ...]`. A frozen
app has no `python` on the target machine to be `worker_python`, so `bs_launch_helpers.py`
adds one branch, used everywhere a stage command is built:

```python
if getattr(sys, "frozen", False):
    [sys.executable, "<subcommand>", *args]   # BreakdownStudio.exe re-invokes itself
else:
    [worker_python or sys.executable, "<script>.py", *args]   # unchanged source behaviour
```

`bs_launcher.py` is the entry point PyInstaller freezes (see `breakdown_studio.spec`). With no
arguments it launches the Qt GUI (falling back to Tkinter if PySide6 fails to import); with a
subcommand (`worker`, `ocr`, `gsheets`, `repair`, `match`, `contact`, `template`, `tk`) it
imports the matching module and calls its existing `main()`, forwarding argv. None of the
worker modules themselves changed: they still parse `sys.argv` the same way they always did.

## Building it yourself

Requires a Python 3.9+ interpreter to bootstrap the build venv (does not need to be the same
one the frozen app will use at runtime; PyInstaller bundles its own copy of what it finds in
the build venv). If your only Python lacks `ensurepip` (a known issue with some vendor-bundled
interpreters, including the Shotgun-bundled Python on some studio machines), `build.bat`
handles it automatically: it creates the venv with `--without-pip` and bootstraps pip via
`get-pip.py`, no `ensurepip` required.

```
packaging\build.bat
```

Output: `dist\BreakdownStudio\BreakdownStudio.exe` (one-folder build). The script stages the
actual PyInstaller work in a local temp directory even if the app folder itself lives on a
network drive (Dropbox, NAS, etc.), then copies the result back: PyInstaller's file scanning
is slow and occasionally unreliable directly over SMB.

Override the base interpreter with `set BS_BUILD_PYTHON=C:\path\to\python.exe` before running
`build.bat` if `python` isn't the one you want on PATH.

### What a build produced here looked like

One-folder build, `console=False` (no terminal flash when double-clicked):

- `BreakdownStudio.exe`: about 5.8 MB
- Full `dist\BreakdownStudio\` folder: about 276 MB (1,745 files). This is mostly PySide6's
  Qt binaries; Pillow, numpy, and the google-api client libraries are comparatively small.
- Cold start to a responsive window: about 1 second.
- `BreakdownStudio.exe worker --help` / `BreakdownStudio.exe match --help`: print full help
  text and exit 0, no attempt to spawn a system Python.
- `BreakdownStudio.exe worker qc --movie ... --output-base ...` against a synthetic
  `Scenes.csv`: ran the real QC stage inside the frozen exe (CSV parse, perceptual-hash
  comparison skipped gracefully when thumbs are absent, `PROGRESS`/`DONE` lines printed) and
  wrote a correct `qc_flags.csv`, the same code path the GUI drives, confirmed working with
  zero Python installed on the target machine.
- `BreakdownStudio.exe ocr slate ...` against frames with no thumbnails and no `easyocr`
  installed in the build: failed with the intended friendly message (`bs_ocr requires the
  'easyocr' package ... pip install easyocr`), exit code 1, rather than a traceback. This is
  the documented graceful-degradation behaviour, not a bug.

## .gitignore

`packaging/build_env/` (the build venv), `build/` and `dist/` (PyInstaller's working and
output directories) are all gitignored: see `packaging/.gitignore` and the root
`.gitignore`. Nobody should ever commit a build artifact; every release is built fresh from
source and attached to a GitHub release instead.
