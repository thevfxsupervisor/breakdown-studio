---
name: protected-shots
description: Manually-added invisible-VFX shots in the master have no slate and must never be auto-omitted by a cut update
metadata:
  type: project
---

A mature master breakdown is a **curated** producer document: a large share of its VFX shots are
**hand-added invisible-VFX** (DMP backgrounds, screen replacements, clean-ups, online
transitions/shakes, muzzle flashes, look treatments, CG additions). These have **no on-set slate**,
so they never match a new cut by slate or by picture.

**Rule:** a master shot flagged as manually added (e.g. `NEW Cut<N>` / `manual` / `added` in the
revision column) is PROTECTED and must **never be auto-omitted** by a cut update. Only clean
editorial shots with no such flag are legitimate omit candidates. Keep the protected list and
exclude it from every omit proposal. If a prior cut wrongly omitted a protected shot, restore it in
the next gated update.

This is the [[persist-decisions]] principle in the raw: the "kept, do not omit" decision must
persist cut-to-cut, or the auto-omit re-flags the same hand-added shots every cut. Related:
[[match-1to1-unique]].
