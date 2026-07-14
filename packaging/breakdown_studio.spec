# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Breakdown Studio.

One-folder build (COLLECT, not a single exe): faster startup, and the antivirus/SmartScreen
false-positive rate on a folder-of-files + a small launcher exe is much lower than on a
self-extracting single-file exe.

What's bundled (frozen, no separate Python install needed by the end user):
  - bs_launcher.py as the entry point (breakdown-studio / worker / ocr / gsheets / repair /
    match / contact / template / tk subcommands - see bs_launcher.py's docstring)
  - Qt GUI (breakdown_studio_qt.py, PySide6) and Tkinter GUI (breakdown_studio.py) as a fallback
  - bs_worker.py, bs_ocr.py (EasyOCR is optional, see below), bs_gsheets.py, bs_repair.py,
    bs_match.py, contact_sheet.py, make_blank_template.py, bs_launch_helpers.py
  - Pillow, numpy, PySide6, google-api-python-client + google-auth + google-auth-oauthlib
  - tkinter (ships with the base CPython install; near-zero marginal size, kept as the
    no-PySide6 fallback path)

Deliberately NOT bundled (documented in packaging/README.md, not a bug):
  - torch / transnetv2-pytorch (TransNetV2 shot detection): multi-hundred-MB CUDA wheels: a
    frozen build is the wrong place for these. Detection must be run via a separate
    "transnet_env" venv the user (or install-transnet.bat) sets up, pointed at from Settings ->
    "TransNetV2 Python". The GUI already guards this at run time (see
    breakdown_studio_qt.py: "Set the TransNetV2 Python in Settings").
  - easyocr (+ its torch dependency): same reasoning. bs_ocr.py already degrades gracefully:
    it raises a friendly "bs_ocr requires the 'easyocr' package ... pip install easyocr"
    ImportError instead of crashing when the package is absent, so the OCR stage rows in the
    GUI show that message in the log and every other stage keeps working.

Build:  packaging\build.bat   (from the packaging/ directory, using packaging/build_env)
Output: dist/BreakdownStudio/BreakdownStudio.exe
"""
import sys
from pathlib import Path

block_cipher = None

# packaging/breakdown_studio.spec -> app root is one directory up
APP_ROOT = Path(SPECPATH).resolve().parent

ENTRY_SCRIPT = str(APP_ROOT / "bs_launcher.py")

# Modules bs_launcher dispatches to by importlib.import_module() at runtime: PyInstaller's
# static import scan won't see those, so they must be listed explicitly or they'd be missing
# from the frozen build and the "worker"/"ocr"/... subcommands would fail with ImportError.
HIDDEN_IMPORTS = [
    "bs_worker",
    "bs_ocr",
    "bs_gsheets",
    "bs_repair",
    "bs_match",
    "bs_doctor",
    "bs_fetch",
    "bs_enrich",
    "bs_miro",
    "clip_similarity",
    "contact_sheet",
    "make_blank_template",
    "breakdown_studio",
    "breakdown_studio_qt",
    "bs_launch_helpers",
    # google client libraries pull in plugin modules PyInstaller's analyzer sometimes misses
    "google.auth.transport.requests",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery",
    "googleapiclient.http",
]

a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[str(APP_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # explicitly kept out - see docstring above
        "torch",
        "torchvision",
        "torchaudio",
        "easyocr",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BreakdownStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BreakdownStudio",
)
