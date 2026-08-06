# STATE, breakdown-studio

**Read this first.** Last updated 2026-08-06 by the `windows` seat.

## Status: SHIPPED and public

Repo public, v1.0.0 Windows release published, landing page live at
https://thevfxsupervisor.com/breakdown-studio/ (verified 200 including both logo assets).
Engineering is complete; the remaining work is launch and distribution, most of it Geoff-gated.

## Landed 2026-08-05

- **Brand alignment.** The page had drifted: old orange accent, old sage green, placeholder logo, no
  favicon, and no link back to the main site at all. Now on the shared palette with the aperture
  logo, favicon, and backlinks in the header and a new footer row.
- **Leak fixed.** The demo strip and the cut-matching mock were built from real production slates
  with their real takes, timecodes and notes. Replaced with a synthetic `9xxx` range. See CLAUDE.md.
  **Git history still contains the old values** and this repo is public; a history rewrite is Geoff's
  call and has not been done.

## Open items

1. **Decide on the git-history question above.** Removing the data from the live page reduced
   exposure but did not un-publish it.
2. **The `gh-pages` branch is stale and vestigial.** Pages deploys from the Actions workflow on
   `main`. The branch now holds hand-pushed commits that are not what is served. Worth deleting to
   remove the trap, but it is a shared public branch so confirm before doing it.
3. Non-Windows platforms are unproven for the packaged build.
4. Distribution is the real gap, not engineering.

## Do not re-derive

The long history of this project (sprints, test counts, template scrubbing, launch mechanics) is
recorded in the `windows` seat's memory store under `project_breakdown_studio`. It is ~330 lines of
mostly historical sediment; treat it as an archive, not as current state. This file is current state.
