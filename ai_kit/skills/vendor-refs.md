---
name: vendor-refs
description: Cut per-shot reference clips from the edit, filter to bid-scope VFX only, watermark a set per vendor, and stage the delivery. The clips are named by the master shot code so they line up 1:1 with the bid sheet.
---

# vendor-refs

Vendors bid better against the actual picture than against thumbnails. This skill builds one
reference clip per VFX shot, named by the **master shot code** (so clip names match bid-sheet rows
exactly), then produces a **separately watermarked copy per vendor** for leak attribution.

## Procedure

1. **Build the shot list from the breakdown, not the detector.** A clip is warranted only for rows
   that are shots in the bid: matched to a master code, or confirmed new. Take each shot's master
   code, start tcid, and duration in frames.
2. **Extract one clip per shot** from the cut movie: seek by tcid, length = duration/fps,
   frame-accurate re-encode (crf ~18, faststart, keep audio). Name it `<master_code>.mp4`.
   Parallelize; a feature's worth of shots is minutes of work, not hours.
3. **Filter to bid scope by the master Status column, resolved by header.** "VFX" ships to vendors.
   **Online** (transitions, shakes, wipes handled in finishing), **Omitted**, and **Practical** do
   not; strip them from the vendor sets. Keep the un-watermarked master set complete (including
   Online) as the internal reference.
4. **Watermark per vendor:** large centered vendor name, very transparent (white at roughly 15-20%
   alpha), studio-burn-in style. Render the text with **PIL to a transparent PNG** and composite
   with ffmpeg `overlay`; on many ffmpeg builds `drawtext` segfaults, so do not rely on it. One
   output folder per vendor, every clip watermarked with that vendor's own name.
5. **Verify before delivering:** spot-check a bright and a dark clip in each set (watermark legible,
   correct vendor); confirm counts match the bid-scope shot count; confirm an Online shot is absent.
6. **Stage the delivery** where the operator says (they may route all vendors through one post
   house). Report totals and wait for the send decision; delivery is theirs.

## Hard rules

- Clip names are the **master** codes, never the detector's temporary ids.
- Scope by the master **Status** column (by header). VFX only to vendors; never assume the cut-side
  match list equals the bid scope.
- Each vendor gets clips carrying **their own** watermark; never ship an unwatermarked set outside.
- If the source is a burn-in cut, the slate/TC/note in picture is a feature (the vendor sees the
  shot id and the note), but say clearly these are references, not clean plates.
- These are individual clips; never assemble or ship the whole film without an explicit decision.
