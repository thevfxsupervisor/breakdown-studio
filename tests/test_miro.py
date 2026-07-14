#!/usr/bin/env python
"""bs_miro tests: the Miro shot-wall stage is registered in both GUIs, bs_miro exposes the
push/resync/verify/cluster subcommands with the right flags, its pure helpers work, and CLIP
is optional (importing bs_miro must not need torch). No network / no real Miro API is touched.

Run:  python tests/test_miro.py    (from the breakdown_studio folder)
"""
import json
import os
import subprocess
import sys

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


# --- 1. bs_miro imports WITHOUT torch (CLIP is optional) ----------------------------------------
import bs_miro  # noqa: E402
check("bs_miro imports without torch/transformers", True)
check("bs_miro has push/resync/verify/cluster commands",
      all(hasattr(bs_miro, f"cmd_{c}") for c in ("push", "resync", "verify", "cluster")))

# --- 2. pure helpers -----------------------------------------------------------------------------
check("_tc_to_frames parses HH:MM:SS:FF", bs_miro._tc_to_frames("01:00:41:13", 24.0) == ((1 * 60) * 60 + 41) * 24 + 13)
check("_tc_to_frames blank -> None", bs_miro._tc_to_frames("") is None)
check("_extract_url pulls URL from =IMAGE()",
      bs_miro._extract_url('=IMAGE("https://drive.google.com/uc?id=ABC")') == "https://drive.google.com/uc?id=ABC")
check("_extract_url on raw url", bs_miro._extract_url("https://x/y.jpg") == "https://x/y.jpg")
check("colour_for VFX default", bs_miro.colour_for("VFX", bs_miro.DEFAULT_COLORS) == "#D6E4FF")
check("colour_for Online default", bs_miro.colour_for("Online", bs_miro.DEFAULT_COLORS) == "#FFE0B2")
check("colour_for unknown -> fallback", bs_miro.colour_for("Zzz", bs_miro.DEFAULT_COLORS) == bs_miro.FALLBACK_COLOR)
cx, cy = bs_miro.cell_xy(0)
check("cell_xy order 0 is grid origin", (cx, cy) == (bs_miro.OX, bs_miro.OY))
check("cell_xy wraps at COLS", bs_miro.cell_xy(bs_miro.COLS)[1] == bs_miro.OY + bs_miro.PITCH_Y)
check("resolve_token passes through a literal token", bs_miro.resolve_token("abc123") == "abc123")

# --- 3. argparse: subcommands accept the flags the GUI passes ------------------------------------
def help_of(*argv):
    r = subprocess.run([sys.executable, os.path.join(HERE, "bs_miro.py"), *argv, "--help"],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

rc, out = help_of("push")
check("bs_miro push --help exits 0", rc == 0, out[:120])
for flag in ("--spreadsheet-id", "--tab", "--header-row", "--miro-token", "--miro-board",
             "--state", "--only-status", "--status-suffix", "--type-colors"):
    check(f"push accepts {flag}", flag in out)
rc, out = help_of("resync")
check("resync accepts --no-append", "--no-append" in out and rc == 0)
rc, out = help_of("verify")
check("verify --help exits 0", rc == 0)
rc, out = help_of("cluster")
check("cluster accepts --sim-threshold + --cluster-exclude",
      "--sim-threshold" in out and "--cluster-exclude" in out)

# --- 4. GUIs register the miro stage (guard tkinter/qt absence) ----------------------------------
try:
    import breakdown_studio as tk
    sids = [s[0] for s in tk.STAGES]
    check("tk STAGES includes 'miro'", "miro" in sids, sids)
    check("'miro' after 'buildsheet'", sids.index("miro") > sids.index("buildsheet"))
    mrow = next(s for s in tk.STAGES if s[0] == "miro")
    check("'miro' stage does not need the movie", mrow[2] is False, mrow)
except ImportError as e:
    SKIP += 1
    print(f"  skip tk STAGES checks (tkinter unavailable: {e})")

try:
    import breakdown_studio_qt as qt
    qsids = [s[0] for s in qt.STAGES]
    check("qt STAGES includes 'miro'", "miro" in qsids, qsids)
except ImportError as e:
    SKIP += 1
    print(f"  skip qt STAGES checks (PySide6 unavailable: {e})")

# --- 5. launcher + config wiring ----------------------------------------------------------------
import bs_launcher  # noqa: E402
import bs_launch_helpers  # noqa: E402
check("bs_launcher SUBCOMMANDS has 'miro'", bs_launcher.SUBCOMMANDS.get("miro") == ("bs_miro", "main"))
check("worker_argv maps bs_miro -> 'miro'", bs_launch_helpers._SUBCOMMAND_FOR_SCRIPT.get("bs_miro") == "miro")
cfg = json.load(open(os.path.join(HERE, "config.example.json"), encoding="utf-8"))
check("config.example.json has miro_token", "miro_token" in cfg)
check("config.example.json has miro_board", "miro_board" in cfg)

tail = f" ({SKIP} skipped)" if SKIP else ""
print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed{tail}")
sys.exit(1 if FAIL else 0)
