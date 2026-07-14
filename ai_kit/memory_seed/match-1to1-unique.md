---
name: match-1to1-unique
description: A master shot code is unique, so cross-cut matching must be a global 1:1 assignment - exact code beats ordinal beats slate beats visual
metadata:
  type: feedback
---

A master Shot Code is **unique** (one editorial shot), so cross-cut matching MUST be a **global 1:1
assignment**: at most one new-cut shot proposes any master code, and each code is used at most once.

**Why:** per-shot argmax with no global constraint produces collisions (two shots claiming one
code). The fix is a greedy 1:1 assignment.

**Priority tiers (highest first):**
1. **Exact code**: new shot's own code == a master code. Prefer above all else.
2. **Ordinal within a (slate, take) group**: pair pieces in **counter order** (absorbs drift);
   never reorder a slate group by picture.
3. **Slate / take-relaxed.**
4. **Visual (CLIP)**: slate-less shots only; a slate match ALWAYS beats a visual one.

Guardrail: conditional-format the proposed-code column so duplicates turn red; it must be fully
unique before the match is trusted. Related: [[protected-shots]], [[persist-decisions]].
