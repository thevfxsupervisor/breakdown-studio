---
name: persist-decisions
description: Record unique per-shot operator decisions in the sheet keyed on stable identity and inherit them into the next cut so only deltas need review
metadata:
  type: feedback
---

Record every unique per-shot operator decision (slate override, boundary merge/split call,
placeholder id, VFX/partial scope, confirmed match, "keep - do not omit") **in the sheet**, keyed on
**stable identity** (slate + take, not a counter or timecode), and **inherit it into the next cut**.

**Why:** each new cut should surface only *deltas* for review, not re-litigate settled shots.
Without this, protected shots get re-flagged for omit every cut, boundary corrections get lost, and
the operator re-does the same work each time footage changes.

**How to apply:** on a cut update, join the new cut to the recorded decisions by stable identity,
carry the decisions forward, and only present changed/new/ambiguous shots. Related:
[[protected-shots]], [[match-1to1-unique]].
