---
name: qc-boundaries
description: Find and stage fixes for shot-boundary errors (detector over-splits and missed cuts) using the slate as an oracle. Dry-run by default.
---

# qc-boundaries

The shot detector both **over-splits** (one shot broken in two on fast motion) and **misses cuts**
(two shots merged into one). Use the slate burn-in as a second, independent oracle to catch both,
then stage merges/splits for review. Dry-run by default; apply only on explicit confirmation.

## Procedure

1. **Read the slate per detected shot** (top-left burn-in). The slate changes at every real
   editorial cut, so it is an independent boundary signal that needs no second detector.
2. **Flag over-splits:** consecutive detected shots with the **same slate + take + continuous
   action** are likely one shot the detector broke. Propose a **merge**.
3. **Flag missed cuts:** within one detected "shot", the start / mid / end frames disagree (high
   perceptual-hash distance) or the slate changes mid-shot. Propose a **split** at the boundary.
4. **Also surface** duration outliers (a single very long "shot" is usually two merged) and
   ultra-short shots (possible false split). The app's `qc` stage writes these to `qc_flags.csv`.
5. **Stage** the proposed merges/splits as a review list; do not modify the cut list yet.

## Applying (only after operator confirms)

- Use the repair tools **dry-run first** (`merge_shots`, `split_shot`), then apply, then
  `rethumb_shot` to regenerate frames/thumbs for the changed shots.
- Frame accuracy: stills are pulled at `(k - 0.5) / fps` via output-seek; a merge/split changes
  which frames are start/mid/end, so always re-thumb after.

## Finding merges the operator hasn't named (autonomous)

Some missed cuts hide behind a postviz/placeholder slate the slate-oracle can't read. To find them:

1. **Shortlist by divergence:** aHash the start / mid / end frames; a shot spanning a cut diverges hard.
   But divergence alone **over-flags** swish-pans, set-extension plates, and long lock-offs: it is only
   a filter.
2. **Confirm with a slate change:** OCR the slate on the start frame and the end frame. A real merge
   **changes slate** mid-shot (or goes placeholder to real). Same slate start-to-end = one dynamic shot.
3. **Respect transition notes:** `SWISH PAN` / `TRACK WIPE` / `MORPH CUT` / `HIDDEN SPLIT` mark
   **intentional** transition/join VFX: the transition is the work, so keep it as ONE shot.
4. **Record the decision either way** in the sheet (a checked-not-split note) so it is not
   re-investigated next cut. Split only what a contact strip confirms.

## Hard rules

- Dry-run by default; never edit the cut list without an explicit apply step.
- The burn-in **timecode** is continuous across cuts and is **useless** as a boundary signal: use
  the **slate**, not the TC, as the oracle.
- A start-to-end **slate change** is the reliable merge signal; frame divergence and placeholder slates
  over-flag. Transition-note shots are single shots, not merges.
