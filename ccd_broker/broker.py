"""Broker core: queues, roster, request dispatch.

Transport-agnostic on purpose — nothing in this module imports `socket`. The
one abstraction boundary is the `Transport` protocol at the bottom of this
docstring; `ccd_broker.transport_uds.UnixSocketTransport` is the only concrete
implementation (see PLAN-ccd-v2.md §5.1: no plugin registry, no loader, no
`--transport` flag).

State (all in-memory, no persistence — a broker restart means empty queues and
an empty roster; workers re-`announce` on their next start):

    queues:  <handle> -> [msg, msg, ...]     FIFO, one deque per handle
    roster:  <handle> -> {model, effort, owner, cwd, session}
                                             owner=None until claimed;
                                             cwd/session None unless announced

Wire model — one logical request per connection, line-delimited JSON:

    -> {"method": "send", "args": {"to": "w1", "msg": "hi", "from": "disp"}}
    <- {"ok": true, "id": "m1"}

| method   | args                    | reply                                     |
|----------|-------------------------|-------------------------------------------|
| send     | {to, msg, from?}        | {ok:true, id}                             |
| recv     | {handle, timeout?}      | {ok:true, from, msg} / {ok:false,         |
|          |                         |  reason:"timeout"}          *blocking*    |
| announce | {handle, model, effort, | {ok:true} / {ok:false} if the handle is   |
|          |  cwd?, session?,        |  live with a different model/effort, or   |
|          |  exclusive?, force?}    |  live at all when exclusive               |
| retire   | {handle}                | {ok:true}  (releases anything it claimed) |
| claim    | {handle, owner, force?} | {ok:true} / {ok:false} if already claimed |
| release  | {handle, owner?}        | {ok:true}                                 |
| roster   | {}                      | {ok:true, workers:[{handle,model,effort,  |
|          |                         |  owner,cwd,session}]}                     |
| ping     | {}                      | {ok:true, version, started_at}            |

Affiliation (ADR-0007): a worker announces *unowned*; a dispatcher `claim`s it,
exclusively and atomically. The broker then refuses a **dispatcher's** `send` to
a worker another dispatcher holds — but never refuses a sender that claims
nothing, so a human at a third shell can always reach any worker. Sender
identity is self-asserted (ADR-0006), so this stops a confused dispatcher, not a
dishonest one.

Where the dashboard's data comes from (ADR-0008): `announce` carries two
optional self-asserted fields, `cwd` and `session`, and `roster` hands them
back untouched. They are the *only* correlation between a handle and the
session's own transcript, which is where the dashboard reads working tree,
status, cost and content — the broker stores no content and learns nothing
about a message it has delivered (ADR-0003). Both are self-asserted like
`from` (ADR-0006): they prevent an accident, not a lie. The broker neither
validates them nor reads a transcript itself.

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

VERSION = "1.2.0"  # 1.2: announce carries cwd/session for the dashboard (ADR-0008)

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
            # Refuse only a *dispatcher's* send to a worker someone else
            # holds. A sender that claims nothing is never refused: losing
            # that escape hatch would mean a stuck worker could only be fixed
            # by killing it (ADR-0007).
            target = self._roster.get(to)
            owner = target.get("owner") if target else None
            if owner is not None and owner != sender and self._owns_any(sender):
                return _err(
                    f"'{to}' is claimed by '{owner}'; "
                    f"'{sender}' claims workers of its own and may not send to it"
                )
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
        model = _as_text(args.get("model"))
        effort = _as_text(args.get("effort"))
        # Where this session lives and which transcript is its own. Optional,
        # unvalidated, and never read by the broker — they exist so `roster`
        # can hand the dashboard something to correlate a handle with
        # (ADR-0008). A participant that has neither (a non-Claude-Code
        # backend, a shell) simply announces without them.
        cwd = _as_opt_text(args.get("cwd"))
        session = _as_opt_text(args.get("session"))
        force = bool(args.get("force"))
        # `exclusive` is for a caller that KNOWS it is starting a new session
        # — a launcher reserving a name before exec. For it, an identical
        # model/effort is not the idempotent re-announce below but a genuine
        # collision: two workers of the same tier on the same issue and phase
        # is precisely the case the launcher's ordinal suffix exists for, and
        # the broker cannot tell the two apart on its own (there is no session
        # identity). Announcing without it stays idempotent, so an
        # Esc-interrupted worker still reclaims its own handle.
        exclusive = bool(args.get("exclusive"))
        with self._cond:
            live = self._roster.get(handle)
            if live is not None and exclusive and not force:
                return _err(
                    f"handle '{handle}' is already announced "
                    f"({live['model']}/{live['effort']})"
                )
            if live is not None and not force:
                # Idempotent re-announce is the common case and must keep
                # working: a session Esc-interrupted mid-park re-announces the
                # same handle at the same tier, and locking it out of its own
                # identity would be worse than the hijack this guards against.
                # A *different* tier means a different session took the name,
                # which is the accident worth catching. Forgery is out of scope
                # (ADR-0006) — an impostor announcing an identical tier is
                # indistinguishable from the real thing and always will be.
                if live["model"] != model or live["effort"] != effort:
                    return _err(
                        f"handle '{handle}' is already announced as "
                        f"{live['model']}/{live['effort']}; retire it first, "
                        f"or pass force"
                    )
            self._roster[handle] = {
                "handle": handle,
                "model": model,
                "effort": effort,
                # A re-announce must not silently drop an existing claim.
                "owner": live.get("owner") if live else None,
                # Nor drop a known cwd/session because the re-announce came
                # from somewhere that could not supply them (a plain shell has
                # no $CLAUDE_CODE_SESSION_ID). Omitted means "unchanged", not
                # "cleared".
                "cwd": cwd if cwd is not None else (live.get("cwd") if live else None),
                "session": (
                    session if session is not None
                    else (live.get("session") if live else None)
                ),
            }
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
            # Releasing what it claimed is the only automatic recovery there
            # is: the roster has no liveness, so a dispatcher that exits
            # without retiring holds its workers until someone forces them
            # free (ADR-0007).
            for entry in self._roster.values():
                if entry.get("owner") == handle:
                    entry["owner"] = None
        return {"ok": True}

    def _owns_any(self, handle: str) -> bool:
        """Is this handle a dispatcher — i.e. does it hold at least one claim?

        Caller must hold the lock.
        """
        return any(e.get("owner") == handle for e in self._roster.values())

    def _m_claim(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        handle = args.get("handle")
        owner = args.get("owner")
        if not _is_handle(handle):
            return _err("claim requires a non-empty string 'handle'")
        if not _is_handle(owner):
            return _err("claim requires a non-empty string 'owner'")
        if handle == owner:
            return _err("a handle cannot claim itself")
        force = bool(args.get("force"))
        with self._cond:
            entry = self._roster.get(handle)
            if entry is None:
                return _err(f"no announced handle '{handle}' to claim")
            held = entry.get("owner")
            # Exclusive and atomic: two dispatchers racing for one worker
            # resolve here rather than both believing they won.
            if held is not None and held != owner and not force:
                return _err(
                    f"'{handle}' is already claimed by '{held}' "
                    f"(pass force to take it over)"
                )
            entry["owner"] = owner
        return {"ok": True, "handle": handle, "owner": owner}

    def _m_release(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        handle = args.get("handle")
        if not _is_handle(handle):
            return _err("release requires a non-empty string 'handle'")
        owner = args.get("owner")
        with self._cond:
            entry = self._roster.get(handle)
            if entry is None:
                return _err(f"no announced handle '{handle}' to release")
            held = entry.get("owner")
            if held is None:
                return {"ok": True, "handle": handle}  # already free
            if _is_handle(owner) and held != owner and not bool(args.get("force")):
                return _err(
                    f"'{handle}' is claimed by '{held}', not '{owner}' "
                    f"(pass force to release it anyway)"
                )
            entry["owner"] = None
        return {"ok": True, "handle": handle}

    def _m_roster(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        with self._cond:
            workers = [dict(e) for _, e in sorted(self._roster.items())]
        return {"ok": True, "workers": workers}

    def _m_ping(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        return {"ok": True, "version": VERSION, "started_at": self.started_at}

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
    "claim": Broker._m_claim,
    "release": Broker._m_release,
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


def _as_opt_text(value: Any) -> Optional[str]:
    """Text, or None for both a missing field and an empty one.

    The CLI always sends `cwd=`/`session=` even when it has nothing to put
    there, so "" has to mean the same as absent — otherwise announcing from a
    shell would blank what a session had already reported.
    """
    if value is None:
        return None
    text = str(value)
    return text or None


def serve(transport_factory: Callable[[Broker], Transport]) -> None:
    """Run a broker forever on the given transport.

    `transport_factory` is a class, not a registry key — v1 hardcodes
    `UnixSocketTransport` at the call site in `__main__`.
    """
    broker = Broker()
    transport = transport_factory(broker)
    transport.serve()
