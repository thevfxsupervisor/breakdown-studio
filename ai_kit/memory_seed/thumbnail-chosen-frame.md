---
name: thumbnail-chosen-frame
description: The representative thumbnail is the operator's CHOSEN frame (start/mid/end per the Frame column), never a hardcoded middle - applies to the sheet build, master pushes, and every cross-sheet thumbnail copy
metadata:
  type: feedback
---

Each shot has three stills (url_start / url_mid / url_end) and a `Frame` column holding the
operator's choice. The shot's representative thumbnail is `IMAGE(url_[Frame])`:
`=IMAGE(IF(Frame="start",url_start,IF(Frame="end",url_end,url_mid)))` - never `=IMAGE(url_mid)`
hardcoded.

**Why:** the operator picks the frame that actually shows the VFX work (the impact frame, the
clean plate moment, the creature reveal). A pipeline that silently reverts to the middle frame
throws that judgment away, and side-by-side review views end up comparing the wrong frames.

**How to apply:** whenever code or a formula reaches for "the thumbnail" - the sheet build, a
push of thumbnails into a master, a reference column showing the master's frame next to the new
cut's, vendor packages, contact sheets - resolve it through the `Frame` column. When exporting
across sheets, expose the CHOSEN url in the export tab so the importer cannot get it wrong.
