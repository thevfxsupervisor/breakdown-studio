---
name: reconcile-notes
description: Reconcile changed VFX editorial notes between two cuts non-destructively, preserving producer annotations. Never overwrites a note.
---

# reconcile-notes

When a new cut's OCR'd VFX notes differ from what's in the breakdown, reconcile **without
destroying** anything a producer wrote. Notes are append-only.

## Procedure

1. Match each new-cut shot to its master/breakdown row (use `/match-cut`'s result, keyed on stable
   identity, not timecode).
2. For each shot, diff the new note against the existing note:
   - **Unchanged**: leave it.
   - **Changed**: append as `old ## new` (keep both; do not overwrite).
   - **Removed**: append `## Blank` rather than deleting the old text.
3. **Hold on OCR noise.** If the new read is shaky (low confidence, garbled, inconsistent across the
   3 frames), do **not** trust it: flag it for a human read instead of writing it.
4. Tag the reconciliation status so the operator can see which rows changed this cut.
5. Preserve any producer flag/annotation already in the note verbatim.

## Hard rules

- Append, never overwrite. A note cell only grows.
- Producer annotations always survive.
- Prefer holding over writing a doubtful OCR result.
