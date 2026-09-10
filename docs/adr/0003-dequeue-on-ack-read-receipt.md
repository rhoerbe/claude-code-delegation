# Delivery is acknowledged by a kernel read-receipt, not by the client saying so

A message is reserved (popped) when a `recv` is served, but only *consumed* once the broker can show the client actually took it. The check is `SIOCOUTQ` on the broker's end of the socket: it reports how many bytes we sent that the peer has not yet consumed, and drops to zero the moment the peer reads them. If the client dies with the reply still unread, the message is re-queued to the **front** of its handle's queue, preserving FIFO order.

We chose a kernel-level receipt over the obvious alternatives because a task handed to a worker that then vanished must not be silently lost, and asking the client to confirm cannot cover the case where the client dies before it can confirm anything.

## Considered options

* **Close-as-acknowledgement** (treat the client hanging up as "it got it"). Rejected: indistinguishable from a client that died with the reply unread. Retained only as a fallback where `SIOCOUTQ` is unavailable.
* **An explicit ack message from the client.** Rejected: doubles the round-trips and still cannot report the death it is meant to detect.

## Consequences

This is the one place the transport can turn a single delivery into a duplicate — if it ever concludes "not taken" about a message the client did read, the next `recv` hands out the same task twice. Because that failure is invisible from both sides, every re-queue now logs which path caused it. Distinguishing the cases relies on Linux AF_UNIX semantics (`POLLERR` is set *before* the unread queue is purged), so the exactness of this mechanism is platform-specific.
