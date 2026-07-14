---
name: budget-deltas
description: Build the duration-negotiation view in the master - what the vendor bid on vs what the shot is now, per shot, with the delta color-coded. The producer's renegotiation tool after a re-cut.
---

# budget-deltas

Vendors bid on a specific cut. When the edit changes, shot durations drift, and the producer needs
a per-shot view of **bid duration vs current duration** to renegotiate from. This skill builds that
view inside the master breakdown, without touching any cost machinery.

## Procedure

1. **Pick three adjacent columns** in the master (repurpose stale ones from an older cut if the
   operator agrees; resolve everything by header, and clear stale static values first or the new
   array formulas will refuse to spill).
2. **Column A of the block: "Bid Dur (f)"** - the duration the vendor actually bid on, looked up
   from the bid-era cut's import tab keyed on the shot's bid-era code.
3. **Column B: "Current Dur (f)"** - the new cut's duration, looked up from the current cut's
   import tab keyed on the master code.
4. **Column C: "Dur delta (f)" = B minus A**, as a row-2 header array formula so every existing and
   future row fills automatically.
5. **Conditional formatting on the delta:** red when negative (the shot got SHORTER: negotiate
   down), green when positive (longer: scope creep to price). Leave the delta visible for NEW shots
   too - it reads as the full added frame count, which is exactly the scope signal wanted.
6. **Verify on known shots** (one unchanged, one shrunk, one grown) before declaring it live, and
   confirm zero broken references across the sheet afterwards.

## How the producer uses it

Sort or filter by the delta: every red row is a shorter shot to renegotiate, every green row is
growth to re-price before the vendor does. The delta compares **bid-era to now** (skipping
intermediate cuts) because that is the conversation with the vendor; per-cut history lives in the
import tabs if needed.

## Hard rules

- This block is a **view**; the cost engine's own duration column updates separately. Never wire
  costs to this block, and never touch cost/producer columns.
- Row-2 header array formulas keyed on the master code; never per-row literals (new rows must fill
  themselves).
- If others depend on the master (bidding tooling, vendor imports), coordinate before adding the
  columns and have them re-run their integrity checks after.
