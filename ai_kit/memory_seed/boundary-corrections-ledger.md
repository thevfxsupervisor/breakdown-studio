---
name: boundary-corrections-ledger
description: Operator-discovered missed cuts recur every cut behind placeholder slates - record each one in a persistent ledger keyed on slate identity and re-apply it bottom-to-top on every new cut
metadata:
  type: feedback
---

Some missed cuts hide behind postviz or placeholder slates ("work in progress", a `.mov`
filename) that the slate oracle cannot read. The detector will merge the same two shots again on
EVERY future cut, and the operator will find the same boundary again, unless the decision is
recorded.

**The ledger:** a JSON file of corrections, each keyed on stable slate identity (not timecode,
which changes every cut): the split point, which piece is the VFX shot, its master match, and a
note. On each new cut, apply the ledger FIRST (before fresh boundary QC), bottom-to-top by row so
inserts do not shift the rows still pending.

**Why bottom-to-top:** a split inserts a row; applying top-down would shift every later target
row and corrupt the batch.

**How to apply:** the repair tool's ledger mode replays each correction (dry-run first), then
patches the resulting pieces' identity and match, because a mechanical split leaves those on the
wrong halves. New operator discoveries get appended to the ledger the moment they are confirmed,
so the next cut starts smarter than this one.
