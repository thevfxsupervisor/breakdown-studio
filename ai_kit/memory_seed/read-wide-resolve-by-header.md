---
name: read-wide-resolve-by-header
description: Resolving sheet columns by header is necessary but NOT sufficient - the read range must also be wide enough to contain the column, or a moved column breaks every tool that reads a fixed range
metadata:
  type: feedback
---

Two-part rule for every tool that reads the breakdown sheet:

1. **Resolve columns by header name, never by letter or index.** Operators rename and reorder
   columns constantly; a `moveDimension` auto-updates in-sheet formula references and conditional
   formatting, but silently breaks any code holding a hardcoded index.
2. **Read WIDE.** Header resolution only works if the column is inside the range you fetched.
   A tool reading `A1:Z` cannot find a column that moved to `AC`, and fails with a confusing
   KeyError far from the cause. Read to the sheet's last column (or a generous fixed bound well
   past it).

**Why:** we moved one review column three positions right; every formula survived, and two
repair tools broke - not because they used letters, but because their read window ended before
the column's new home.

**How to apply:** after moving any column, re-check every script that reads the sheet, not just
the ones that write. Grep for fixed ranges (`A1:Z`, `A:AB`) and widen them. Also normalize join
keys on both sides (a numeric-looking id may be text in one place and a number in another).
