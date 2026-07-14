# Breakdown Studio: AI kit

Breakdown Studio automates the **mechanical** half of a VFX shot breakdown (detect, frame,
thumbnail, OCR, upload). This kit briefs an AI coding assistant (Claude Code, or any AI pair) to
help with the **judgment** half the app can't do well: matching a new cut to the master, boundary
QC, note reconcile, and the gated master update.

It is optional. The app runs fine without it. But if you work alongside an AI assistant, this kit is
what turns "a GUI app" into "a GUI app plus a briefed collaborator that already knows your pipeline,
its guardrails, and the traps."

## What's here

| File / folder | What it is | Where it goes |
|---|---|---|
| `CLAUDE.md` | The project brief. Loads into every assistant session automatically. | Copy to your **project root** (rename the `SHW_` prefix + fill sheet IDs). |
| `skills/` | The judgment tasks as invokable skills, each with its guardrails. | Copy into your assistant's skills folder (e.g. `.claude/skills/`). |
| `memory_seed/` | Example durable-memory files demonstrating the "record a decision, inherit it next cut" pattern (8 seeds). | Seed your assistant's memory store, or keep as reference. |
| `PATTERNS.md` | Multi-assistant production patterns: the shared channel, ownership + ping-before-structural-change, cross-assistant gates, verify-after with the peer's own harness. As actually operated on a feature. | Read when you run more than one assistant on a show. |

## The seven skills

The runbook, in show order:

- **`/intake-cut`**: the whole "a new cut arrived" flow: stage, detect, OCR, boundary QC, cut
  sheet, match, and stop at the operator gates. Orchestrates the skills below; never writes to
  the master itself.

The judgment skills it calls:

- **`/qc-boundaries`**: find detector over-splits and missed cuts using the slate as an oracle;
  stage merges/splits. Dry-run by default.
- **`/match-cut`**: stage a unique 1:1 match of this cut's shots to the master, for operator review.
  Never writes to the master.
- **`/reconcile-notes`**: reconcile changed VFX notes non-destructively (append, never overwrite;
  preserve producer annotations).
- **`/update-master`**: the one skill that writes to the master, behind an approval gate:
  copy-neighbour inserts, live media links, protected shots never omitted, verify after write.

The producer deliverables:

- **`/budget-deltas`**: the duration-negotiation view: per shot, what the vendor bid on vs what
  the shot is now, delta color-coded red (shorter: negotiate down) / green (longer: re-price).
- **`/vendor-refs`**: per-shot reference clips named by master code, filtered to bid-scope VFX
  by the master Status column, watermarked separately per vendor for leak attribution.

## The guardrails these encode (learned in production)

- **Shot identity is the slate, never the timecode.** TC is per-edit; matching across cuts by TC is
  wrong.
- **Matching is a unique 1:1 assignment**, priority exact-code > ordinal-within-slate > slate >
  visual. Slate always beats visual.
- **Protected shots** (hand-added invisible-VFX with no slate) are **never auto-omitted**.
- **Persist per-shot decisions** keyed on stable identity so each cut only surfaces deltas.
- **The `=IMAGE()` `#REF` trap:** the API misreports working thumbnails as `#REF`; verify in the
  browser, never delete on that signal.
- **Sheets discipline:** resolve columns by header, copy-a-sibling-row for inserts, drive-copy to
  preserve formatting, never write literals into an array-formula column, dry-run + verify-after-write.

See `CLAUDE.md` for the full brief.
