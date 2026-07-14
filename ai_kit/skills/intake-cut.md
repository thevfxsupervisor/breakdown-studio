---
name: intake-cut
description: The whole "a new cut arrived" flow, end to end - detect, extract, OCR, boundary QC, build the cut sheet, match to the master, and stop at the operator gates. Orchestrates the other skills in order.
---

# intake-cut

Editorial delivered a new cut. This is the standing runbook that takes it from a movie file to a
staged, reviewable update of the master, using the app for the mechanical stages and the other
skills for the judgment stages. It never writes to the master itself; it ends at the gates.

## Procedure

1. **Stage the source on fast local storage** (per-shot extraction is IO-heavy; a synced/network
   drive makes every stage slower). Probe resolution/fps first; **run the FPS-mismatch check
   against the previous cut** before any frame-based comparison.
2. **Mechanical pass (the app):** detect shots -> frames (start/mid/end) -> thumbnails -> QC flags.
   Then the OCR stages: slate, VFX notes, show-TC offset probe.
3. **Boundary QC gate** (see `/qc-boundaries`): resolve over-splits and missed cuts BEFORE matching;
   a wrong boundary poisons every downstream identity. Apply the persistent corrections ledger from
   previous cuts first (known postviz merges recur), then review the fresh flags.
4. **Build the cut breakdown sheet** and let the operator do their pass: confirm VFX shots (OCR note
   or their own flag - a row with neither is not a shot), adjust slates the OCR got wrong.
   **Gate 1: operator signs off the breakdown.**
5. **Match to the master** (see `/match-cut`): the tiered 1:1 assignment, uniqueness-guarded,
   staged into the review columns. Cross-check unmatched shots against the master code column
   before trusting a NEW. **Gate 2: operator approves matches, news, and omit candidates.**
6. **Reconcile notes** (see `/reconcile-notes`) and stage the master update (see `/update-master`)
   only after Gate 2. Duration deltas flow into the negotiation view (see `/budget-deltas`).
7. **Persist every operator decision** made along the way in the sheet, keyed on slate identity,
   so the NEXT cut only surfaces deltas.

## Hard rules

- Gates are real: no master write before Gate 2, no exceptions for "obvious" cases.
- Apply the corrections ledger bottom-to-top so row inserts do not shift pending targets.
- Timecode is per-edit; identity is the slate. Never match across cuts by TC.
- If the cut arrives at a different frame rate than the last one, stop and flag it; every
  frame-based comparison silently breaks otherwise.
