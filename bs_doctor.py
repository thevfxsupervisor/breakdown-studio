#!/usr/bin/env python
"""bs_doctor.py - environment self-check for Breakdown Studio.

Run 'python bs_launcher.py doctor' (or, once packaged, 'breakdown-studio doctor') to get a
PASS/WARN/FAIL table of the local install: Python version, worker imports, optional AI
extras, GUI toolkits, ffmpeg/ffprobe, and config.json. This is meant to be the first
troubleshooting step (README/QUICKSTART point here) instead of copy-pasted paths or blind
pixel-typing.

Kept stdlib-only at IMPORT time: every probe that touches an optional dependency (PIL,
numpy, googleapiclient, torch, transnetv2_pytorch, easyocr, tkinter, PySide6) is wrapped in
its own try/except inside a small check function, so this module (and 'python bs_launcher.py
doctor') runs under ANY interpreter, even a bare system Python with none of the extras
installed.

CLI:  python bs_doctor.py            (same as 'python bs_launcher.py doctor')
Exit code: 1 if any check is FAIL, else 0 (WARN/INFO rows never fail the run: they flag
optional features that are not set up yet, not a broken install).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG = APP_DIR / "config.json"

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


# =============================================================================================
# individual checks -- each returns (name, status, detail); no import-time side effects, and
# every optional dependency is imported INSIDE the function so this module always loads.
# =============================================================================================

def check_python_version(version_info=None):
    """version_info is injectable (defaults to sys.version_info) so this stays testable
    without needing to run under an old interpreter."""
    v = version_info or sys.version_info
    ok = (v[0], v[1]) >= (3, 9)
    detail = f"{v[0]}.{v[1]}.{v[2] if len(v) > 2 else 0}"
    return ("Python version", PASS if ok else FAIL, detail)


def check_in_bs_env(executable=None):
    """Informational only: is this interpreter running out of a 'bs_env' folder (the
    installer's venv name, see UX_PLAN.md P0.1)? Never WARN/FAIL, just tells the user what
    they're running under."""
    exe = Path(executable or sys.executable)
    in_bs_env = "bs_env" in exe.parts
    return ("Running inside bs_env", INFO, f"{exe} ({'yes' if in_bs_env else 'no'})")


def _try_import(module_name):
    try:
        __import__(module_name)
        return True, ""
    except Exception as e:
        return False, str(e)


def check_worker_import(module_name, display_name=None):
    """Worker-env dependency (Pillow/numpy/googleapiclient): every stage except Detect/OCR/AI
    enrichment needs these, so a missing one is a FAIL, not a WARN."""
    display_name = display_name or module_name
    ok, err = _try_import(module_name)
    if ok:
        return (f"worker import: {display_name}", PASS, "")
    return (f"worker import: {display_name}", FAIL,
            f"missing: pip install -r requirements-worker.txt ({err})")


def check_ai_import(module_name, display_name=None):
    """AI extra (torch / transnetv2_pytorch / easyocr): optional, only needed for Detect and
    the OCR stages, so a missing one is a WARN, not a FAIL."""
    display_name = display_name or module_name
    ok, err = _try_import(module_name)
    if ok:
        return (f"AI extra: {display_name}", PASS, "")
    return (f"AI extra: {display_name}", WARN,
            "AI features not installed, Detect and OCR stages unavailable, "
            "re-run installer and answer Y")


def check_tkinter():
    ok, err = _try_import("tkinter")
    if ok:
        return ("GUI: tkinter", PASS, "")
    return ("GUI: tkinter", WARN, f"tkinter unavailable ({err})")


def check_pyside6():
    """Informational: PySide6 is one of two GUIs (Tkinter is the always-available fallback),
    so its absence is not a warning, just a fact."""
    ok, err = _try_import("PySide6")
    if ok:
        return ("GUI: PySide6", INFO, "installed")
    return ("GUI: PySide6", INFO, "not installed (Tkinter GUI still works)")


def resolve_ffbinary(cfg, key, default_name):
    """Same resolve order the GUIs use for ffmpeg/ffprobe: config.json value first (if it
    exists as a file, or resolves on PATH), else the bare name on PATH."""
    val = (cfg.get(key) or "").strip()
    if val and Path(val).exists():
        return val
    if val:
        found = shutil.which(val)
        if found:
            return found
    return shutil.which(default_name)


def check_ffbinary(cfg, key, default_name, label, run_fn=None):
    """run_fn(argv) -> CompletedProcess is injectable for tests; defaults to subprocess.run."""
    run_fn = run_fn or subprocess.run
    path = resolve_ffbinary(cfg, key, default_name)
    if not path:
        return (label, FAIL, f"not found on PATH or in config.json ('{key}')")
    try:
        r = run_fn([path, "-version"], capture_output=True, text=True, timeout=10)
    except Exception as e:
        return (label, FAIL, f"could not run '{path} -version': {e}")
    text = (r.stdout or "") + (r.stderr or "")
    first_line = text.splitlines()[0].strip() if text.strip() else ""
    if r.returncode == 0 and first_line:
        return (label, PASS, first_line)
    return (label, FAIL, f"'{path} -version' failed (exit {r.returncode})")


def check_config_exists(config_path=None, frozen=None):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    config_path = Path(config_path) if config_path else CONFIG
    if config_path.exists():
        return ("config.json", PASS, str(config_path))
    if frozen:
        # A fresh unzip has no config yet; the app writes one on the first Settings save.
        return ("config.json", WARN,
                "no settings saved yet: open the app, set ffmpeg/ffprobe in Settings, Save")
    return ("config.json", FAIL,
            f"not found at {config_path}: copy config.example.json to config.json")


def check_worker_python(cfg, frozen=None):
    """frozen is injectable for tests; defaults to sys.frozen (set by PyInstaller)."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    val = (cfg.get("worker_python") or "").strip()
    if frozen:
        # The packaged build dispatches pipeline stages to itself: no external Python is
        # needed, so an unset worker_python is expected there, not a problem.
        return ("config: worker_python", INFO,
                val or "not needed: the packaged build runs stages self-contained")
    if not val:
        return ("config: worker_python", FAIL,
                "not set: run the installer, or set it in Settings")
    if not Path(val).exists():
        return ("config: worker_python", FAIL, f"set but the file does not exist: {val}")
    return ("config: worker_python", PASS, val)


def check_transnet_python(cfg, frozen=None):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    val = (cfg.get("transnet_python") or "").strip()
    if not val:
        if frozen:
            return ("config: transnet_python", INFO,
                    "blank: the packaged build ships without AI features; point this at an "
                    "AI-features Python to enable Detect and OCR (README, Advanced)")
        return ("config: transnet_python", WARN,
                "blank: Detect will try worker_python, then skip if it can't import "
                "transnetv2_pytorch")
    if not Path(val).exists():
        return ("config: transnet_python", WARN, f"set but the file does not exist: {val}")
    return ("config: transnet_python", PASS, val)


def check_google_client_secret(cfg):
    val = (cfg.get("google_client_secret") or "").strip()
    if not val:
        return ("config: google_client_secret", WARN,
                "blank: Google Sheets stages disabled until set, see SETUP_GOOGLE.md")
    if not Path(val).exists():
        return ("config: google_client_secret", WARN, f"set but the file does not exist: {val}")
    return ("config: google_client_secret", PASS, val)


def load_config(config_path=None):
    config_path = Path(config_path) if config_path else CONFIG
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# =============================================================================================
# assembly + formatting -- pure functions, unit-testable without subprocess/ffmpeg/OCR engines
# =============================================================================================

def run_checks(cfg=None):
    """Run every check and return the ordered list of (name, status, detail) rows."""
    cfg = cfg if cfg is not None else load_config()
    return [
        check_python_version(),
        check_in_bs_env(),
        check_worker_import("PIL", "PIL (Pillow)"),
        check_worker_import("numpy"),
        check_worker_import("googleapiclient", "googleapiclient (google-api-python-client)"),
        check_ai_import("torch"),
        check_ai_import("transnetv2_pytorch"),
        check_ai_import("easyocr"),
        check_tkinter(),
        check_pyside6(),
        check_ffbinary(cfg, "ffmpeg", "ffmpeg", "ffmpeg"),
        check_ffbinary(cfg, "ffprobe", "ffprobe", "ffprobe"),
        check_config_exists(),
        check_worker_python(cfg),
        check_transnet_python(cfg),
        check_google_client_secret(cfg),
    ]


def format_table(rows):
    """Plain ASCII, aligned columns: 'STATUS  name  detail'. Pure string formatting, no I/O."""
    name_w = max((len(r[0]) for r in rows), default=4)
    lines = []
    for name, status, detail in rows:
        line = f"{status:<5} {name:<{name_w}}"
        if detail:
            line += f"  {detail}"
        lines.append(line)
    return "\n".join(lines)


def exit_code_for(rows):
    return 1 if any(status == FAIL for _name, status, _detail in rows) else 0


def main():
    rows = run_checks()
    print("Breakdown Studio doctor")
    print("=" * 70)
    print(format_table(rows))
    print("=" * 70)
    code = exit_code_for(rows)
    if code:
        print("One or more checks FAILED: see details above.")
    else:
        print("All checks passed (WARN/INFO rows are optional features, not problems).")
    return code


if __name__ == "__main__":
    sys.exit(main())
