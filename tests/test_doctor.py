#!/usr/bin/env python
"""bs_doctor / launch-helper pure-logic tests - stdlib only, no real subprocess/ffmpeg/OCR
engines required (checks that DO shell out, like check_ffbinary, are exercised with a fake
run_fn injected instead of calling the real ffmpeg binary).

Run:  python tests/test_doctor.py       (from the breakdown_studio folder)
Covers: doctor's row-building checks, format_table/exit_code_for, and the
bs_launch_helpers preflight self-heal functions (adopt_bs_env, resolve_detect_interpreter,
doctor_argv) that back UX_PLAN.md P0.3.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bs_doctor as D  # noqa: E402
import bs_launch_helpers as H  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


# =============================================================================================
# format_table / exit_code_for -- pure string/aggregation logic
# =============================================================================================

rows = [("Python version", D.PASS, "3.10.13"), ("GUI: PySide6", D.INFO, "installed"),
        ("config: transnet_python", D.WARN, "blank")]
table = D.format_table(rows)
check("format_table: one line per row", len(table.splitlines()) == 3, table)
check("format_table: status column present", all(r[1] in table for r in rows))
check("format_table: name column present", all(r[0] in table for r in rows))
check("format_table: detail shown when non-empty", "3.10.13" in table and "installed" in table)
check("format_table: columns aligned (status is fixed width)",
      table.splitlines()[0][:5] == f"{D.PASS:<5}", table.splitlines()[0])

no_detail_rows = [("x", D.PASS, ""), ("yy", D.WARN, "")]
table2 = D.format_table(no_detail_rows)
check("format_table: blank detail -> no trailing double-space garbage",
      not table2.splitlines()[0].endswith("  "), repr(table2.splitlines()[0]))

check("exit_code_for: no FAIL -> 0",
      D.exit_code_for([("a", D.PASS, ""), ("b", D.WARN, ""), ("c", D.INFO, "")]) == 0)
check("exit_code_for: one FAIL -> 1",
      D.exit_code_for([("a", D.PASS, ""), ("b", D.FAIL, "")]) == 1)
check("exit_code_for: empty rows -> 0", D.exit_code_for([]) == 0)


# =============================================================================================
# check_python_version -- injectable version_info, no need to run under an old interpreter
# =============================================================================================

name, status, detail = D.check_python_version((3, 9, 0))
check("check_python_version: 3.9.0 -> PASS", status == D.PASS, (status, detail))
name, status, detail = D.check_python_version((3, 12, 4))
check("check_python_version: 3.12.4 -> PASS", status == D.PASS, (status, detail))
name, status, detail = D.check_python_version((3, 8, 10))
check("check_python_version: 3.8.10 -> FAIL", status == D.FAIL, (status, detail))
name, status, detail = D.check_python_version((2, 7, 18))
check("check_python_version: 2.7.18 -> FAIL", status == D.FAIL, (status, detail))


# =============================================================================================
# check_in_bs_env -- informational only, never WARN/FAIL
# =============================================================================================

_, status, detail = D.check_in_bs_env(r"C:\app\bs_env\Scripts\python.exe")
check("check_in_bs_env: path containing bs_env -> INFO 'yes'",
      status == D.INFO and "yes" in detail, detail)
_, status, detail = D.check_in_bs_env(r"C:\Python310\python.exe")
check("check_in_bs_env: unrelated path -> INFO 'no'",
      status == D.INFO and "no" in detail, detail)


# =============================================================================================
# check_worker_import / check_ai_import -- real _try_import, but only against stdlib / a
# guaranteed-absent fake module name, so no optional dependency needs to be installed
# =============================================================================================

_, status, _ = D.check_worker_import("os")
check("check_worker_import: stdlib module -> PASS", status == D.PASS)
_, status, detail = D.check_worker_import("definitely_not_a_real_module_bsdoctor")
check("check_worker_import: missing module -> FAIL", status == D.FAIL, detail)
check("check_worker_import: FAIL names requirements-worker.txt",
      "requirements-worker.txt" in detail, detail)

_, status, _ = D.check_ai_import("os")
check("check_ai_import: stdlib module -> PASS", status == D.PASS)
_, status, detail = D.check_ai_import("definitely_not_a_real_module_bsdoctor")
check("check_ai_import: missing module -> WARN (not FAIL)", status == D.WARN, detail)
check("check_ai_import: WARN mentions re-run installer and answer Y",
      "re-run installer and answer Y" in detail, detail)


# =============================================================================================
# check_tkinter / check_pyside6
# =============================================================================================

_, status, _ = D.check_tkinter()
check("check_tkinter: status is PASS or WARN (never FAIL)", status in (D.PASS, D.WARN), status)
_, status, _ = D.check_pyside6()
check("check_pyside6: always informational", status == D.INFO, status)


# =============================================================================================
# check_ffbinary -- inject a fake run_fn so no real ffmpeg binary is needed
# =============================================================================================

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


with tempfile.TemporaryDirectory() as td:
    fake_ffmpeg = Path(td) / "ffmpeg.exe"
    fake_ffmpeg.write_text("not a real binary", encoding="utf-8")

    def fake_run_ok(argv, **kw):
        return _FakeCompleted(0, stdout="ffmpeg version 6.0-fake Copyright (c) fake\nmore text\n")

    name, status, detail = D.check_ffbinary({"ffmpeg": str(fake_ffmpeg)}, "ffmpeg", "ffmpeg",
                                            "ffmpeg", run_fn=fake_run_ok)
    check("check_ffbinary: config path + successful -version -> PASS", status == D.PASS, detail)
    check("check_ffbinary: PASS detail is the first output line",
          detail == "ffmpeg version 6.0-fake Copyright (c) fake", detail)

    def fake_run_fail(argv, **kw):
        return _FakeCompleted(1, stdout="", stderr="error: no such filter\n")

    name, status, detail = D.check_ffbinary({"ffmpeg": str(fake_ffmpeg)}, "ffmpeg", "ffmpeg",
                                            "ffmpeg", run_fn=fake_run_fail)
    check("check_ffbinary: non-zero exit -> FAIL", status == D.FAIL, detail)

    def fake_run_raises(argv, **kw):
        raise OSError("boom")

    name, status, detail = D.check_ffbinary({"ffmpeg": str(fake_ffmpeg)}, "ffmpeg", "ffmpeg",
                                            "ffmpeg", run_fn=fake_run_raises)
    check("check_ffbinary: run_fn raising -> FAIL, not an exception", status == D.FAIL, detail)

# neither config nor PATH has the binary -> FAIL, and no subprocess is ever invoked
name, status, detail = D.check_ffbinary({"ffmpeg": ""}, "ffmpeg",
                                        "definitely_not_a_real_binary_bsdoctor", "ffmpeg")
check("check_ffbinary: nothing resolves -> FAIL without shelling out", status == D.FAIL, detail)


# =============================================================================================
# config.json checks -- worker_python / transnet_python / google_client_secret
# =============================================================================================

check("check_worker_python: blank -> FAIL", D.check_worker_python({})[1] == D.FAIL)
check("check_worker_python: set but missing file -> FAIL",
      D.check_worker_python({"worker_python": r"C:\nope\python.exe"})[1] == D.FAIL)
with tempfile.NamedTemporaryFile(delete=False) as tf:
    real_path = tf.name
check("check_worker_python: set + file exists -> PASS",
      D.check_worker_python({"worker_python": real_path})[1] == D.PASS)

check("check_transnet_python: blank -> WARN (not FAIL)",
      D.check_transnet_python({})[1] == D.WARN)
check("check_transnet_python: set but missing file -> WARN",
      D.check_transnet_python({"transnet_python": r"C:\nope\python.exe"})[1] == D.WARN)
check("check_transnet_python: set + file exists -> PASS",
      D.check_transnet_python({"transnet_python": real_path})[1] == D.PASS)

_, status, detail = D.check_google_client_secret({})
check("check_google_client_secret: blank -> WARN", status == D.WARN, detail)
check("check_google_client_secret: blank WARN mentions SETUP_GOOGLE.md",
      "SETUP_GOOGLE.md" in detail, detail)
check("check_google_client_secret: set + file exists -> PASS",
      D.check_google_client_secret({"google_client_secret": real_path})[1] == D.PASS)
os.unlink(real_path)

check("check_config_exists: missing path -> FAIL",
      D.check_config_exists(r"C:\nope\config.json")[1] == D.FAIL)
with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
    tf.write(b"{}")
    cfg_path = tf.name
check("check_config_exists: existing path -> PASS", D.check_config_exists(cfg_path)[1] == D.PASS)
os.unlink(cfg_path)


# =============================================================================================
# run_checks -- assembles the full row list, but with a fully injectable cfg; ffbinary rows
# will resolve however this test box happens to (real ffmpeg presence varies), so only check
# the STRUCTURE (row count, no crash, exit_code_for works on the result), not exact statuses.
# =============================================================================================

all_rows = D.run_checks(cfg={})
check("run_checks: returns 16 rows", len(all_rows) == 16, len(all_rows))
check("run_checks: every row is a 3-tuple", all(len(r) == 3 for r in all_rows))
check("run_checks: every status is one of PASS/WARN/FAIL/INFO",
      all(r[1] in (D.PASS, D.WARN, D.FAIL, D.INFO) for r in all_rows),
      [r for r in all_rows if r[1] not in (D.PASS, D.WARN, D.FAIL, D.INFO)])
expected_names = [
    "Python version", "Running inside bs_env",
    "worker import: PIL (Pillow)", "worker import: numpy",
    "worker import: googleapiclient (google-api-python-client)",
    "AI extra: torch", "AI extra: transnetv2_pytorch", "AI extra: easyocr",
    "GUI: tkinter", "GUI: PySide6", "ffmpeg", "ffprobe", "config.json",
    "config: worker_python", "config: transnet_python", "config: google_client_secret",
]
check("run_checks: row names/order match the documented table",
      [r[0] for r in all_rows] == expected_names, [r[0] for r in all_rows])
check("exit_code_for on run_checks output does not crash",
      D.exit_code_for(all_rows) in (0, 1))


# =============================================================================================
# bs_launch_helpers: resolve_detect_interpreter -- decision order for the Detect self-heal
# (UX_PLAN.md P0.3): transnet_python (if it exists) wins; else worker_python IF it can import
# transnetv2_pytorch; else None (caller skips Detect instead of failing the run).
# =============================================================================================

with tempfile.NamedTemporaryFile(delete=False) as tf:
    transnet_path = tf.name
with tempfile.NamedTemporaryFile(delete=False) as tf:
    worker_path = tf.name

probe_calls = []


def probe_true(p):
    probe_calls.append(p)
    return True


def probe_false(p):
    probe_calls.append(p)
    return False


cfg = {"transnet_python": transnet_path, "worker_python": worker_path}
probe_calls.clear()
result = H.resolve_detect_interpreter(cfg, probe_true)
check("resolve_detect_interpreter: transnet_python set+exists -> used directly",
      result == transnet_path, result)
check("resolve_detect_interpreter: transnet_python path never probes worker_python",
      probe_calls == [], probe_calls)

cfg = {"transnet_python": "", "worker_python": worker_path}
result = H.resolve_detect_interpreter(cfg, probe_true)
check("resolve_detect_interpreter: blank transnet, worker CAN import -> worker_python used",
      result == worker_path, result)

result = H.resolve_detect_interpreter(cfg, probe_false)
check("resolve_detect_interpreter: blank transnet, worker CANNOT import -> None",
      result is None, result)

cfg = {"transnet_python": r"C:\nope\python.exe", "worker_python": worker_path}
result = H.resolve_detect_interpreter(cfg, probe_true)
check("resolve_detect_interpreter: transnet_python set but file missing -> falls back to worker",
      result == worker_path, result)

cfg = {"transnet_python": "", "worker_python": r"C:\nope\python.exe"}
result = H.resolve_detect_interpreter(cfg, probe_true)
check("resolve_detect_interpreter: worker_python file missing -> None (probe never trusted)",
      result is None, result)

cfg = {"transnet_python": "", "worker_python": ""}
result = H.resolve_detect_interpreter(cfg, probe_true)
check("resolve_detect_interpreter: both blank -> None", result is None, result)

os.unlink(transnet_path)
os.unlink(worker_path)


# =============================================================================================
# bs_launch_helpers: adopt_bs_env -- GUI preflight self-heal
# =============================================================================================

with tempfile.TemporaryDirectory() as td:
    app_dir = Path(td)
    candidate = H.bs_env_candidate(app_dir)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("fake interpreter", encoding="utf-8")

    cfg = {"worker_python": ""}
    adopted = H.adopt_bs_env(cfg, app_dir)
    check("adopt_bs_env: blank worker_python + bs_env present -> adopted",
          adopted is True and cfg["worker_python"] == str(candidate), cfg)

    cfg = {"worker_python": str(Path(td) / "nope" / "python.exe")}
    adopted = H.adopt_bs_env(cfg, app_dir)
    check("adopt_bs_env: stale worker_python (missing file) -> adopted",
          adopted is True and cfg["worker_python"] == str(candidate), cfg)

    real_worker = Path(td) / "real_python.exe"
    real_worker.write_text("real", encoding="utf-8")
    cfg = {"worker_python": str(real_worker)}
    adopted = H.adopt_bs_env(cfg, app_dir)
    check("adopt_bs_env: worker_python already valid -> not touched",
          adopted is False and cfg["worker_python"] == str(real_worker), cfg)

with tempfile.TemporaryDirectory() as td2:
    cfg = {"worker_python": ""}
    adopted = H.adopt_bs_env(cfg, td2)
    check("adopt_bs_env: no bs_env present -> not adopted", adopted is False, cfg)


# =============================================================================================
# bs_launch_helpers: doctor_argv -- frozen vs from-source argv shape
# =============================================================================================

argv = H.doctor_argv("C:/env/python.exe")
check("doctor_argv: from-source includes worker_python, bs_launcher.py, doctor",
      argv[0] == "C:/env/python.exe" and argv[-1] == "doctor"
      and "bs_launcher.py" in argv[1], argv)

argv_default = H.doctor_argv(None)
check("doctor_argv: falls back to sys.executable when worker_python is None",
      argv_default[0] == sys.executable, argv_default)

_orig_frozen = getattr(sys, "frozen", None)
sys.frozen = True
try:
    argv_frozen = H.doctor_argv("C:/env/python.exe")
finally:
    if _orig_frozen is None:
        del sys.frozen
    else:
        sys.frozen = _orig_frozen
check("doctor_argv: frozen build self-dispatches [sys.executable, 'doctor']",
      argv_frozen == [sys.executable, "doctor"], argv_frozen)


# =============================================================================================
# bs_launcher: 'doctor' subcommand registered
# =============================================================================================

import bs_launcher as L  # noqa: E402
check("bs_launcher SUBCOMMANDS includes 'doctor'", "doctor" in L.SUBCOMMANDS, L.SUBCOMMANDS)
check("bs_launcher 'doctor' dispatches to bs_doctor.main",
      L.SUBCOMMANDS.get("doctor") == ("bs_doctor", "main"), L.SUBCOMMANDS.get("doctor"))


print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
