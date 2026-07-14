#!/usr/bin/env python
"""Breakdown Studio (Qt) - PySide6 port of breakdown_studio.py.

Same GUI, same pipeline, same config.json schema as the Tkinter original: this is a
straight port to PySide6 so the app can pick up Qt's native look-and-feel and async
QProcess plumbing instead of the stdlib Tkinter + threading/queue combo. The two apps
are interchangeable: they read and write the exact same config.json next to this file,
so switching between them (or running either on a machine that only has one toolkit)
just works.

  Detect (TransNetV2) -> Frames -> Thumbnails -> QC -> Cut clips -> Contact sheet
      -> Reference clips -> Build/update Google breakdown sheet

Each stage runs as a QProcess (never blocking the UI thread). Stdout is parsed for the
same tagged lines the worker scripts emit for the Tkinter app:
  - "PROGRESS <stage> <done>/<total>"   -> drives the progress bar + ETA
  - lines containing "CONNECTED <email>"        -> Google account label
  - lines containing "NEW_SPREADSHEET_ID <id>"  -> new sheet created from template
  - lines starting with "TEMPLATE_ID <id>"      -> blank template derived from master

Run:  python breakdown_studio_qt.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bs_launch_helpers import (
    worker_argv, adopt_bs_env, resolve_detect_interpreter, probe_can_import, doctor_argv,
)

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    print(
        "Breakdown Studio (Qt) needs PySide6, which isn't installed in this Python.\n"
        "Install it with:\n\n"
        "    python -m pip install PySide6\n\n"
        "Then run this script again:  python breakdown_studio_qt.py\n"
        "(The Tkinter version, breakdown_studio.py, needs no extra install and works "
        "as a fallback.)",
        file=sys.stderr,
    )
    sys.exit(1)

APP_DIR = Path(__file__).resolve().parent
CONFIG = APP_DIR / "config.json"

# ---- stage definitions: id, label, needs movie, default on -------------------
STAGES = [
    ("detect", "Detect shots (TransNetV2)", True, True),
    ("frames", "Extract frames (start/mid/end)", True, True),
    ("thumbs", "Make thumbnails", False, True),
    ("slate_ocr", "Slate OCR (needs EasyOCR)", False, False),
    ("note_ocr", "VFX-note OCR (needs EasyOCR)", False, False),
    ("boundary_qc", "Boundary QC (needs EasyOCR)", False, False),
    ("transcribe", "Transcribe dialogue (needs faster-whisper)", True, False),
    ("describe", "Describe shots (needs local Ollama)", False, False),
    ("qc", "QC: flag suspect shots", True, True),
    ("cuts", "Extract cut clips", True, True),
    ("contact", "Contact sheet (8K)", False, True),
    ("refclips", "Reference clips (burn-in)", False, False),
    ("buildsheet", "Build / update Google breakdown sheet", True, False),
    ("match", "Match new cut to master (1:1 staging)", False, False),
    ("miro", "Push shot-wall to Miro board", False, False),
]
STAGE_LABELS = {sid: label for sid, label, *_ in STAGES}
STAGE_LABELS["fetch"] = "Fetch video from URL"  # synthetic stage, not a STAGES checkbox row
# stage ids that shell out to bs_ocr.py (slate/notes/boundaryqc subcommands)
OCR_STAGE_SUBCMD = {"slate_ocr": "slate", "note_ocr": "notes", "boundary_qc": "boundaryqc"}
# stage ids that shell out to bs_enrich.py (transcribe/describe subcommands; contract CLI,
# module built concurrently -- see _cmd_for's ENRICH branch for the friendly-missing message)
ENRICH_STAGE_SUBCMD = {"transcribe": "transcribe", "describe": "describe"}

DEFAULTS = {
    "transnet_python": "",   # interpreter with torch + transnetv2_pytorch
    "worker_python": sys.executable,   # interpreter with Pillow + numpy
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "scripts_dir": str(APP_DIR),       # where bs_worker / contact_sheet / transnet_detect live
    "output_base": "",
    "prefix": "SHW",
    "encoder": "libx264",
    "workers": str(max(2, (os.cpu_count() or 4) // 2)),
    "threshold": "0.25",
    "canvas": "7680x4320",
    "last_movie": "",
    "google_client_secret": "",
    "google_token": "",
    "master_id": "",
    "template_id": "",
    "spreadsheet_id": "",
    "sheet_tab": "Shots_Breakdown",
    "sheet_first_row": "3",
    "sheet_mode": "existing",
    "new_title": "New Film - Shot Breakdown & Estimate",
    "drive_folder_id": "",
    "show_tc_offset": "0",
    "fps": "0",
    "ocr_upscale": "1",
    "ocr_crops": {
        "slate": [0, 0, 660, 140],
        "note": [0, 940, 960, 1080],
        "showtc": [1560, 0, 1920, 140],
    },
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def autodetect(cfg):
    """Fill blank ffmpeg/ffprobe paths with anything found on PATH (best effort, no
    hardcoded studio paths: keep this file generic/portable)."""
    if not cfg.get("ffmpeg") or cfg["ffmpeg"] == "ffmpeg":
        cfg["ffmpeg"] = shutil.which("ffmpeg") or cfg["ffmpeg"]
    if not cfg.get("ffprobe") or cfg["ffprobe"] == "ffprobe":
        cfg["ffprobe"] = shutil.which("ffprobe") or cfg["ffprobe"]
    return cfg


def preflight_warnings(cfg):
    """Cheap, non-blocking startup check: is ffmpeg reachable, does worker_python exist?
    Returns a short list of human-readable warning strings (empty if all good)."""
    warnings = []
    ffmpeg = (cfg.get("ffmpeg") or "").strip()
    ffmpeg_ok = bool(ffmpeg) and (shutil.which(ffmpeg) is not None or Path(ffmpeg).exists())
    if not ffmpeg_ok:
        warnings.append("ffmpeg not found: set it in Settings")
    worker_python = (cfg.get("worker_python") or "").strip()
    worker_ok = bool(worker_python) and Path(worker_python).exists()
    if not worker_ok:
        warnings.append("Worker Python not found: set it in Settings")
    return warnings


def is_url(s):
    """True if the Movie field holds a URL to fetch (http/https) rather than a local file
    path. Kept as a one-line seam so both GUIs and bs_fetch.py agree on the check."""
    return bool(s) and s.strip().lower().startswith(("http://", "https://"))


def find_scenes_csv(output_base, stem):
    """Mirrors bs_worker.find_scenes_csv: prefer a reconciled live scenes/ CSV, else
    scenes_transnet/, else any *Scenes*.csv, so bs_ocr.py gets the same CSV bs_worker used."""
    base = Path(output_base) / stem
    for sub in ("scenes", "scenes_transnet"):
        p = base / sub / f"{stem}-Scenes.csv"
        if p.exists():
            return p
    for sub in ("scenes", "scenes_transnet", "."):
        d = base / sub
        if d.exists():
            cands = sorted(d.glob("*Scenes*.csv"))
            if cands:
                return cands[0]
    return None


def _fmt_eta(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    elif secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    else:
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


# =============================================================================
class SettingsDialog(QtWidgets.QDialog):
    """Every config.json path/value, same rows/order as the Tkinter Settings dialog.

    "file" rows get a file-browse "..." button; "dir" rows get a folder-browse button;
    plain rows are just a text field (numbers, IDs, tab names).
    """

    ROWS = [
        ("transnet_python", "TransNetV2 Python (torch)", "file"),
        ("worker_python", "Worker Python (Pillow+numpy)", "file"),
        ("ffmpeg", "ffmpeg binary", "file"),
        ("ffprobe", "ffprobe binary", "file"),
        ("scripts_dir", "Scripts folder", "dir"),
        ("canvas", "Contact-sheet canvas (WxH)", None),
        ("prefix", "Shot-code prefix", None),
        ("fps", "FPS override (0 = auto-detect)", None),
        ("show_tc_offset", "Show-TC offset (frames)", None),
        ("google_client_secret", "Google OAuth client secret JSON", "file"),
        ("google_token", "Google token cache (auto)", "file"),
        ("drive_folder_id", "Drive folder ID (uploads)", None),
        ("master_id", "Master spreadsheet ID (to template)", None),
        ("template_id", "Blank-template spreadsheet ID", None),
        ("spreadsheet_id", "Spreadsheet ID (active project)", None),
        ("sheet_tab", "Breakdown tab name", None),
        ("sheet_first_row", "First data row", None),
    ]

    CROP_ROWS = [
        ("slate", "Slate crop (top-left)"),
        ("note", "VFX-note crop (bottom-left)"),
        ("showtc", "Show-TC crop (top-right)"),
    ]

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(860, 640)
        outer = QtWidgets.QVBoxLayout(self)

        grid = QtWidgets.QGridLayout()
        self.edits = {}
        for i, (key, label, kind) in enumerate(self.ROWS):
            grid.addWidget(QtWidgets.QLabel(label), i, 0)
            edit = QtWidgets.QLineEdit(str(cfg.get(key, "")))
            self.edits[key] = edit
            grid.addWidget(edit, i, 1)
            if kind == "file":
                btn = QtWidgets.QPushButton("...")
                btn.setFixedWidth(28)
                btn.clicked.connect(lambda _=False, e=edit: self._browse_file(e))
                grid.addWidget(btn, i, 2)
            elif kind == "dir":
                btn = QtWidgets.QPushButton("...")
                btn.setFixedWidth(28)
                btn.clicked.connect(lambda _=False, e=edit: self._browse_dir(e))
                grid.addWidget(btn, i, 2)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        # ---- OCR crops editor: 3 rows x [x0, y0, x1, y1] ----
        crop_box = QtWidgets.QGroupBox("OCR crop boxes (pixels: x0 y0 x1 y1): verify against a real "
                                       "frame before trusting OCR output")
        cgrid = QtWidgets.QGridLayout(crop_box)
        cgrid.addWidget(QtWidgets.QLabel("Region"), 0, 0)
        for c, hdr in enumerate(("x0", "y0", "x1", "y1")):
            cgrid.addWidget(QtWidgets.QLabel(hdr), 0, c + 1)
        crops = cfg.get("ocr_crops", {}) or {}
        self.crop_edits = {}
        for r, (key, label) in enumerate(self.CROP_ROWS, start=1):
            cgrid.addWidget(QtWidgets.QLabel(label), r, 0)
            box = crops.get(key, [0, 0, 0, 0])
            fields = []
            for c in range(4):
                e = QtWidgets.QLineEdit(str(box[c] if c < len(box) else 0))
                e.setFixedWidth(60)
                e.setValidator(QtGui.QIntValidator(0, 100000))
                cgrid.addWidget(e, r, c + 1)
                fields.append(e)
            self.crop_edits[key] = fields
        outer.addWidget(crop_box)

        bottom = QtWidgets.QHBoxLayout()
        doctor_btn = QtWidgets.QPushButton("Run doctor")
        doctor_btn.clicked.connect(self._run_doctor)
        bottom.addWidget(doctor_btn)
        bottom.addStretch()
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save |
                                              QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        outer.addLayout(bottom)

    def _run_doctor(self):
        """Settings 'Run doctor' button: shell '<worker_python> bs_launcher.py doctor' and show
        its PASS/WARN/FAIL table in a small monospace dialog (UX_PLAN.md P3)."""
        cmd = doctor_argv(self.edits["worker_python"].text().strip() or None)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               cwd=str(Path(__file__).resolve().parent))
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            out = f"ERROR running doctor: {e}"
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Doctor")
        dlg.resize(760, 520)
        lay = QtWidgets.QVBoxLayout(dlg)
        txt = QtWidgets.QPlainTextEdit(out)
        txt.setReadOnly(True)
        txt.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        txt.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
        lay.addWidget(txt)
        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        box.rejected.connect(dlg.accept)
        lay.addWidget(box)
        dlg.exec()

    def _browse_file(self, edit):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select file", edit.text())
        if p:
            edit.setText(p)

    def _browse_dir(self, edit):
        p = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder", edit.text())
        if p:
            edit.setText(p)

    def values(self):
        out = {k: e.text().strip() for k, e in self.edits.items()}
        crops = {}
        for key, fields in self.crop_edits.items():
            try:
                crops[key] = [int(f.text().strip() or 0) for f in fields]
            except ValueError:
                crops[key] = [0, 0, 0, 0]
        out["ocr_crops"] = crops
        return out


# =============================================================================
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Breakdown Studio")
        self.resize(1000, 760)
        self.setMinimumSize(860, 620)

        self.cfg = autodetect(load_config())
        # preflight self-heal (UX_PLAN.md P0.3): adopt the installer's bs_env if worker_python
        # is blank/stale, silently -- logged once the log widget exists, below.
        adopted = adopt_bs_env(self.cfg, APP_DIR)
        if adopted:
            save_config(self.cfg)
        self.stage_checks = {}

        # pipeline (multi-stage) run state
        self._pipeline_process = None
        self._pipeline_running = False
        self._pipeline_stages = []
        self._pipeline_stage_index = 0
        self._pipeline_movie = ""
        self._pipeline_base = ""
        self._pipeline_env = None
        self._detect_interpreter = None  # resolved per-run by _run_selected; see _cmd_for
        self._stop_flag = False
        self._stage_buf = ""
        self._prog_start_time = 0.0
        self._prog_last_time = 0.0
        self._prog_last_done = 0
        self._prog_deltas = []

        # quick (single-shot) Google-helper processes; kept alive in a set
        self._quick_procs = set()

        self._build_ui()
        self._show_preflight_warnings()
        self._update_url_hint()
        if adopted:
            self._log(f"Adopted worker environment: {self.cfg['worker_python']}\n")

    # ------------------------------------------------------------------
    # video-URL input
    # ------------------------------------------------------------------
    def _update_url_hint(self, *_):
        self.url_hint_lbl.setVisible(is_url(self.movie_edit.text()))

    # ------------------------------------------------------------------
    # startup preflight
    # ------------------------------------------------------------------
    def _show_preflight_warnings(self):
        warnings = preflight_warnings(self.cfg)
        if warnings:
            self.preflight_lbl.setText("  " + "   |   ".join(warnings) + "  ")
            self.preflight_lbl.setVisible(True)
        else:
            self.preflight_lbl.setVisible(False)

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        # movie + output rows
        paths = QtWidgets.QGridLayout()
        self.movie_edit = QtWidgets.QLineEdit(self.cfg.get("last_movie", ""))
        self.out_edit = QtWidgets.QLineEdit(self.cfg.get("output_base", ""))
        self._path_row(paths, 0, "Movie file", self.movie_edit, self._pick_movie)
        self._path_row(paths, 1, "Output folder", self.out_edit, self._pick_out)
        root.addLayout(paths)

        # URL hint: shown only when the Movie field looks like http(s)://... -- Run will
        # fetch it (yt-dlp / direct download) into output_base/_fetched/ before the rest
        # of the selected stages run.
        self.url_hint_lbl = QtWidgets.QLabel(
            "This looks like a video URL. Click Run and it will be fetched automatically "
            "before the pipeline starts.")
        self.url_hint_lbl.setStyleSheet("color: #069; padding: 0 4px;")
        self.url_hint_lbl.setVisible(False)
        root.addWidget(self.url_hint_lbl)
        self.movie_edit.textChanged.connect(self._update_url_hint)

        # preflight banner (hidden unless something is missing)
        self.preflight_lbl = QtWidgets.QLabel("")
        self.preflight_lbl.setStyleSheet(
            "background-color: #fff3cd; color: #a60; padding: 4px 8px;")
        self.preflight_lbl.setVisible(False)
        root.addWidget(self.preflight_lbl)

        # options row
        opt = QtWidgets.QHBoxLayout()
        self.prefix_edit = QtWidgets.QLineEdit(self.cfg["prefix"])
        self.prefix_edit.setFixedWidth(60)
        self.enc_combo = QtWidgets.QComboBox()
        self.enc_combo.addItems(["libx264", "nvenc"])
        self.enc_combo.setCurrentText(self.cfg["encoder"])
        self.workers_edit = QtWidgets.QLineEdit(self.cfg["workers"])
        self.workers_edit.setFixedWidth(45)
        self.thresh_edit = QtWidgets.QLineEdit(self.cfg["threshold"])
        self.thresh_edit.setFixedWidth(55)
        opt.addWidget(QtWidgets.QLabel("Prefix"))
        opt.addWidget(self.prefix_edit)
        opt.addSpacing(12)
        opt.addWidget(QtWidgets.QLabel("Encoder"))
        opt.addWidget(self.enc_combo)
        opt.addSpacing(12)
        opt.addWidget(QtWidgets.QLabel("Workers"))
        opt.addWidget(self.workers_edit)
        opt.addSpacing(12)
        opt.addWidget(QtWidgets.QLabel("Detect thr."))
        opt.addWidget(self.thresh_edit)
        opt.addStretch()
        settings_btn = QtWidgets.QPushButton("Settings...")
        settings_btn.clicked.connect(self._settings)
        opt.addWidget(settings_btn)
        root.addLayout(opt)

        # stages
        stage_box = QtWidgets.QGroupBox("Pipeline stages")
        sgrid = QtWidgets.QGridLayout(stage_box)
        for i, (sid, label, _nm, on) in enumerate(STAGES):
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(on)
            self.stage_checks[sid] = cb
            sgrid.addWidget(cb, i // 3, i % 3)
        root.addWidget(stage_box)

        # google breakdown sheet
        g_box = QtWidgets.QGroupBox("Google breakdown sheet  (connect your own account)")
        gv = QtWidgets.QVBoxLayout(g_box)

        g1 = QtWidgets.QHBoxLayout()
        self.connect_btn = QtWidgets.QPushButton("Connect Google...")
        self.connect_btn.clicked.connect(self._connect_google)
        g1.addWidget(self.connect_btn)
        self.gacct_lbl = QtWidgets.QLabel("not connected")
        self.gacct_lbl.setStyleSheet("color: #888;")
        g1.addWidget(self.gacct_lbl)
        g1.addSpacing(20)
        self.mode_new_radio = QtWidgets.QRadioButton("New film (from template)")
        self.mode_existing_radio = QtWidgets.QRadioButton("Existing project")
        mode_group = QtWidgets.QButtonGroup(self)
        mode_group.addButton(self.mode_new_radio)
        mode_group.addButton(self.mode_existing_radio)
        if self.cfg.get("sheet_mode", "existing") == "new":
            self.mode_new_radio.setChecked(True)
        else:
            self.mode_existing_radio.setChecked(True)
        self.mode_new_radio.toggled.connect(self._mode_changed)
        g1.addWidget(self.mode_new_radio)
        g1.addWidget(self.mode_existing_radio)
        g1.addStretch()
        gv.addLayout(g1)

        g2 = QtWidgets.QHBoxLayout()
        self.new_title_lbl = QtWidgets.QLabel("New title")
        self.new_title_edit = QtWidgets.QLineEdit(self.cfg.get("new_title", ""))
        self.create_btn = QtWidgets.QPushButton("Create sheet")
        self.create_btn.clicked.connect(self._create_sheet)
        g2.addWidget(self.new_title_lbl)
        g2.addWidget(self.new_title_edit)
        g2.addWidget(self.create_btn)
        g2.addWidget(QtWidgets.QLabel("Sheet ID/URL"))
        self.sid_edit = QtWidgets.QLineEdit(self.cfg.get("spreadsheet_id", ""))
        g2.addWidget(self.sid_edit, 1)
        self.thumbs_sheet_check = QtWidgets.QCheckBox("upload thumbnails")
        self.thumbs_sheet_check.setChecked(True)
        g2.addWidget(self.thumbs_sheet_check)
        gv.addLayout(g2)

        g3 = QtWidgets.QHBoxLayout()
        self.dryrun_check = QtWidgets.QCheckBox("Preview only: show adds/updates, write nothing")
        g3.addWidget(self.dryrun_check)
        g3.addStretch()
        self.tmpl_lbl = QtWidgets.QLabel("template: " + (self.cfg.get("template_id") or "none"))
        self.tmpl_lbl.setStyleSheet("color: #888;")
        g3.addWidget(self.tmpl_lbl)
        self.make_template_btn = QtWidgets.QPushButton("Make blank template from master...")
        self.make_template_btn.clicked.connect(self._make_template)
        g3.addWidget(self.make_template_btn)
        gv.addLayout(g3)

        root.addWidget(g_box)
        self._mode_changed()

        # run buttons
        rb = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("▶  Run selected")
        self.run_btn.clicked.connect(self._run_selected)
        rb.addWidget(self.run_btn)
        self.runall_btn = QtWidgets.QPushButton("Run all")
        self.runall_btn.clicked.connect(self._run_all)
        rb.addWidget(self.runall_btn)
        self.stop_btn = QtWidgets.QPushButton("■ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        rb.addWidget(self.stop_btn)
        rb.addStretch()
        open_btn = QtWidgets.QPushButton("Open output folder")
        open_btn.clicked.connect(self._open_out)
        rb.addWidget(open_btn)
        root.addLayout(rb)

        # progress
        pf = QtWidgets.QHBoxLayout()
        self.stage_lbl = QtWidgets.QLabel("Idle")
        self.stage_lbl.setMinimumWidth(260)
        pf.addWidget(self.stage_lbl)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        pf.addWidget(self.bar, 1)
        self.pct_lbl = QtWidgets.QLabel("")
        self.pct_lbl.setMinimumWidth(70)
        pf.addWidget(self.pct_lbl)
        self.eta_lbl = QtWidgets.QLabel("")
        self.eta_lbl.setMinimumWidth(120)
        self.eta_lbl.setStyleSheet("font-style: italic; color: #666;")
        pf.addWidget(self.eta_lbl)
        root.addLayout(pf)

        # log
        log_box = QtWidgets.QGroupBox("Log")
        lv = QtWidgets.QVBoxLayout(log_box)
        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.log_edit.setStyleSheet(
            "background-color: #111; color: #d8d8d8; font-family: Consolas, monospace; font-size: 9pt;")
        lv.addWidget(self.log_edit)
        root.addWidget(log_box, 1)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch()
        save_log_btn = QtWidgets.QPushButton("Save log...")
        save_log_btn.clicked.connect(self._save_log)
        bottom.addWidget(save_log_btn)
        root.addLayout(bottom)

    def _path_row(self, grid, row, label, edit, cmd):
        grid.addWidget(QtWidgets.QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        btn = QtWidgets.QPushButton("Browse...")
        btn.clicked.connect(cmd)
        grid.addWidget(btn, row, 2)
        grid.setColumnStretch(1, 1)

    # ------------------------------------------------------------------
    # pickers
    # ------------------------------------------------------------------
    def _pick_movie(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select reference movie", "",
            "Video (*.mov *.mp4 *.mkv *.mxf *.avi);;All files (*.*)")
        if p:
            self.movie_edit.setText(p)
            if not self.out_edit.text().strip():
                self.out_edit.setText(str(Path(p).parent / "breakdown_output"))

    def _pick_out(self):
        p = QtWidgets.QFileDialog.getExistingDirectory(self, "Select output folder")
        if p:
            self.out_edit.setText(p)

    def _open_out(self):
        base = self.out_edit.text().strip()
        stem = Path(self.movie_edit.text()).stem if self.movie_edit.text().strip() else ""
        d = Path(base) / stem if stem else Path(base)
        if d.exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(d)))
        else:
            QtWidgets.QMessageBox.information(self, "Breakdown Studio", f"Not created yet:\n{d}")

    # ------------------------------------------------------------------
    # settings dialog
    # ------------------------------------------------------------------
    def _settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.cfg.update(dlg.values())
            self._persist()
            self._log("[settings saved]\n")
            self.tmpl_lbl.setText("template: " + (self.cfg.get("template_id") or "none"))
            self._show_preflight_warnings()

    # ------------------------------------------------------------------
    # config persistence
    # ------------------------------------------------------------------
    def _persist(self):
        self.cfg.update({
            "last_movie": self.movie_edit.text(), "output_base": self.out_edit.text(),
            "prefix": self.prefix_edit.text(), "encoder": self.enc_combo.currentText(),
            "workers": self.workers_edit.text(), "threshold": self.thresh_edit.text(),
            "spreadsheet_id": self.sid_edit.text().strip(),
            "sheet_mode": "new" if self.mode_new_radio.isChecked() else "existing",
            "new_title": self.new_title_edit.text().strip(),
        })
        save_config(self.cfg)

    # ------------------------------------------------------------------
    # google helpers
    # ------------------------------------------------------------------
    def _mode_changed(self, *_):
        new = self.mode_new_radio.isChecked()
        for w in (self.new_title_lbl, self.new_title_edit, self.create_btn):
            w.setVisible(new)

    def _google_env(self):
        env = QtCore.QProcessEnvironment.systemEnvironment()
        if self.cfg.get("google_client_secret"):
            env.insert("BS_GOOGLE_CLIENT_SECRET", self.cfg["google_client_secret"])
        if self.cfg.get("google_token"):
            env.insert("BS_GOOGLE_TOKEN", self.cfg["google_token"])
        env.insert("BS_FFPROBE", self.cfg["ffprobe"])
        return env

    @staticmethod
    def _sid(s):
        m = re.search(r"/d/([A-Za-z0-9_-]{20,})", s or "")
        return m.group(1) if m else (s or "").strip()

    def _gs_cmd(self, *extra):
        wp = self.cfg["worker_python"] or sys.executable
        args = list(extra)
        if self.cfg.get("google_client_secret"):
            args += ["--client-secret", self.cfg["google_client_secret"]]
        if self.cfg.get("google_token"):
            args += ["--token", self.cfg["google_token"]]
        return worker_argv(self._scripts_dir() / "bs_gsheets.py", args, wp)

    def _connect_google(self):
        self._log("\n[connecting to Google - a browser may open to sign in]\n")

        def on_line(ln):
            if "CONNECTED" in ln:
                self.gacct_lbl.setText(ln.split("CONNECTED", 1)[1].strip())
                self.gacct_lbl.setStyleSheet("color: #2a7;")

        self._run_quick(self._gs_cmd("whoami"), self._google_env(), on_line=on_line,
                         button=self.connect_btn)

    def _create_sheet(self):
        tid = self.cfg.get("template_id", "").strip()
        if not tid:
            QtWidgets.QMessageBox.critical(
                self, "Breakdown Studio",
                "Set a template_id in config.json first "
                "(run make_blank_template.py to create one).")
            return
        title = self.new_title_edit.text().strip() or "New Film - Shot Breakdown"
        self._log(f"\n[creating '{title}' from template]\n")

        def on_line(ln):
            if "NEW_SPREADSHEET_ID" in ln:
                sid = ln.split()[1]
                self.sid_edit.setText(sid)
                self.mode_existing_radio.setChecked(True)
                self._mode_changed()
                self._persist()
                self._log(f"[sheet ready: {sid}]\n")

        self._run_quick(self._gs_cmd("copy-template", "--template-id", tid, "--title", title),
                         self._google_env(), on_line=on_line, button=self.create_btn)

    def _make_template(self):
        master = self.cfg.get("master_id", "").strip()
        if not master:
            QtWidgets.QMessageBox.critical(
                self, "Breakdown Studio",
                "Set 'Master spreadsheet ID' in Settings first: the master to copy "
                "and strip into a fresh blank template.")
            return
        resp = QtWidgets.QMessageBox.question(
            self, "Make blank template",
            "Copy the master (read-only) and strip the copy into a NEW blank "
            "template?\n\nThe master is never modified.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        self._log("\n[deriving blank template from master - master is copied, never modified]\n")
        args = ["--master-id", master]
        if self.cfg.get("google_client_secret"):
            args += ["--client-secret", self.cfg["google_client_secret"]]
        if self.cfg.get("google_token"):
            args += ["--token", self.cfg["google_token"]]
        cmd = worker_argv(self._scripts_dir() / "make_blank_template.py", args,
                           self.cfg["worker_python"] or sys.executable)

        def on_line(ln):
            if ln.startswith("TEMPLATE_ID "):
                tid = ln.split()[1]
                self.cfg["template_id"] = tid
                save_config(self.cfg)
                self.tmpl_lbl.setText(f"template: {tid}")
                self.tmpl_lbl.setStyleSheet("color: #2a7;")
                self.mode_new_radio.setChecked(True)
                self._mode_changed()
                self._log(f"[blank template ready: {tid} - saved as template_id]\n")

        self._run_quick(cmd, self._google_env(), on_line=on_line, button=self.make_template_btn)

    def _run_quick(self, cmd, env, on_line=None, button=None):
        """Run a short one-shot subprocess via QProcess; stream to the log, call
        on_line(text) per complete line (no trailing newline), matching the tagged-line
        protocol the worker scripts use for the Tkinter app."""
        self._log(f"\n$ {' '.join(self._q(c) for c in cmd)}\n")
        proc = QtCore.QProcess(self)
        proc.setProgram(str(cmd[0]))
        proc.setArguments([str(c) for c in cmd[1:]])
        proc.setWorkingDirectory(str(self._scripts_dir()))
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        state = {"buf": ""}

        def flush_line(line):
            self._log(line + "\n")
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

        def on_output():
            data = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
            state["buf"] += data
            lines = state["buf"].split("\n")
            state["buf"] = lines.pop()
            for line in lines:
                flush_line(line)

        def cleanup():
            self._quick_procs.discard(proc)
            if button:
                button.setEnabled(True)

        def on_finished(_exit_code, _exit_status):
            if state["buf"]:
                flush_line(state["buf"])
                state["buf"] = ""
            cleanup()

        def on_error(error):
            if error == QtCore.QProcess.FailedToStart:
                self._log(f"ERROR: {proc.errorString()}\n")
                cleanup()

        proc.readyReadStandardOutput.connect(on_output)
        proc.finished.connect(on_finished)
        proc.errorOccurred.connect(on_error)

        if button:
            button.setEnabled(False)
        self._quick_procs.add(proc)
        proc.start()

    # ------------------------------------------------------------------
    # run orchestration (main pipeline)
    # ------------------------------------------------------------------
    def _run_all(self):
        for cb in self.stage_checks.values():
            cb.setChecked(True)
        self._run_selected()

    def _run_selected(self):
        if self._pipeline_running:
            return
        movie = self.movie_edit.text().strip()
        base = self.out_edit.text().strip()
        if not base:
            QtWidgets.QMessageBox.critical(self, "Breakdown Studio", "Choose an output folder.")
            return
        stages = [sid for sid, *_ in STAGES if self.stage_checks[sid].isChecked()]
        if not stages:
            QtWidgets.QMessageBox.critical(self, "Breakdown Studio", "Select at least one stage.")
            return
        fetch_first = is_url(movie)
        needs_movie = any(nm for sid, _l, nm, _o in STAGES if sid in stages)
        if needs_movie and not fetch_first and not Path(movie).exists():
            QtWidgets.QMessageBox.critical(
                self, "Breakdown Studio", "Select a valid movie file, or paste a video URL.")
            return
        # Detect self-heal (UX_PLAN.md P0.3): try the dedicated transnet_python env first,
        # then fall back to worker_python if IT can already import transnetv2_pytorch
        # (single-venv installs). If neither works, skip Detect instead of failing the run.
        self._detect_interpreter = None
        if "detect" in stages:
            self._detect_interpreter = resolve_detect_interpreter(
                self.cfg, lambda p: probe_can_import(p, "transnetv2_pytorch"))
            if self._detect_interpreter is None:
                stages = [sid for sid in stages if sid != "detect"]
                self._log("\nDetect skipped: AI features not installed. Re-run the installer "
                          "and answer Y to the AI features question, or set TransNetV2 Python "
                          "in Settings.\n")
                if not stages:
                    QtWidgets.QMessageBox.critical(
                        self, "Breakdown Studio", "Select at least one stage.")
                    return
        self._persist()
        self._stop_flag = False
        self.run_btn.setEnabled(False)
        self.runall_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._pipeline_running = True
        self._pipeline_movie = movie
        self._pipeline_base = base
        # A URL in the Movie field runs a synthetic "fetch" stage first: bs_fetch.py
        # downloads it to output_base/_fetched/<title>.<ext>, prints "FETCHED <path>",
        # and _on_stage_finished swaps _pipeline_movie to that local path before any
        # movie-needing stage runs. Not added to self.stage_checks / STAGES: it's not a
        # user-toggleable checkbox, just plumbing that runs when the input is a URL.
        self._pipeline_stages = (["fetch"] if fetch_first else []) + stages
        self._pipeline_stage_index = 0
        self._pipeline_env = self._pipeline_environment()
        self._run_next_stage()

    def _pipeline_environment(self):
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("BS_FFMPEG", self.cfg["ffmpeg"])
        env.insert("BS_FFPROBE", self.cfg["ffprobe"])
        if self.cfg.get("google_client_secret"):
            env.insert("BS_GOOGLE_CLIENT_SECRET", self.cfg["google_client_secret"])
        if self.cfg.get("google_token"):
            env.insert("BS_GOOGLE_TOKEN", self.cfg["google_token"])
        return env

    def _scripts_dir(self):
        sd = (self.cfg.get("scripts_dir") or "").strip()
        p = Path(sd) if sd and sd != "." else APP_DIR
        if not p.is_absolute():
            p = (APP_DIR / p).resolve()
        return p if p.exists() else APP_DIR

    def _cmd_for(self, sid, movie, base):
        sd = self._scripts_dir()
        stem = Path(movie).stem
        wp = self.cfg["worker_python"] or sys.executable
        common = ["--movie", movie, "--output-base", base, "--workers", self.workers_edit.text()]
        if sid == "fetch":
            args = ["--url", movie, "--output-base", base, "--config", str(CONFIG)]
            fmt = (self.cfg.get("fetch_format") or "").strip()
            if fmt:
                args += ["--format", fmt]
            return worker_argv(Path(sd) / "bs_fetch.py", args, wp)
        if sid in ENRICH_STAGE_SUBCMD:
            args = [ENRICH_STAGE_SUBCMD[sid], "--movie", movie, "--output-base", base,
                    "--config", str(CONFIG)]
            return worker_argv(Path(sd) / "bs_enrich.py", args, wp)
        if sid == "detect":
            # _detect_interpreter is resolved once per run in _run_selected (transnet_python,
            # else worker_python if it can import transnetv2_pytorch); the cfg lookups here are
            # just a defensive fallback in case _cmd_for is ever called outside that path.
            interp = (self._detect_interpreter or self.cfg.get("transnet_python")
                     or self.cfg.get("worker_python") or sys.executable)
            return [interp, str(Path(sd) / "transnet_detect.py"),
                    "--movie", movie, "--output-base", base, "--threshold", self.thresh_edit.text()]
        if sid in ("frames", "cuts"):
            extra = ["--encoder", self.enc_combo.currentText()] if sid == "cuts" else []
            args = [sid, *common, "--prefix", self.prefix_edit.text(), *extra]
            return worker_argv(Path(sd) / "bs_worker.py", args, wp)
        if sid in ("thumbs", "refclips", "qc"):
            args = [sid, *common, "--prefix", self.prefix_edit.text()]
            return worker_argv(Path(sd) / "bs_worker.py", args, wp)
        if sid in OCR_STAGE_SUBCMD:
            frames_dir = Path(base) / stem / "frames"
            scenes_csv = find_scenes_csv(base, stem)
            if scenes_csv is None:
                return ("ERROR", f"No Scenes.csv found for '{stem}' under {base} "
                                 f"(run Detect + Frames first).")
            args = [OCR_STAGE_SUBCMD[sid], "--frames-dir", str(frames_dir),
                    "--scenes-csv", str(scenes_csv), "--config", str(CONFIG),
                    "--prefix", self.prefix_edit.text()]
            return worker_argv(Path(sd) / "bs_ocr.py", args, wp)
        if sid == "contact":
            out = str(Path(base) / stem / f"_{stem}_contact_sheet.jpg")
            args = ["--movie-stem", stem, "--output-base", base,
                    "--prefix", self.prefix_edit.text(), "--canvas", self.cfg["canvas"],
                    "--out", out]
            return worker_argv(Path(sd) / "contact_sheet.py", args, wp)
        if sid == "buildsheet":
            spreadsheet = self._sid(self.sid_edit.text())
            if not spreadsheet:
                return ("ERROR", "No Google sheet selected: create one or paste a Sheet ID/URL.")
            cmd = self._gs_cmd("build", "--spreadsheet-id", spreadsheet, "--movie", movie,
                               "--output-base", base, "--prefix", self.prefix_edit.text(),
                               "--tab", self.cfg.get("sheet_tab", "Shots_Breakdown"),
                               "--first-row", str(self.cfg.get("sheet_first_row", "3")))
            if self.thumbs_sheet_check.isChecked():
                cmd += ["--thumbs"]
            if self.dryrun_check.isChecked():
                cmd += ["--dry-run"]
            return cmd
        if sid == "match":
            spreadsheet = self._sid(self.sid_edit.text())
            master = self._sid(self.cfg.get("master_id", ""))
            if not spreadsheet:
                return ("ERROR", "No Google sheet selected: Match needs the built breakdown sheet.")
            if not master:
                return ("ERROR", "No master sheet set: add master_id in Settings to enable Match to master.")
            tab = self.cfg.get("sheet_tab", "Shots_Breakdown")
            args = ["assign", "--new-sheet-id", spreadsheet, "--new-tab", tab,
                    "--master-sheet-id", master, "--tab", tab,
                    "--write-sheet", spreadsheet, "--write-sheet-tab", tab]
            return worker_argv(Path(sd) / "bs_match.py", args, wp)
        if sid == "miro":
            spreadsheet = self._sid(self.sid_edit.text())
            if not spreadsheet:
                return ("ERROR", "No Google sheet selected: Miro push needs the built breakdown sheet.")
            token = (self.cfg.get("miro_token") or "").strip()
            board = (self.cfg.get("miro_board") or "").strip()
            if not token or not board:
                return ("ERROR", "Miro not configured: add miro_token and miro_board in Settings.")
            tab = self.cfg.get("sheet_tab", "Shots_Breakdown")
            state = str(Path(base) / "miro_board_state.json")
            header_row = str(int(str(self.cfg.get("sheet_first_row", "3")) or "3") - 1)
            args = ["push", "--spreadsheet-id", spreadsheet, "--tab", tab,
                    "--header-row", header_row, "--miro-token", token, "--miro-board", board,
                    "--state", state, "--title", self.cfg.get("new_title", "Shot wall"),
                    "--status-suffix"]
            if self.cfg.get("miro_only_status"):
                args += ["--only-status", self.cfg["miro_only_status"]]
            return worker_argv(Path(sd) / "bs_miro.py", args, wp)
        return None

    def _run_next_stage(self):
        if self._stop_flag or self._pipeline_stage_index >= len(self._pipeline_stages):
            if not self._stop_flag and self._pipeline_stages:
                self.stage_lbl.setText("Done")
                self.bar.setValue(100)
                self._log("\n=== pipeline finished ===\n")
            self._pipeline_finish()
            return

        sid = self._pipeline_stages[self._pipeline_stage_index]
        n = self._pipeline_stage_index + 1
        total = len(self._pipeline_stages)
        cmd = self._cmd_for(sid, self._pipeline_movie, self._pipeline_base)
        if isinstance(cmd, tuple) and cmd and cmd[0] == "ERROR":
            self._log(f"\n*** {cmd[1]} ***\n")
            self._pipeline_finish()
            return

        self.stage_lbl.setText(f"[{n}/{total}] {STAGE_LABELS[sid]}")
        self.bar.setValue(0)
        self.pct_lbl.setText("")
        self.eta_lbl.setText("")
        self._log(f"\n$ {' '.join(self._q(c) for c in cmd)}\n")

        self._stage_buf = ""
        self._prog_start_time = time.time()
        self._prog_last_time = self._prog_start_time
        self._prog_last_done = 0
        self._prog_deltas = []

        proc = QtCore.QProcess(self)
        proc.setProgram(str(cmd[0]))
        proc.setArguments([str(c) for c in cmd[1:]])
        proc.setWorkingDirectory(str(self._scripts_dir()))
        proc.setProcessEnvironment(self._pipeline_env)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_stage_output)
        proc.finished.connect(self._on_stage_finished)
        proc.errorOccurred.connect(self._on_stage_error)
        self._pipeline_process = proc
        proc.start()

    def _on_stage_output(self):
        proc = self._pipeline_process
        if proc is None:
            return
        data = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stage_buf += data
        lines = self._stage_buf.split("\n")
        self._stage_buf = lines.pop()
        for line in lines:
            self._handle_stage_line(line)

    def _handle_stage_line(self, line):
        if line.startswith("PROGRESS "):
            try:
                _, _st, frac = line.split()
                done, total = (int(x) for x in frac.split("/"))
                self._update_progress(done, total)
            except Exception:
                pass
        elif line.startswith("FETCHED ") or line.startswith("SKIP "):
            # bs_fetch.py's result line: swap the URL for the downloaded local file so
            # every subsequent stage in this run (frames, detect, ...) sees a real path.
            local_path = line.split(" ", 1)[1].strip()
            self._pipeline_movie = local_path
            self.movie_edit.setText(local_path)
            self._log(line + "\n")
        else:
            self._log(line + "\n")

    def _update_progress(self, done, total):
        pct = (done / total * 100) if total else 0
        self.bar.setValue(int(pct))
        self.pct_lbl.setText(f"{done}/{total}")

        # rolling average of completed-shot durations -> ETA (appears after first shot)
        now = time.time()
        units = done - self._prog_last_done
        dt = now - self._prog_last_time
        if units > 0 and dt >= 0:
            self._prog_deltas.append(dt / units)
            if len(self._prog_deltas) > 25:
                self._prog_deltas.pop(0)
        self._prog_last_done = done
        self._prog_last_time = now

        remaining = total - done
        if remaining > 0 and self._prog_deltas:
            avg = sum(self._prog_deltas) / len(self._prog_deltas)
            self.eta_lbl.setText(f"ETA {_fmt_eta(avg * remaining)}")
        elif remaining <= 0:
            self.eta_lbl.setText(f"took {_fmt_eta(now - self._prog_start_time)}")

    def _on_stage_finished(self, exit_code, _exit_status):
        if self._stage_buf:
            self._handle_stage_line(self._stage_buf)
            self._stage_buf = ""
        self._pipeline_process = None

        if not self._pipeline_stages:
            return
        sid = self._pipeline_stages[self._pipeline_stage_index]

        if self._stop_flag:
            self._log("\n[stopped]\n")
            self._pipeline_finish()
            return

        if exit_code != 0:
            self._log(f"\n*** stage '{sid}' failed (exit {exit_code}). Stopping. ***\n")
            self.bar.setValue(0)
            self._pipeline_finish()
            return

        self._pipeline_stage_index += 1
        self._run_next_stage()

    def _on_stage_error(self, error):
        proc = self._pipeline_process
        if error == QtCore.QProcess.FailedToStart and proc is not None:
            # FailedToStart never emits finished(): end the pipeline here.
            self._log(f"\nERROR launching: {proc.errorString()}\n")
            if self._pipeline_stages:
                sid = self._pipeline_stages[self._pipeline_stage_index]
                self._log(f"\n*** stage '{sid}' failed (could not start). Stopping. ***\n")
            self.bar.setValue(0)
            self._pipeline_process = None
            self._pipeline_finish()

    def _pipeline_finish(self):
        self._pipeline_running = False
        self.run_btn.setEnabled(True)
        self.runall_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _stop(self):
        self._stop_flag = True
        if self._pipeline_process is not None and \
                self._pipeline_process.state() != QtCore.QProcess.NotRunning:
            self._pipeline_process.terminate()
        self._log("\n[stop requested]\n")

    @staticmethod
    def _q(s):
        return f'"{s}"' if " " in str(s) else str(s)

    # ------------------------------------------------------------------
    # log
    # ------------------------------------------------------------------
    def _log(self, text):
        self.log_edit.moveCursor(QtGui.QTextCursor.End)
        self.log_edit.insertPlainText(text)
        self.log_edit.ensureCursorVisible()

    def _save_log(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save log", "", "Text files (*.txt);;All files (*.*)")
        if p:
            Path(p).write_text(self.log_edit.toPlainText(), encoding="utf-8")


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
