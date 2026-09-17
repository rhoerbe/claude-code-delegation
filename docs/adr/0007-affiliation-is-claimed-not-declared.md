---
status: accepted
---

# A worker's dispatcher is claimed, not declared

A worker serves exactly one dispatcher. Rather than have the worker name that dispatcher when it announces, workers announce **unowned** and a dispatcher **claims** them from the pool. Scope — which working trees a group of sessions covers — is declared once by the dispatcher at launch and inherited by whatever it claims.

Declaration at announce was the obvious design and was rejected on ordering: it forces the dispatcher to exist first and its handle to be threaded into every worker launch. Claiming inverts that, and matches how the work actually starts — bring up some workers, then decide what to farm out. It also makes affiliation a broker-mediated transaction, atomic under the lock the broker already holds, so two dispatchers racing for one worker resolve rather than both believing they won.

Scope is declared rather than inferred because it cannot be inferred: working groups span repositories (one project's tooling legitimately reaching into another's tree), while every checkout is a sibling of every other, so the filesystem cannot distinguish a group. A registry mapping repos to groups was rejected as a second source of truth that goes stale silently.

## Enforcement, and its limit

The broker refuses a **dispatcher's** send to a worker another dispatcher has claimed. A sender that is not a claiming dispatcher — a human at a third shell — is never refused, because losing that escape hatch means a stuck worker can only be fixed by killing it.

This prevents rather than detects: a convention the worker applies on receipt has already taken the task text into its context before it can object. But per ADR-0006 the sender identity is self-asserted, so it stops a confused dispatcher, not a dishonest one.

**Amended 2026-09-17 (#36): the hatch stays, but it is no longer reachable by accident, and its stamp is not an address.** The escape hatch is identified by an absent sender, which the broker records as the literal `unknown`. That made a `send` whose author had merely forgotten `-f` indistinguishable from a human deliberately claiming nothing — and the worse half, irreversibly so: a worker following its skill replies to the sender its `recv` printed, so the reply went to `unknown`, was accepted with `ok` and a message id, and sat on a queue no roster entry would ever drain. A real run lost a worker's entire report that way, recovered only because the dispatcher noticed the silence and drained the queue by hand.

Two changes, neither touching who may send to whom. `ccd send` defaults its sender to `$CCD_HANDLE` — the default `recv`/`announce`/`ret` already applied — so a participant reaches the hatch only by passing `-f ''` on purpose, while a plain shell that has no handle to default to still reaches it exactly as before. And `unknown` becomes a reserved name: refused as a `send` destination, refused as an `announce` handle. Reserving only the stamp itself was deliberate — a general "the destination must be on the roster" check would have been wrong, because sending to a worker that has not announced yet is normal and must keep working.

`announce` also stops silently overwriting a live handle. That closes a hijack (any client could previously seize any roster entry) and moves collision detection into the only component that can see the whole roster, so a launcher can respond to a broker error instead of racing to check first.

## Release

Release cascades when a dispatcher retires, can be done explicitly, and can be **forced** — a claim may be broken and the worker taken over. The forced path is not a corner case: the roster has no liveness of any kind, and a dispatcher that dies without retiring leaves its claims held indefinitely. A TTL was rejected because refreshing it needs a heartbeat, and not polling is the point of ADR-0002. The human is the recovery mechanism, as with the Esc interrupt.

## Consequences

**This supersedes part of [ADR-0004](0004-one-transport-behind-a-seam.md)**, which keeps configuration out of the broker. Affiliation is policy, and it now lives there. The distinction being drawn is that this is *mechanism with no configuration surface* — one field on announce, one comparison on send, no file, no flag, no registry — and it is in the broker because that is the only place it can prevent rather than detect.
