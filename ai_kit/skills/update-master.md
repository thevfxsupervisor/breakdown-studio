---
name: update-master
description: Apply a confirmed cut match to the master breakdown behind an approval gate. Copy-neighbour inserts, live media links, never omit protected shots. The one skill that writes to the master.
---

# update-master

Apply an **already-confirmed** match (from `/match-cut`, signed off by the operator) to the master
breakdown. This is the only skill that writes to the master, so it is the most guarded.

**Preconditions (do not proceed without all of these):**
- The operator has confirmed the match proposal (the proposed-code column is unique, no red cells).
- The proposed-code column is verified unique one more time here.
- If anyone else depends on the master (bidding, budget), you have coordinated the change with them
  first, and they will re-run their integrity checks afterward.

## Procedure

1. **Wire the media columns live** from a per-cut import tab (IMPORTRANGE of the cut sheet, keyed on
   the shot code): timecode, duration, thumbnail, editorial note flow in via header array formulas.
   Resolve every column **by header**.
2. **Update matched shots in place.** Never touch cost/producer columns.
3. **Insert NEW shots by copying a sibling row over the new row first** (inheriting per-row formulas
   and formatting), then fill specifics. A blank insert leaves formulas missing. Insert in edit
   order. After inserting, verify conditional-format ranges did not fragment.
4. **Omits are gated.** Only omit shots the operator approved as genuine drops. **Never omit a
   protected (manually-added invisible-VFX) shot**: exclude the protected list. If a prior cut
   wrongly omitted a protected shot, restore it here.
5. **Verify after writing.** Re-read every changed row and confirm it matches intent: no
   column-shift, no shattered array formulas, no double protection, thumbnails resolve **in the
   browser** (an API `#REF` on an `=IMAGE()` cell is render lag, not a failure: do not "fix" it).
6. **Hand off for integrity checks** if others depend on the master.

## Hard rules

- Approval gate is mandatory. No silent master writes.
- Protected shots are never auto-omitted.
- Copy-a-sibling-row for every insert; never blank-insert.
- Verify in the browser before declaring thumbnails broken.
- Push the operator's **chosen** frame, never hardcoded middle. The thumbnail is `IMAGE(url_[Frame])` where
  `Frame` is the shot's start/mid/end choice - resolve it, don't assume `url_mid`.
