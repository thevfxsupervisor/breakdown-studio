# CLAUDE.md, breakdown-studio

Guidance for Claude Code working in this repo. Read `STATE.md` first for current status.

Fleet-wide rules come from the global `C:\Users\geoff\.claude\CLAUDE.md`. Do not restate them here.

## What this is

Breakdown Studio: an open-source (MIT) shot-breakdown pipeline for VFX supervisors and producers.
Detects every shot in an edit, reads burned-in slates and VFX notes, thumbnails into a live Google
Sheet, and re-matches each new cut back to a master 1:1. Ships as a Tkinter and a Qt GUI plus a
packaged Windows build. It grew out of a real production pipeline and was genericised for release.

- Repo: https://github.com/thevfxsupervisor/breakdown-studio (**PUBLIC**)
- Landing page: https://thevfxsupervisor.com/breakdown-studio/
- v1.0.0 Windows release is published.

## PUBLIC repo: the leak-gate is the first rule

Everything here is world-readable including git history. No client or show names, no real sheet IDs,
no real shot codes, no personal paths. The generic shot prefix is `SHW_`, never the client's.

**Sample data must be synthetic and must stay synthetic.** On 2026-08-05 the landing page was found
to be using **real production slates** (0884, 0885, 0519, 0932, 0940, 0958, 0053) paired with their
real takes, timecodes and VFX notes. They were replaced with a `9xxx` range, which cannot collide
because the real show's slates are all `0xxx`. The data block in `site/index.html` carries a comment
saying so. Never paste real breakdown rows in here, not even as a convincing demo.

## The landing page and how it actually deploys

Source of truth is **`site/index.html`** (one self-contained page) plus `site/assets/`.

**Deploy = edit `site/`, commit, push to `main`.** A GitHub Actions workflow
(`.github/workflows/pages.yml`) uploads `site/` as the Pages artifact. Pages is set to
`build_type: workflow`.

**The `gh-pages` branch is VESTIGIAL.** The Pages config still lists it as `source.branch`, which is
misleading leftover from the original setup, and an old memory note claims it is the deploy target.
It is not. Pushing to `gh-pages` does nothing useful; the workflow on `main` is what publishes.
Verify with `gh run list` after a push, and confirm the served page rather than the branch.

## Brand tokens (must match the main brand site)

Defined in the `:root` block of `site/index.html`. Keep in sync with
`thevfxsupervisor.github.io/static/style.css`:

- Accent `--amber: #0585f8` (blue despite the name, sampled from the logo's blue blades), hover
  `--amber-soft: #3aa3f5`, status `--green: #5ad3c0`, ground `--bg: #131619`.
- Button ink on the accent is dark; light ink fails contrast.
- `site/assets/logo-icon.png` is the aperture mark, `site/assets/favicon.png` the simplified version
  for small sizes. Both are copies of the main site's; update together.

The page links back to the brand site from the header (Case study, Course, tinted with `.nav-out`)
and a `.footlinks` row in the footer. Keep those working; they are how the two properties connect.

## The application

- Stdlib-first. The GUIs orchestrate the CLI tools as subprocesses.
- `bs_launcher.py` multiplexes subcommands (`worker`, `ocr`, `gsheets`, `repair`, `match`, `contact`,
  `template`, `doctor`). Frozen builds self-dispatch through it, so any new module used via
  `importlib` must be added to `HIDDEN_IMPORTS` in the PyInstaller spec or the exe breaks.
- `config.json` holds all paths and interpreters and is **gitignored** (it contains Geoff's real
  paths). `config.example.json` is the shipped template.
- Tests: 498 across 9 suites, stdlib only. Run them before any release.
- `ai_kit/` is the differentiator: a generic project brief plus judgment skills for a briefed AI
  assistant. It ships with `SHW_` codes and no client data.

## Gotchas that have already cost time

- **Never copy an internal `.gitignore` into this repo.** The internal one blanket-ignores `*.txt`
  and `*.csv`, which silently dropped the `requirements-*.txt` files from tracking.
- Pushing may pick the wrong GitHub account from a cached Windows credential. Use
  `git -c credential.helper='!gh auth git-credential' push`.
- Git Bash `tar` ignores `-a`; use `/c/Windows/System32/tar.exe` for a real zip.
