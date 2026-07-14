# Multi-assistant production patterns

Everything in this kit works with one assistant. On a real show, the workload splits naturally
into more than one seat: one assistant owns the breakdown (cuts, matching, the master's identity
columns), another owns bidding (vendor sheets, cost rollups, integrity checks), maybe a third owns
renders or a GPU box. These patterns are how multiple assistants shared one production safely, as
actually operated on a feature. They are conventions, not software; any file-capable assistant can
follow them.

## The channel

A shared folder (`.session_comms/`) at the project root, one JSON "outbox" file per assistant:

```json
{"session":"breakdown", "updated":"2026-01-01 14:30:00", "to":"bidding",
 "status":"one-line current state", "message":"one-off note, cleared once acknowledged",
 "data":{"structured":"payload"}, "done":false, "stop":false}
```

Rules: you write only your OWN file, you poll the others (a file-watcher or a slow loop), and
`status` is always current so a crashed or restarted peer can catch up by reading the channel.

## Ownership and the ping protocol

- **Every shared document has ONE owner per concern.** The breakdown assistant owns the master's
  identity/media columns; the bidding assistant owns vendor links and cost columns. Neither writes
  in the other's area without a ping.
- **Ping BEFORE structural change, verify AFTER.** Row inserts, column inserts, and moves in a
  shared sheet get announced first ("inserting N rows, keyed on the frozen code column"), and the
  affected peer re-runs its own integrity harness immediately after ("74 checks green" beats
  "looks fine"). Value-only edits in your own columns need an FYI at most.
- **The harness is the peer's, not yours.** The owner of the dependent system decides what "still
  intact" means and runs their own checks. You just make it easy: say exactly what changed, where,
  and what you already verified.

## Gates travel across assistants

The operator gates in `/intake-cut` bind every assistant, not just the one that hit them. If the
breakdown assistant is waiting on the operator's match sign-off, the bidding assistant does not
build vendor sheets from the half-approved master. Encode the gate state in the channel `status`
so peers can see it.

## Why this is worth the ceremony

A shared master with live cross-sheet imports is the one place a confident assistant can do real
damage fast. In production, this protocol caught a column-shift before it reached vendor sheets,
kept two assistants from writing the same cells in the same hour, and let a 57-row structural
insert land with the dependent system verified green twice in the same afternoon. The cost is a
few JSON writes; the benefit is that nobody reconstructs a broken budget at midnight.
