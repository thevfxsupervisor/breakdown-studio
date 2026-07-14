---
name: note-diff-typo-filter
description: When appending changed notes across cuts, filter OCR-typo variants first - a naive word-diff appends noise ("DUPLICA" vs "DUBLICA") and buries the real changes
metadata:
  type: feedback
---

Note reconciliation is append-only (`old ## new`), but a naive "append when any word differs"
floods the sheet with OCR-typo variants of the SAME note: `CROWD DUPLICATION` vs `CROWD
DUBLICATION`, `Q&A` vs `QSA`, pipe-joined vs space-joined history. Each append then reads like an
editorial change that never happened.

**The filter:** before appending, compare word sets with (a) short tokens dropped (under 4
characters), and (b) a small edit-distance tolerance (Levenshtein <= 2) so near-identical words
count as the same word. Append only when genuinely NEW words survive the filter; hold the
typo-variants unchanged.

**Why:** in production this cut a candidate list of 30 appends to 19 real ones; the 11 filtered
were all OCR spelling drift. The producer reads every append as "editorial changed this shot's
note", so false appends are not noise, they are misinformation.
