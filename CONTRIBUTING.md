# Contributing

Thanks for your interest in Breakdown Studio!

## Dev Setup

1. Clone the repo and `cd breakdown_studio/`.
2. Install Python 3.9+.
3. Run the installer to create the two virtual environments:
   - **Windows:** `install.bat`
   - **macOS/Linux:** `bash install.sh`
   
   This creates:
   - `bs_env/` (worker): Pillow, numpy, google-auth, ffmpeg-python
   - `transnet_env/` (detection): PyTorch, TransNetV2

4. Install **ffmpeg** and **ffprobe** system-wide or point `config.json` to them.

## Running locally

1. Update `config.json` with your paths (see `config.example.json` for a template).
2. Launch the app:
   - **Windows:** double-click `Breakdown Studio.lnk` or run `python breakdown_studio.py`
   - **macOS/Linux:** `python3 breakdown_studio.py`

## Code style

- Python 3.9+ compatible
- No external dependencies beyond those in `install.sh` / `install.bat`
- GUI uses only the standard library (Tkinter)
- Worker / detection scripts may use numpy, Pillow, google-auth, torch/TransNetV2
- Comments explain *why*, not *what*; code should be readable
- All paths are **absolute** or relative to `scripts_dir`; never hard-code client paths

## Before submitting a PR

1. **No secrets:** strip all config.json, tokens, credentials, sheet IDs, and client paths.
2. **Test locally** against a sample movie file (use a short test clip, not a full feature).
3. **Check `.gitignore`:** make sure `config.json`, virtualenvs, outputs, and logs are excluded.
4. **One feature per PR**: keep the scope clear.
5. **Update README.md** if you add or change a stage, option, or output file.

## Known limitations

- Detection uses TransNetV2 only; other detectors can be added but are not yet supported.
- ShotGrid is not wired in (it needs per-org credentials); add later if needed.
- The GUI is single-threaded; stages run sequentially, not in parallel (by design, to stream logs cleanly).

## Questions?

Open an issue or start a discussion. If you're working on Breakdown Studio in a studio context, consider checking `ai_kit/` for optional judgment-helper skills.
