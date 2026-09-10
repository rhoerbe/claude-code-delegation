---
status: accepted
---

# One security boundary: peer-uid authenticates, claims authorize

`ccd` runs inside a single security boundary — one uid, one socket, one broker, and every participant fully trusted. We are keeping that, and drawing one line inside it: **the peer-uid check is authentication only, and every authorization decision keys off a worker's claim (ADR-0007), never off the uid.**

The distinction matters because the uid check is currently doing both jobs at once, and that is what would make the boundary permanent. Authorization that keys off uid can only ever express "same user or not", so the day a participant runs as a different user — the model `rhoerbe/freigang` is built on, where each isolated agent gets its own host user — every rule written that way has to be rewritten rather than extended.

## What this is not

**This does not isolate anything.** Within one uid every session can read every other project's files, and the sender identity on a message is self-asserted (`from` is whatever the caller says it is), so enforcement prevents *accidents, not forgery*. The confidentiality measures elsewhere in this design are about not manufacturing new adjacency — not building a place where two clients' material sits together that the filesystem had not already created. Anyone reading "broker-enforced" and concluding that `ccd` separates tenants has misread it.

## Consequences

Multi-uid participation is deliberately **out of scope**, not designed for. Nothing here builds toward it and no allowlist exists. What is promised is narrower and cheaper: because no authorization keys off the uid, admitting other uids later is a change to the connection check alone, not to the model above it.
