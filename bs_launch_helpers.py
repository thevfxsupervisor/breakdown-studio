#!/usr/bin/env python
"""bs_launch_helpers.py - one shared helper for building stage subprocess commands.

Both GUIs (breakdown_studio.py Tkinter, breakdown_studio_qt.py Qt) shell out to worker
scripts as separate processes. Running from source that's "<python> bs_worker.py <args>".
A packaged (PyInstaller) build has no "python" on the target machine at all, so it has to
shell out to itself instead: [sys.executable, "worker", <args>] via bs_launcher.py's
subcommand dispatch.

worker_argv() is that one decision point, used by both GUIs everywhere they used to write
"[worker_python, str(script_path), *args]" directly. It keeps the frozen/non-frozen branch
in exactly one place instead of duplicated per call site.

Also home to the GUI preflight self-heal helpers (UX_PLAN.md P0.3): adopt_bs_env() picks up
the installer's default worker venv when config is blank/stale, and resolve_detect_interpreter()
decides which interpreter (if any) should run the Detect stage, falling back from a dedicated
transnet_python env to worker_python (single-venv installs) before giving up and letting the
caller skip Detect instead of failing the whole run.
"""
import subprocess
import sys
from pathlib import Path

# subcommand name bs_launcher.py registers for each worker script, keyed by the script's
# filename (without .py) as the source build invokes it.
_SUBCOMMAND_FOR_SCRIPT = {
    "bs_worker": "worker",
    "bs_ocr": "ocr",
    "bs_gsheets": "gsheets",
    "bs_repair": "repair",
    "bs_match": "match",
    "contact_sheet": "contact",
    "make_blank_template": "template",
    "bs_fetch": "fetch",
    "bs_enrich": "enrich",
    "bs_miro": "miro",
}


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def worker_argv(script_path, args, worker_python=None):
    """Build the argv list to run `script_path` (a bs_*.py under scripts_dir) with `args`.

    Frozen:      [sys.executable, "<subcommand>", *args]   (self-dispatch, no python needed)
    Not frozen:  [worker_python or sys.executable, str(script_path), *args]

    `script_path` may be a Path or str; only its stem is used to pick the frozen
    subcommand, so callers can keep passing the same "scripts_dir / 'bs_worker.py'"
    expressions they used before.
    """
    stem = Path(script_path).stem
    if is_frozen():
        subcmd = _SUBCOMMAND_FOR_SCRIPT.get(stem)
        if subcmd is None:
            raise ValueError(f"no frozen subcommand registered for '{stem}'")
        return [sys.executable, subcmd, *args]
    return [worker_python or sys.executable, str(script_path), *args]


def doctor_argv(worker_python=None):
    """Build the argv for the Settings 'Run doctor' button: 'bs_launcher.py doctor', run under
    worker_python when running from source, or the frozen build's self-dispatch otherwise
    (matches worker_argv()'s frozen/non-frozen split, just for the launcher itself rather
    than one of the bs_*.py worker scripts)."""
    if is_frozen():
        return [sys.executable, "doctor"]
    app_dir = Path(__file__).resolve().parent
    return [worker_python or sys.executable, str(app_dir / "bs_launcher.py"), "doctor"]


def bs_env_candidate(app_dir):
    """Path the installer's default worker venv would live at, for this platform. Does not
    check existence -- callers do that (see adopt_bs_env)."""
    app_dir = Path(app_dir)
    if sys.platform.startswith("win"):
        return app_dir / "bs_env" / "Scripts" / "python.exe"
    return app_dir / "bs_env" / "bin" / "python"


def adopt_bs_env(cfg, app_dir):
    """GUI preflight self-heal (UX_PLAN.md P0.3): if cfg['worker_python'] is blank or points
    to a file that no longer exists, and the installer's bs_env venv exists next to the app,
    silently adopt it into cfg. Returns True (and mutates cfg in place) if adopted, else False
    (cfg untouched). Caller is responsible for saving config and logging the one-line notice --
    kept out of this function so it stays a plain, easily unit-tested decision."""
    worker = (cfg.get("worker_python") or "").strip()
    if worker and Path(worker).exists():
        return False
    candidate = bs_env_candidate(app_dir)
    if candidate.exists():
        cfg["worker_python"] = str(candidate)
        return True
    return False


def resolve_detect_interpreter(cfg, probe_fn):
    """Decide which interpreter (if any) should run the Detect (TransNetV2) stage.

    Decision order:
      1. cfg['transnet_python'] is set and the file exists -> use it (the classic dedicated
         torch env from the two-venv install).
      2. otherwise, cfg['worker_python'] is set, the file exists, and probe_fn(worker_python)
         reports it can import transnetv2_pytorch -> use worker_python (single-venv install;
         UX_PLAN.md P1 has the installer's "AI features" question install everything,
         including transnetv2-pytorch, into the same bs_env).
      3. otherwise -> None: the caller should SKIP the Detect stage (log a clear line) rather
         than fail the whole pipeline run.

    probe_fn is injected rather than hardcoded to a subprocess call so this stays a pure
    decision function for unit tests; real callers pass a probe built on probe_can_import().
    """
    transnet = (cfg.get("transnet_python") or "").strip()
    if transnet and Path(transnet).exists():
        return transnet
    worker = (cfg.get("worker_python") or "").strip()
    if worker and Path(worker).exists() and probe_fn(worker):
        return worker
    return None


def probe_can_import(python_path, module_name, timeout=8):
    """Best-effort: can '<python_path> -c "import <module_name>"' succeed? Never raises --
    any failure (missing interpreter, import error, timeout) just resolves to False. Used to
    probe whether worker_python already has transnetv2_pytorch (single-venv install) before
    resolve_detect_interpreter() decides to skip the Detect stage."""
    try:
        r = subprocess.run([python_path, "-c", f"import {module_name}"],
                           capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False
