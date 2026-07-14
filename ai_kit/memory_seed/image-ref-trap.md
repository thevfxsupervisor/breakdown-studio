---
name: image-ref-trap
description: The API and curl report a WORKING =IMAGE() sheet cell as #REF/302 - it is browser render lag, never delete a thumbnail on that signal
metadata:
  type: reference
---

Reading a Google Sheet via the API (or `curl`) reports a **working** `=IMAGE()` cell as `#REF` or a
302 redirect. This is **browser render/activation lag, not a broken, deprecated, or capped image.**
There is no reliable hard render cap you can detect this way.

**Rules:**
- Verify thumbnails **in the browser**, never via the API's `#REF` signal.
- **Never delete or "fix" a thumbnail** because the API returned `#REF`.
- Large image galleries fill in lazily as you scroll; that is normal.
- Before sharing a copy of a sheet, paste-special-as-values into a **copy** (never the source) so
  the images are frozen for the recipient.

This trap has caused real data loss elsewhere (a re-detect + name-dedup left many wrong thumbs, and
API `#REF` reads nearly triggered deletions of good ones). Treat any tool that "cleans up" IMAGE
cells based on an API read as dangerous. Related: [[persist-decisions]].
