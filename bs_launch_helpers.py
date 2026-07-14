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
"""
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
