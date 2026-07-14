---
name: match-cut
description: Match a new cut's shots to the master breakdown with a unique 1:1 assignment, staged for operator review. Never writes to the master.
---

# match-cut

Prepare a review-ready proposal matching this cut's shots to the master breakdown. This skill
**stages** a proposal; it never applies anything to the master. Applying is a separate, gated step
(see `/update-master`).

## Procedure

1. **Load both sides.** The new cut's shot list (with official codes + slates where OCR found them)
   and the master `Shots_Breakdown`. Resolve columns **by header name**, not letter.
2. **Assign 1:1, in priority order** (a master code is unique; use it at most once):
   1. Exact code (new shot's own code == a master code).
   2. Ordinal within a (slate, take) group, paired in **counter order** (absorbs drift; never
      reorder a slate group by picture).
   3. Slate / take-relaxed.
   4. Visual (CLIP): **only for slate-less shots**; a slate match always beats a visual one.
   Leftovers on the new side = NEW; leftovers on the master side = candidate omits.
3. **Protect the untouchables.** Before proposing any omit, exclude every master shot flagged as
   manually added (invisible-VFX: DMP, screen-replace, clean-up, transitions, etc.). These never
   match by slate or picture and must **never be auto-omitted**. See memory `protected-shots`.
4. **Stage into the cut sheet** (not the master): a match-tier column, a proposed-master-code
   column, a note column, and (optional) side-by-side thumbnails. Put a **conditional-format rule**
   on the proposed-code column so duplicates turn red. The column must be fully unique before you
   report done.
5. **Report** counts: confident code/ordinal matches, visual matches, take-relaxed, weak, NEW,
   genuine omits (after protection). Call out every red (duplicate) cell and every weak match for
   the operator.

## Hard rules

- Never write to the master in this skill.
- 1:1 only: never let two shots claim one code (per-shot argmax is banned).
- Slate beats visual, always. Visual is slate-less-only.
- Dry-run mindset: you are producing a proposal for a human to confirm, not a decision.
