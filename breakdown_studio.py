#!/usr/bin/env python
"""Breakdown Studio - a GUI for the film shot-breakdown / thumbnail / reference-clip pipeline.

Cross-platform (Tkinter, Python standard library only). It orchestrates the existing tools as
subprocesses so the whole process is repeatable without the command line:

  Detect (TransNetV2) -> Frames -> Thumbnails -> Cut clips -> Contact sheet -> Reference clips

Each stage runs the right interpreter for its dependencies (the torch env for TransNetV2, a worker
Python with Pillow+numpy for everything else) and streams its log into the window with a progress
bar. Paths are stored in config.json next to this file, so the app installs onto any machine by
copying the folder and pointing it at that machine's Python/ffmpeg in Settings.

Run:  python breakdown_studio.py        (or use the launcher for your OS)
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bs_launch_helpers import (
    worker_argv, adopt_bs_env, resolve_detect_interpreter, probe_can_import, doctor_argv,
)

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
    """Fill blank paths with anything we can find on this machine (best effort)."""
    import shutil
    if not cfg.get("ffmpeg") or cfg["ffmpeg"] == "ffmpeg":
        cfg["ffmpeg"] = shutil.which("ffmpeg") or cfg["ffmpeg"]
    if not cfg.get("ffprobe") or cfg["ffprobe"] == "ffprobe":
        cfg["ffprobe"] = shutil.which("ffprobe") or cfg["ffprobe"]
    # ffmpeg/ffprobe come from PATH if not set; the TransNet interpreter has no generic default,
    # so set it once in Settings (it lives in the torch venv the installer makes).
    return cfg


def preflight_warnings(cfg):
    """Cheap, non-blocking startup check: is ffmpeg reachable, does worker_python exist?
    Returns a short list of human-readable warning strings (empty if all good)."""
    import shutil
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


# =============================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Breakdown Studio")
        self.geometry("980x720")
        self.minsize(820, 600)
        self.cfg = autodetect(load_config())
        # preflight self-heal (UX_PLAN.md P0.3): adopt the installer's bs_env if worker_python
        # is blank/stale, silently -- logged once the log widget exists, below.
        adopted = adopt_bs_env(self.cfg, APP_DIR)
        if adopted:
            save_config(self.cfg)
        self.q = queue.Queue()
        self.proc = None
        self.worker = None
        self.stop_flag = threading.Event()
        self.stage_vars = {}
        self._detect_interpreter = None  # resolved per-run by _run_selected; see _cmd_for
        self._build()
        self._show_preflight_warnings()
        if adopted:
            self._log(f"Adopted worker environment: {self.cfg['worker_python']}\n")
        self._update_url_hint()
        self.after(80, self._drain)

    # ---- layout -------------------------------------------------------------
    def _build(self):
        pad = dict(padx=8, pady=4)
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        # movie + output rows
        self.movie_var = tk.StringVar(value=self.cfg.get("last_movie", ""))
        self.out_var = tk.StringVar(value=self.cfg.get("output_base", ""))
        self._path_row(top, "Movie file", self.movie_var, self._pick_movie, 0)
        self._path_row(top, "Output folder", self.out_var, self._pick_out, 1)

        # URL hint: shown only when the Movie field looks like http(s)://... -- Run will
        # fetch it (yt-dlp / direct download) into output_base/_fetched/ before the rest
        # of the selected stages run. Created here (right after the movie/output rows) but
        # NOT packed yet -- `top` is the only other widget packed so far, so pack(before=top)
        # in _update_url_hint() always re-inserts it right after `top` regardless of how
        # many times it's toggled, instead of a bare pack() re-appending it at the end of
        # the stack (after the log box's fill+expand has already claimed the remaining space).
        self._url_hint_anchor = top
        self.url_hint_lbl = ttk.Label(
            self,
            text="This looks like a video URL. Click Run and it will be fetched "
                 "automatically before the pipeline starts.",
            foreground="#069", anchor="w")
        self.movie_var.trace_add("write", lambda *_: self._update_url_hint())

        # preflight banner (hidden unless something is missing)
        self.preflight_lbl = ttk.Label(self, text="", foreground="#a60",
                                       background="#fff3cd", anchor="w")

        # options row
        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        self.prefix_var = tk.StringVar(value=self.cfg["prefix"])
        self.enc_var = tk.StringVar(value=self.cfg["encoder"])
        self.workers_var = tk.StringVar(value=self.cfg["workers"])
        self.thresh_var = tk.StringVar(value=self.cfg["threshold"])
        ttk.Label(opt, text="Prefix").pack(side="left")
        ttk.Entry(opt, textvariable=self.prefix_var, width=7).pack(side="left", padx=(2, 12))
        ttk.Label(opt, text="Encoder").pack(side="left")
        ttk.Combobox(opt, textvariable=self.enc_var, values=["libx264", "nvenc"], width=8,
                     state="readonly").pack(side="left", padx=(2, 12))
        ttk.Label(opt, text="Workers").pack(side="left")
        ttk.Entry(opt, textvariable=self.workers_var, width=5).pack(side="left", padx=(2, 12))
        ttk.Label(opt, text="Detect thr.").pack(side="left")
        ttk.Entry(opt, textvariable=self.thresh_var, width=6).pack(side="left", padx=(2, 12))
        ttk.Button(opt, text="Settings…", command=self._settings).pack(side="right")

        # stages
        sf = ttk.LabelFrame(self, text="Pipeline stages")
        sf.pack(fill="x", **pad)
        for i, (sid, label, _nm, on) in enumerate(STAGES):
            v = tk.BooleanVar(value=on)
            self.stage_vars[sid] = v
            ttk.Checkbutton(sf, text=label, variable=v).grid(
                row=i // 3, column=i % 3, sticky="w", padx=10, pady=3)

        # google breakdown sheet
        gf = ttk.LabelFrame(self, text="Google breakdown sheet  (connect your own account)")
        gf.pack(fill="x", **pad)
        g1 = ttk.Frame(gf); g1.pack(fill="x", padx=6, pady=3)
        ttk.Button(g1, text="Connect Google…", command=self._connect_google).pack(side="left")
        self.gacct = ttk.Label(g1, text="not connected", foreground="#888")
        self.gacct.pack(side="left", padx=10)
        self.mode_var = tk.StringVar(value=self.cfg.get("sheet_mode", "existing"))
        ttk.Radiobutton(g1, text="New film (from template)", value="new",
                        variable=self.mode_var, command=self._mode_changed).pack(side="left", padx=(20, 4))
        ttk.Radiobutton(g1, text="Existing project", value="existing",
                        variable=self.mode_var, command=self._mode_changed).pack(side="left")
        g2 = ttk.Frame(gf); g2.pack(fill="x", padx=6, pady=3)
        self.new_title_var = tk.StringVar(value=self.cfg.get("new_title", ""))
        self.sid_var = tk.StringVar(value=self.cfg.get("spreadsheet_id", ""))
        self.thumbs_sheet_var = tk.BooleanVar(value=True)
        self.dryrun_var = tk.BooleanVar(value=False)
        self.new_title_lbl = ttk.Label(g2, text="New title")
        self.new_title_ent = ttk.Entry(g2, textvariable=self.new_title_var, width=34)
        self.create_btn = ttk.Button(g2, text="Create sheet", command=self._create_sheet)
        ttk.Label(g2, text="Sheet ID/URL").pack(side="left")
        ttk.Entry(g2, textvariable=self.sid_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Checkbutton(g2, text="upload thumbnails", variable=self.thumbs_sheet_var).pack(side="right")
        self._mode_changed()
        g3 = ttk.Frame(gf); g3.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Checkbutton(g3, text="Preview only: show adds/updates, write nothing",
                        variable=self.dryrun_var).pack(side="left")
        ttk.Button(g3, text="Make blank template from master…",
                   command=self._make_template).pack(side="right")
        self.tmpl_lbl = ttk.Label(g3, text=("template: " + (self.cfg.get("template_id") or "none")),
                                  foreground="#888")
        self.tmpl_lbl.pack(side="right", padx=10)

        # run buttons
        rb = ttk.Frame(self)
        rb.pack(fill="x", **pad)
        self.run_btn = ttk.Button(rb, text="▶  Run selected", command=self._run_selected)
        self.run_btn.pack(side="left")
        self.runall_btn = ttk.Button(rb, text="Run all", command=self._run_all)
        self.runall_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(rb, text="■ Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(rb, text="Open output folder", command=self._open_out).pack(side="right")

        # progress
        pf = ttk.Frame(self)
        pf.pack(fill="x", **pad)
        self.stage_lbl = ttk.Label(pf, text="Idle", width=34)
        self.stage_lbl.pack(side="left")
        self.bar = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=8)
        self.pct_lbl = ttk.Label(pf, text="", width=10)
        self.pct_lbl.pack(side="left")

        # log
        lf = ttk.LabelFrame(self, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(lf, height=18, wrap="none", bg="#111", fg="#d8d8d8",
                           insertbackground="#d8d8d8", font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="Save log…", command=self._save_log).pack(side="right")

    # ---- startup preflight ---------------------------------------------------
    def _show_preflight_warnings(self):
        warnings = preflight_warnings(self.cfg)
        if warnings:
            self.preflight_lbl.config(text="  " + "   |   ".join(warnings) + "  ")
            self.preflight_lbl.pack(fill="x", padx=8, pady=(0, 4))
        else:
            self.preflight_lbl.pack_forget()

    # ---- video-URL input ------------------------------------------------------
    def _update_url_hint(self):
        if is_url(self.movie_var.get()):
            # before=<the movie/output-path frame>'s next sibling slot: always reinserts
            # right after the path rows, never at the end of an already-packed stack.
            self.url_hint_lbl.pack(fill="x", padx=8, pady=(0, 4), after=self._url_hint_anchor)
        else:
            self.url_hint_lbl.pack_forget()

    def _path_row(self, parent, label, var, cmd, row):
        ttk.Label(parent, text=label, width=13).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(parent, text="Browse…", command=cmd).grid(row=row, column=2)
        parent.columnconfigure(1, weight=1)

    # ---- pickers ------------------------------------------------------------
    def _pick_movie(self):
        p = filedialog.askopenfilename(title="Select reference movie",
                                       filetypes=[("Video", "*.mov *.mp4 *.mkv *.mxf *.avi"),
                                                  ("All files", "*.*")])
        if p:
            self.movie_var.set(p)
            if not self.out_var.get():
                self.out_var.set(str(Path(p).parent / "breakdown_output"))

    def _pick_out(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.out_var.set(p)

    def _open_out(self):
        base = self.out_var.get()
        stem = Path(self.movie_var.get()).stem if self.movie_var.get() else ""
        d = Path(base) / stem if stem else Path(base)
        if d.exists():
            if sys.platform.startswith("win"):
                os.startfile(d)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
        else:
            messagebox.showinfo("Breakdown Studio", f"Not created yet:\n{d}")

    # ---- settings dialog ----------------------------------------------------
    def _settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("820x510")
        win.transient(self)
        rows = [
            ("transnet_python", "TransNetV2 Python (torch)", True),
            ("worker_python", "Worker Python (Pillow+numpy)", True),
            ("ffmpeg", "ffmpeg binary", True),
            ("ffprobe", "ffprobe binary", True),
            ("scripts_dir", "Scripts folder", False),
            ("canvas", "Contact-sheet canvas (WxH)", False),
            ("google_client_secret", "Google OAuth client secret JSON", True),
            ("google_token", "Google token cache (auto)", True),
            ("master_id", "Master spreadsheet ID (to template)", False),
            ("template_id", "Blank-template spreadsheet ID", False),
            ("sheet_tab", "Breakdown tab name", False),
            ("sheet_first_row", "First data row", False),
        ]
        vars_ = {}
        for i, (key, label, isfile) in enumerate(rows):
            ttk.Label(win, text=label, width=26).grid(row=i, column=0, sticky="w", padx=8, pady=6)
            v = tk.StringVar(value=self.cfg.get(key, ""))
            vars_[key] = v
            ttk.Entry(win, textvariable=v, width=64).grid(row=i, column=1, sticky="ew", padx=4)
            if isfile:
                ttk.Button(win, text="…", width=3,
                           command=lambda vv=v: vv.set(filedialog.askopenfilename() or vv.get())
                           ).grid(row=i, column=2, padx=4)
        win.columnconfigure(1, weight=1)

        def save():
            for k, v in vars_.items():
                self.cfg[k] = v.get().strip()
            self._persist()
            win.destroy()
            self._log("[settings saved]\n")
            self._show_preflight_warnings()
        ttk.Button(win, text="Run doctor", command=self._run_doctor).grid(
            row=len(rows), column=0, sticky="w", padx=8, pady=10)
        ttk.Button(win, text="Save", command=save).grid(row=len(rows), column=1, sticky="e", pady=10)

    def _run_doctor(self):
        """Settings 'Run doctor' button: shell '<worker_python> bs_launcher.py doctor' and show
        its PASS/WARN/FAIL table in a small scrolled-text dialog (UX_PLAN.md P3)."""
        cmd = doctor_argv(self.cfg.get("worker_python"))
        dlg = tk.Toplevel(self)
        dlg.title("Doctor")
        dlg.geometry("760x520")
        dlg.transient(self)
        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        txt = tk.Text(body, wrap="word", bg="#111", fg="#d8d8d8",
                      insertbackground="#d8d8d8", font=("Consolas", 9))
        sb = ttk.Scrollbar(body, command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("end", "Running doctor...\n")

        def work():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", cwd=str(APP_DIR))
                out = (r.stdout or "") + (r.stderr or "")
            except Exception as e:
                out = f"ERROR running doctor: {e}\n"

            def show():
                txt.delete("1.0", "end")
                txt.insert("end", out)
            self.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    # ---- config persistence -------------------------------------------------
    def _persist(self):
        self.cfg.update({
            "last_movie": self.movie_var.get(), "output_base": self.out_var.get(),
            "prefix": self.prefix_var.get(), "encoder": self.enc_var.get(),
            "workers": self.workers_var.get(), "threshold": self.thresh_var.get(),
            "spreadsheet_id": self.sid_var.get().strip(), "sheet_mode": self.mode_var.get(),
            "new_title": self.new_title_var.get().strip(),
        })
        save_config(self.cfg)

    # ---- google helpers -----------------------------------------------------
    def _mode_changed(self):
        new = self.mode_var.get() == "new"
        for w in (self.new_title_lbl, self.new_title_ent, self.create_btn):
            (w.pack(side="left", padx=2) if new else w.pack_forget())

    def _google_env(self):
        env = dict(os.environ)
        if self.cfg.get("google_client_secret"):
            env["BS_GOOGLE_CLIENT_SECRET"] = self.cfg["google_client_secret"]
        if self.cfg.get("google_token"):
            env["BS_GOOGLE_TOKEN"] = self.cfg["google_token"]
        env["BS_FFPROBE"] = self.cfg["ffprobe"]
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
        self._log("\n[connecting to Google: a browser may open to sign in]\n")
        self._run_quick(self._gs_cmd("whoami"), self._google_env(),
                        on_line=lambda ln: self.q.put(("gacct", ln.split("CONNECTED", 1)[1].strip()))
                        if "CONNECTED" in ln else None)

    def _create_sheet(self):
        tid = self.cfg.get("template_id", "").strip()
        if not tid:
            messagebox.showerror("Breakdown Studio", "Set a template_id in config.json first "
                                 "(run make_blank_template.py to create one).")
            return
        title = self.new_title_var.get().strip() or "New Film - Shot Breakdown"
        self._log(f"\n[creating '{title}' from template]\n")

        def on_line(ln):
            if "NEW_SPREADSHEET_ID" in ln:
                self.q.put(("sid", ln.split()[1]))
        self._run_quick(self._gs_cmd("copy-template", "--template-id", tid, "--title", title),
                        self._google_env(), on_line=on_line)

    def _make_template(self):
        master = self.cfg.get("master_id", "").strip()
        if not master:
            messagebox.showerror("Breakdown Studio",
                                 "Set 'Master spreadsheet ID' in Settings first: the master to copy "
                                 "and strip into a fresh blank template.")
            return
        if not messagebox.askyesno("Make blank template",
                                   "Copy the master (read-only) and strip the copy into a NEW blank "
                                   "template?\n\nThe master is never modified."):
            return
        self._log("\n[deriving blank template from master: master is copied, never modified]\n")
        args = ["--master-id", master]
        if self.cfg.get("google_client_secret"):
            args += ["--client-secret", self.cfg["google_client_secret"]]
        if self.cfg.get("google_token"):
            args += ["--token", self.cfg["google_token"]]
        cmd = worker_argv(self._scripts_dir() / "make_blank_template.py", args,
                           self.cfg["worker_python"] or sys.executable)

        def on_line(ln):
            if ln.startswith("TEMPLATE_ID "):
                self.q.put(("template", ln.split()[1]))
        self._run_quick(cmd, self._google_env(), on_line=on_line)

    def _run_quick(self, cmd, env, on_line=None):
        """Run a short subprocess in a thread, stream to log, callback per line."""
        def work():
            self.q.put(("log", f"\n$ {' '.join(self._q(c) for c in cmd)}\n"))
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", env=env,
                                     cwd=str(self._scripts_dir()))
            except FileNotFoundError as e:
                self.q.put(("log", f"ERROR: {e}\n"))
                return
            for line in p.stdout:
                self.q.put(("log", line))
                if on_line:
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:
                        pass
            p.wait()
        threading.Thread(target=work, daemon=True).start()

    # ---- run orchestration --------------------------------------------------
    def _run_all(self):
        for v in self.stage_vars.values():
            v.set(True)
        self._run_selected()

    def _run_selected(self):
        if self.worker and self.worker.is_alive():
            return
        movie = self.movie_var.get().strip()
        base = self.out_var.get().strip()
        if not base:
            messagebox.showerror("Breakdown Studio", "Choose an output folder.")
            return
        stages = [sid for sid, *_ in STAGES if self.stage_vars[sid].get()]
        if not stages:
            messagebox.showerror("Breakdown Studio", "Select at least one stage.")
            return
        fetch_first = is_url(movie)
        needs_movie = any(nm for sid, _l, nm, _o in STAGES if sid in stages)
        if needs_movie and not fetch_first and not Path(movie).exists():
            messagebox.showerror("Breakdown Studio",
                                 "Select a valid movie file, or paste a video URL.")
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
                    messagebox.showerror("Breakdown Studio", "Select at least one stage.")
                    return
        self._persist()
        self.stop_flag.clear()
        self.run_btn.config(state="disabled")
        self.runall_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        # A URL in the Movie field runs a synthetic "fetch" stage first: bs_fetch.py
        # downloads it to output_base/_fetched/<title>.<ext>, prints "FETCHED <path>", and
        # _run_pipeline swaps its local `movie` var to that path before any movie-needing
        # stage runs. Not added to self.stage_vars / STAGES: it's not a user-toggleable
        # checkbox, just plumbing that runs when the input is a URL.
        all_stages = (["fetch"] if fetch_first else []) + stages
        self.worker = threading.Thread(target=self._run_pipeline, args=(movie, base, all_stages),
                                       daemon=True)
        self.worker.start()

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
        common = ["--movie", movie, "--output-base", base, "--workers", self.workers_var.get()]
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
                    "--movie", movie, "--output-base", base, "--threshold", self.thresh_var.get()]
        if sid in ("frames", "cuts"):
            extra = ["--encoder", self.enc_var.get()] if sid == "cuts" else []
            args = [sid, *common, "--prefix", self.prefix_var.get(), *extra]
            return worker_argv(Path(sd) / "bs_worker.py", args, wp)
        if sid in ("thumbs", "refclips", "qc"):
            args = [sid, *common, "--prefix", self.prefix_var.get()]
            return worker_argv(Path(sd) / "bs_worker.py", args, wp)
        if sid in OCR_STAGE_SUBCMD:
            frames_dir = Path(base) / stem / "frames"
            scenes_csv = find_scenes_csv(base, stem)
            if scenes_csv is None:
                return ("ERROR", f"No Scenes.csv found for '{stem}' under {base} "
                                 f"(run Detect + Frames first).")
            args = [OCR_STAGE_SUBCMD[sid], "--frames-dir", str(frames_dir),
                    "--scenes-csv", str(scenes_csv), "--config", str(CONFIG),
                    "--prefix", self.prefix_var.get()]
            return worker_argv(Path(sd) / "bs_ocr.py", args, wp)
        if sid == "contact":
            out = str(Path(base) / stem / f"_{stem}_contact_sheet.jpg")
            args = ["--movie-stem", stem, "--output-base", base,
                    "--prefix", self.prefix_var.get(), "--canvas", self.cfg["canvas"],
                    "--out", out]
            return worker_argv(Path(sd) / "contact_sheet.py", args, wp)
        if sid == "buildsheet":
            spreadsheet = self._sid(self.sid_var.get())
            if not spreadsheet:
                return ("ERROR", "No Google sheet selected: create one or paste a Sheet ID/URL.")
            cmd = self._gs_cmd("build", "--spreadsheet-id", spreadsheet, "--movie", movie,
                               "--output-base", base, "--prefix", self.prefix_var.get(),
                               "--tab", self.cfg.get("sheet_tab", "Shots_Breakdown"),
                               "--first-row", str(self.cfg.get("sheet_first_row", "3")))
            if self.thumbs_sheet_var.get():
                cmd += ["--thumbs"]
            if self.dryrun_var.get():
                cmd += ["--dry-run"]
            return cmd
        if sid == "match":
            spreadsheet = self._sid(self.sid_var.get())
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
            spreadsheet = self._sid(self.sid_var.get())
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

    def _run_pipeline(self, movie, base, stages):
        env = dict(os.environ)
        env["BS_FFMPEG"] = self.cfg["ffmpeg"]
        env["BS_FFPROBE"] = self.cfg["ffprobe"]
        if self.cfg.get("google_client_secret"):
            env["BS_GOOGLE_CLIENT_SECRET"] = self.cfg["google_client_secret"]
        if self.cfg.get("google_token"):
            env["BS_GOOGLE_TOKEN"] = self.cfg["google_token"]
        for n, sid in enumerate(stages, 1):
            if self.stop_flag.is_set():
                break
            cmd = self._cmd_for(sid, movie, base)
            if isinstance(cmd, tuple) and cmd and cmd[0] == "ERROR":
                self.q.put(("log", f"\n*** {cmd[1]} ***\n"))
                self.q.put(("error", sid))
                break
            self.q.put(("stage", f"[{n}/{len(stages)}] {STAGE_LABELS[sid]}"))
            self.q.put(("log", f"\n$ {' '.join(self._q(c) for c in cmd)}\n"))
            rc, fetched = self._spawn(cmd, env)
            if fetched:
                # bs_fetch.py's FETCHED/SKIP line: swap the URL for the downloaded local
                # file so every subsequent stage in this run sees a real path.
                movie = fetched
                self.q.put(("movie", fetched))
            if rc != 0 and not self.stop_flag.is_set():
                self.q.put(("log", f"\n*** stage '{sid}' failed (exit {rc}). Stopping. ***\n"))
                self.q.put(("error", sid))
                break
        else:
            self.q.put(("done_all", ""))
            self.q.put(("log", "\n=== pipeline finished ===\n"))
        self.q.put(("finish", ""))

    @staticmethod
    def _q(s):
        return f'"{s}"' if " " in str(s) else str(s)

    def _spawn(self, cmd, env):
        """Run one stage subprocess to completion, streaming PROGRESS/log lines to the UI
        queue. Returns (returncode, fetched_path_or_None) -- fetched_path is set when the
        stage printed bs_fetch.py's "FETCHED <path>" / "SKIP <path>" result line."""
        fetched = None
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace",
                                         bufsize=1, env=env, cwd=str(self._scripts_dir()))
        except FileNotFoundError as e:
            self.q.put(("log", f"\nERROR launching: {e}\n"))
            return 1, None
        for line in self.proc.stdout:
            if self.stop_flag.is_set():
                self.proc.terminate()
                break
            if line.startswith("PROGRESS "):
                try:
                    _, st, frac = line.split()
                    done, total = (int(x) for x in frac.split("/"))
                    self.q.put(("prog", (done, total)))
                except Exception:
                    pass
            else:
                if line.startswith("FETCHED ") or line.startswith("SKIP "):
                    fetched = line.split(" ", 1)[1].strip()
                self.q.put(("log", line))
        self.proc.wait()
        return self.proc.returncode, fetched

    def _stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self._log("\n[stop requested]\n")

    # ---- UI queue drain -----------------------------------------------------
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "stage":
                    self.stage_lbl.config(text=payload)
                    self.bar.config(value=0)
                    self.pct_lbl.config(text="")
                elif kind == "prog":
                    done, total = payload
                    pct = (done / total * 100) if total else 0
                    self.bar.config(value=pct)
                    self.pct_lbl.config(text=f"{done}/{total}")
                elif kind == "movie":
                    self.movie_var.set(payload)
                elif kind == "gacct":
                    self.gacct.config(text=payload, foreground="#2a7")
                elif kind == "sid":
                    self.sid_var.set(payload)
                    self.mode_var.set("existing")
                    self._mode_changed()
                    self._persist()
                    self._log(f"[sheet ready: {payload}]\n")
                elif kind == "template":
                    self.cfg["template_id"] = payload
                    save_config(self.cfg)
                    self.tmpl_lbl.config(text=f"template: {payload}", foreground="#2a7")
                    self.mode_var.set("new")
                    self._mode_changed()
                    self._log(f"[blank template ready: {payload}: saved as template_id]\n")
                elif kind == "error":
                    self.bar.config(value=0)
                elif kind == "done_all":
                    self.stage_lbl.config(text="Done")
                    self.bar.config(value=100)
                elif kind == "finish":
                    self.run_btn.config(state="normal")
                    self.runall_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def _save_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt",
                                         filetypes=[("Text", "*.txt")])
        if p:
            Path(p).write_text(self.log.get("1.0", "end"), encoding="utf-8")


if __name__ == "__main__":
    App().mainloop()
