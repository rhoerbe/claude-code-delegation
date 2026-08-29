"""Broker core: queues, roster, request dispatch.

Transport-agnostic on purpose — nothing in this module imports `socket`. The
one abstraction boundary is the `Transport` protocol at the bottom of this
docstring; `ccd_broker.transport_uds.UnixSocketTransport` is the only concrete
implementation (see PLAN-ccd-v2.md §5.1: no plugin registry, no loader, no
`--transport` flag).

State (all in-memory, no persistence — a broker restart means empty queues and
an empty roster; workers re-`announce` on their next start):

    queues:  <handle> -> [msg, msg, ...]     FIFO, one deque per handle
    roster:  <handle> -> {model, effort}

Wire model — one logical request per connection, line-delimited JSON:

    -> {"method": "send", "args": {"to": "w1", "msg": "hi", "from": "disp"}}
    <- {"ok": true, "id": "m1"}

| method   | args                    | reply                                     |
|----------|-------------------------|-------------------------------------------|
| send     | {to, msg, from?}        | {ok:true, id}                             |
| recv     | {handle, timeout?}      | {ok:true, from, msg} / {ok:false,         |
|          |                         |  reason:"timeout"}          *blocking*    |
| announce | {handle, model, effort} | {ok:true}                                 |
| retire   | {handle}                | {ok:true}                                 |
| roster   | {}                      | {ok:true, workers:[{handle,model,effort}]}|
| ping     | {}                      | {ok:true, version}                        |

Atomic dequeue-on-ack (the load-bearing rule, PLAN §5.1/§8): `recv` *reserves*
a message by popping it, but the message is only considered consumed once the
reply has been written to the client **and** the client has acknowledged it by
closing the connection cleanly. If the client dies first — an Esc-interrupt
kills the `ccd recv` process while it is parked — the reserved message is
pushed back onto the **front** of that handle's queue, so FIFO order survives
and no task is lost. See `Broker._recv` and `ClientContext.deliver`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Optional, Protocol, runtime_checkable

VERSION = "1.0.0"

#: `recv` timeout when the client does not supply one (24h — a parked worker).
DEFAULT_RECV_TIMEOUT = 86400.0

#: How often a blocked `recv` re-checks that its client is still there. Only an
#: upper bound on detection latency: an arriving message wakes the waiter at
#: once via the condition variable.
LIVENESS_POLL_INTERVAL = 0.25


@runtime_checkable
class ClientContext(Protocol):
    """The two things the broker needs from a connected client.

    Supplied by the transport (it owns the connection); the broker uses it to
    implement dequeue-on-ack without knowing what a socket is.
    """

    def alive(self) -> bool:
        """False once the peer is gone. Must not block."""

    def deliver(self, resp: dict) -> bool:
        """Write `resp` and confirm the client took it.

        True  -> the client received the reply (commit the reservation).
        False -> the client vanished before reading it (re-queue it).
        """


@runtime_checkable
class Transport(Protocol):
    """The sole abstraction boundary (PLAN §5.1).

    A transport owns connections and the accept loop; it turns bytes into a
    request dict, calls `handle`, and writes the reply back.
    """

    name: str

    def serve(self) -> None:
        """Accept loop. Blocks until the broker is shut down."""

    def handle(self, req: dict) -> Optional[dict]:
        """Handle one request.

        Returns the reply dict for the transport to write, or None when there
        is nothing left to write — either the broker already delivered the
        reply itself (the `recv` ack path) or the client is gone.
        """


class Broker:
    """Queues, roster, and request dispatch. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queues: dict[str, deque[dict]] = {}
        self._roster: dict[str, dict] = {}
        self._next_id = 0
        self.started_at = time.time()

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def dispatch(self, req: Any, ctx: Optional[ClientContext] = None) -> Optional[dict]:
        """Route one request.

        `ctx` is the calling client, used only by `recv` (for blocking and for
        dequeue-on-ack). Omitting it makes `recv` non-acking, which is what
        in-process callers and tests want.

        Returns the reply dict, or None if the reply was already delivered
        through `ctx` or the client disappeared.
        """
        if not isinstance(req, dict):
            return _err("malformed request: expected a JSON object")

        method = req.get("method")
        if not isinstance(method, str):
            return _err("missing 'method'")

        args = req.get("args")
        if args is None:
            # Tolerate flat requests ({"method":"send","to":...}) so a thin
            # shell client does not have to build a nested object.
            args = {k: v for k, v in req.items() if k != "method"}
        if not isinstance(args, dict):
            return _err("'args' must be an object")

        handler = _METHODS.get(method)
        if handler is None:
            return _err(f"unknown method: {method}")
        return handler(self, args, ctx)

    # ------------------------------------------------------------------
    # methods
    # ------------------------------------------------------------------

    def _m_send(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        to = args.get("to")
        if not _is_handle(to):
            return _err("send requires a non-empty string 'to'")
        if "msg" not in args:
            return _err("send requires 'msg'")
        msg = args["msg"]
        sender = args.get("from")
        if sender is None or sender == "":
            sender = "unknown"
        with self._cond:
            self._next_id += 1
            item = {"id": f"m{self._next_id}", "from": sender, "msg": msg}
            self._queues.setdefault(to, deque()).append(item)
            self._cond.notify_all()
        return {"ok": True, "id": item["id"]}

    def _m_recv(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        handle = args.get("handle")
        if not _is_handle(handle):
            return _err("recv requires a non-empty string 'handle'")
        timeout = args.get("timeout", DEFAULT_RECV_TIMEOUT)
        if timeout is None:
            timeout = DEFAULT_RECV_TIMEOUT
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return _err("'timeout' must be a number of seconds")
        return self._recv(handle, timeout, ctx)

    def _m_announce(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        handle = args.get("handle")
        if not _is_handle(handle):
            return _err("announce requires a non-empty string 'handle'")
        entry = {
            "handle": handle,
            "model": _as_text(args.get("model")),
            "effort": _as_text(args.get("effort")),
        }
        with self._cond:
            self._roster[handle] = entry
        return {"ok": True}

    def _m_retire(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        handle = args.get("handle")
        if not _is_handle(handle):
            return _err("retire requires a non-empty string 'handle'")
        with self._cond:
            # The queue is deliberately left alone: a retiring worker may still
            # have undelivered mail, and a restarted worker re-announces under
            # the same handle and picks it up.
            self._roster.pop(handle, None)
        return {"ok": True}

    def _m_roster(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        with self._cond:
            workers = [dict(e) for _, e in sorted(self._roster.items())]
        return {"ok": True, "workers": workers}

    def _m_ping(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        return {"ok": True, "version": VERSION}

    # ------------------------------------------------------------------
    # blocking recv + dequeue-on-ack
    # ------------------------------------------------------------------

    def _recv(
        self, handle: str, timeout: float, ctx: Optional[ClientContext]
    ) -> Optional[dict]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._cond:
                queue = self._queues.get(handle)
                if queue:
                    item = queue.popleft()  # reserved, not yet consumed
                else:
                    item = None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {"ok": False, "reason": "timeout"}
                    self._cond.wait(min(remaining, LIVENESS_POLL_INTERVAL))

            if item is None:
                # Nothing reserved yet: if the client walked away while parked,
                # drop the request without touching the queue.
                if ctx is not None and not ctx.alive():
                    return None
                continue

            resp = {
                "ok": True,
                "id": item["id"],
                "from": item["from"],
                "msg": item["msg"],
            }
            if ctx is None:
                return resp  # no ack protocol for in-process callers
            if ctx.deliver(resp):
                return None  # delivered and acknowledged -> consumed
            self.requeue_front(handle, item)
            return None

    def requeue_front(self, handle: str, item: dict) -> None:
        """Put a reserved-but-undelivered message back at the head of the queue.

        Front, not back: the interrupted `recv` held the oldest message, so
        restoring it anywhere else would reorder the queue behind its back.
        """
        with self._cond:
            self._queues.setdefault(handle, deque()).appendleft(item)
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # introspection (tests, diagnostics)
    # ------------------------------------------------------------------

    def queue_depth(self, handle: str) -> int:
        with self._cond:
            return len(self._queues.get(handle, ()))

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "queues": {h: list(q) for h, q in self._queues.items() if q},
                "roster": {h: dict(e) for h, e in self._roster.items()},
            }


_METHODS: dict[str, Callable[[Broker, dict, Optional[ClientContext]], Optional[dict]]] = {
    "send": Broker._m_send,
    "recv": Broker._m_recv,
    "announce": Broker._m_announce,
    "retire": Broker._m_retire,
    "roster": Broker._m_roster,
    "ping": Broker._m_ping,
}


def _err(reason: str) -> dict:
    return {"ok": False, "reason": reason}


def _is_handle(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def serve(transport_factory: Callable[[Broker], Transport]) -> None:
    """Run a broker forever on the given transport.

    `transport_factory` is a class, not a registry key — v1 hardcodes
    `UnixSocketTransport` at the call site in `__main__`.
    """
    broker = Broker()
    transport = transport_factory(broker)
    transport.serve()
