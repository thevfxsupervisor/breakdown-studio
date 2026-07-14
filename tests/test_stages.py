#!/usr/bin/env python
"""Stage-wiring tests: the Match-to-master GUI stage is registered and dispatches valid bs_match args.

Run:  python tests/test_stages.py    (from the breakdown_studio folder)
Guards the Tkinter import so it skips gracefully where tkinter is unavailable; the bs_match
argparse checks always run (no display needed).
"""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PASS = FAIL = SKIP = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# --- 1. GUI registers the Match stage + the master_id config it needs ---------------------------
try:
    import breakdown_studio as tk  # Tkinter GUI; module-level constants, no root created on import
    tk_stages = [s[0] for s in tk.STAGES]
    check("tk STAGES includes 'match'", "match" in tk_stages, tk_stages)
    check("'match' sits after 'buildsheet'",
          "buildsheet" in tk_stages and tk_stages.index("match") == tk_stages.index("buildsheet") + 1)
    check("tk DEFAULTS has master_id (match target)", "master_id" in tk.DEFAULTS)
    # match must not require the movie (it is a sheet-to-sheet op)
    match_row = next(s for s in tk.STAGES if s[0] == "match")
    check("'match' stage does not need the movie", match_row[2] is False, match_row)
except ImportError as e:
    SKIP += 1
    print(f"  skip tk STAGES checks (tkinter unavailable: {e})")


# --- 2. bs_match assign accepts every flag the GUI's match stage passes --------------------------
r = subprocess.run([sys.executable, os.path.join(HERE, "bs_match.py"), "assign", "--help"],
                   capture_output=True, text=True)
check("bs_match assign --help exits 0", r.returncode == 0, r.stderr[:120])
for flag in ("--new-sheet-id", "--master-sheet-id", "--write-sheet", "--new-tab",
             "--write-sheet-tab", "--tab"):
    check(f"bs_match assign accepts {flag}", flag in r.stdout)


tail = f" ({SKIP} skipped)" if SKIP else ""
print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed{tail}")
sys.exit(1 if FAIL else 0)
