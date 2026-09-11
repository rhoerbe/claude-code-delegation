# Architecture decision records

One decision per file, recorded when it was **hard to reverse, surprising without
context, and the result of a real trade-off**. Decisions that fail any of those
three are deliberately not here — see `../../PLAN-ccd-v2.md` for the full
implementing design and `../../PRD.md` for the original requirements.

| | decision |
|---|---|
| [0001](0001-own-broker-not-native-messaging.md) | A private message broker, not Claude Code's native cross-session messaging |
| [0002](0002-worker-blocks-in-recv.md) | A worker blocks in `ccd recv` rather than delegating the wait to a helper |
| [0003](0003-dequeue-on-ack-read-receipt.md) | Delivery is acknowledged by a kernel read-receipt, not by the client saying so |
| [0004](0004-one-transport-behind-a-seam.md) | One transport, behind a seam, with no plugin system |
| [0005](0005-participants-are-interactive-sessions.md) | ccd participants are interactive sessions, never `claude --bg` |
| [0006](0006-one-boundary-uid-authenticates-claims-authorize.md) | One security boundary: peer-uid authenticates, claims authorize |
| [0007](0007-affiliation-is-claimed-not-declared.md) | A worker's dispatcher is claimed, not declared |
| [0008](0008-dashboard-is-metadata-wide-content-scoped.md) | The fleet dashboard is system-wide in metadata, scoped in content |
| [0009](0009-a-launch-picks-one-named-mapping.md) | A launch picks one named mapping, not a slot and an effort |
