#!/usr/bin/env python
"""bs_launcher.py - single entry point for Breakdown Studio, source or frozen.

This is what a packaged build's exe runs. It multiplexes between the GUI and the
various CLI tools that used to be separate "python script.py" invocations:

  breakdown-studio                launch the Qt GUI (falls back to Tkinter if
                                   PySide6 isn't bundled)
  breakdown-studio worker   ...    -> bs_worker.main()      (frames/thumbs/cuts/refclips/qc)
  breakdown-studio ocr      ...    -> bs_ocr.main()         (slate/notes/tcoffset/boundaryqc)
  breakdown-studio gsheets  ...    -> bs_gsheets.main()     (whoami/copy-template/build)
  breakdown-studio repair   ...    -> bs_repair.main()      (split/merge/rethumb/apply-ledger)
  breakdown-studio match    ...    -> bs_match.main()       (compare/assign/audit/fpscheck)
  breakdown-studio contact  ...    -> contact_sheet.main()  (8K contact sheet)
  breakdown-studio template ...    -> make_blank_template.main()  (blank template from master)
  breakdown-studio fetch    ...    -> bs_fetch.main()       (download a video URL to a local file)
  breakdown-studio enrich   ...    -> bs_enrich.main()      (transcribe/describe; optional module)
  breakdown-studio tk               launch the Tkinter GUI explicitly

Not included here: TransNetV2 detection (needs torch, a separate interpreter always) and
EasyOCR-backed OCR stages (bs_ocr degrades to a friendly "pip install easyocr" message when
the package isn't present). Both stay optional pip/venv extras layered on top of a frozen
install; see packaging/README.md.

bs_enrich.py is developed alongside this launcher and may be briefly absent from a checkout;
the "enrich" subcommand imports it lazily (see _dispatch) and prints a friendly error instead
of crashing the whole launcher if the module or its deps (faster-whisper / Ollama) aren't
there yet.

Why this exists: a PyInstaller build has no "python" on the target machine, so the GUIs
can no longer shell out to "<python> bs_worker.py ...". Instead the frozen app shells out
to itself: [sys.executable, "worker", ...]. See bs_launch_helpers.worker_argv() for the
one shared helper both GUIs call when building a stage command.

Each subcommand imports the corresponding module and calls its existing main(), forwarding
whatever argv followed the subcommand. The modules are unmodified: they already read
sys.argv via argparse, so trimming argv down to "just this subcommand's args" before
calling main() is enough to reuse them as-is.

Run from source exactly like before, just via this file instead of the individual
scripts:
    python bs_launcher.py                     # GUI
    python bs_launcher.py worker frames ...    # same args bs_worker.py frames took
"""
import sys

SUBCOMMANDS = {
    "worker": ("bs_worker", "main"),
    "ocr": ("bs_ocr", "main"),
    "gsheets": ("bs_gsheets", "main"),
    "repair": ("bs_repair", "main"),
    "match": ("bs_match", "main"),
    "contact": ("contact_sheet", "main"),
    "template": ("make_blank_template", "main"),
    "fetch": ("bs_fetch", "main"),
    "enrich": ("bs_enrich", "main"),
    "miro": ("bs_miro", "main"),
}


def _dispatch(name, argv):
    """Import module `name` and call its main(), with sys.argv trimmed to [prog, *argv]
    so the module's own argparse.parse_args() (which reads sys.argv[1:]) sees exactly the
    subcommand's arguments, not "worker" itself or anything before it."""
    import importlib
    mod_name, fn_name = SUBCOMMANDS[name]
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        if mod_name == "bs_enrich":
            # bs_enrich.py is built concurrently with this launcher and may be briefly
            # absent from a checkout; fail this one subcommand, not the whole launcher.
            print(f"bs_launcher: 'enrich' is unavailable ({e}).\n"
                  f"bs_enrich.py may not be present in this checkout yet, or a dependency "
                  f"(faster-whisper / Ollama client) is missing.", file=sys.stderr)
            return 1
        raise
    fn = getattr(mod, fn_name)
    old_argv = sys.argv
    sys.argv = [f"{old_argv[0]} {name}"] + argv
    try:
        return fn()
    finally:
        sys.argv = old_argv


def _launch_qt():
    try:
        import breakdown_studio_qt
    except ImportError as e:
        print(f"Qt GUI unavailable ({e}); falling back to the Tkinter GUI.", file=sys.stderr)
        return _launch_tk()
    return breakdown_studio_qt.main()


def _launch_tk():
    import breakdown_studio
    return breakdown_studio.App().mainloop()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if not argv:
        return _launch_qt()

    cmd, rest = argv[0], argv[1:]

    if cmd == "tk":
        return _launch_tk()
    if cmd in ("gui", "qt"):
        return _launch_qt()
    if cmd in SUBCOMMANDS:
        return _dispatch(cmd, rest)
    if cmd in ("-h", "--help"):
        print(__doc__)
        return 0

    print(f"bs_launcher: unknown subcommand '{cmd}'.\n", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
