"""Broker core: queues, roster, request dispatch.

Transport-agnostic on purpose — nothing in this module imports `socket`. The
one abstraction boundary is the `Transport` protocol at the bottom of this
docstring; `ccd_broker.transport_uds.UnixSocketTransport` is the only concrete
implementation (see PLAN-ccd-v2.md §5.1: no plugin registry, no loader, no
`--transport` flag).

State (all in-memory, no persistence — a broker restart means empty queues and
an empty roster; workers re-`announce` on their next start):

    queues:  <handle> -> [msg, msg, ...]     FIFO, one deque per handle
    roster:  <handle> -> {effort, model, pid, mapping,
                          owner, cwd, session}
                                             owner=None until claimed;
                                             cwd/session None unless announced;
                                             pid None for a non-process
                                             participant (a plain shell) —
                                             see "Liveness and reaping" below

Wire model — one logical request per connection, line-delimited JSON:

    -> {"method": "send", "args": {"to": "w1", "msg": "hi", "from": "disp"}}
    <- {"ok": true, "id": "m1"}

| method   | args                    | reply                                      |
|----------|-------------------------|---------------------------------------------|
| send     | {to, msg, from?}        | {ok:true, id}                              |
| recv     | {handle, timeout?}      | {ok:true, from, msg} / {ok:false,          |
|          |                         |  reason:"timeout"}          *blocking*     |
| announce | {handle, effort,        | {ok:true} / {ok:false} if the handle is    |
|          |  model?, pid?, mapping?,|  held by another live session, or live at  |
|          |  cwd?, session?,        |  all when exclusive — see "Handle identity"|
|          |  exclusive?, force?}    |                                             |
| retire   | {handle}                | {ok:true}  (releases anything it claimed)  |
| claim    | {handle, owner, force?} | {ok:true} / {ok:false} if already claimed  |
| release  | {handle, owner?}        | {ok:true}                                  |
| roster   | {}                      | {ok:true, workers:[{handle,effort,         |
|          |                         |  model,pid,mapping,alive,owner,cwd,        |
|          |                         |  session}]}                                |
| ping     | {}                      | {ok:true, version, started_at}             |

Vocabulary (claude-code-delegation#13): "model slot" (`fable`/`opus`/`sonnet`/
`haiku`) is a naming convention used ABOVE this layer — the manifest/launcher
that maps a slot to a concrete model — but the broker does not store it as a
field. It did, briefly, in this same 1.3.0 (never released): a live probe
settled it after — passing a full model id straight to `--model` reached the
model exactly as well as naming a slot Claude Code resolves through
`ANTHROPIC_DEFAULT_*_MODEL` (identical catalog warning, identical assumed
context window). Once a launch already names its model, a roster `slot` would
hold either an alias whose resolved value is already in `model` (a
first-party launch) or nothing meaningful (a manifest launch, which names a
`mapping` entry instead) — vestigial either way, so it was dropped before
anything shipped. What the broker actually carries: `effort` (required, one
value) and `model` (optional — the *resolved* id, e.g. `claude-sonnet-5` or an
OpenRouter slug; typically still empty, since nothing upstream of phase 2/4's
manifest and launcher work can supply it yet). Neither is validated by the
broker — that happens where the manifest is read (phase 2).

Liveness and reaping (#13): `pid` is the announcing session's own top-level
process id (`$CLAUDE_PID` inside Claude Code). A `roster` read lazily reaps —
sound because the broker's socket is per-host and per-uid, so `/proc` is
authoritative for every handle it can hold: an entry whose `pid` is set but
whose `/proc/<pid>` is gone is returned ONE LAST TIME with `alive: false`,
then deleted before the *next* read sees it. A stale entry is not cosmetic —
it makes a dispatcher route work to a queue nobody drains (a silent hang),
and `announce --exclusive` then refuses to reuse the name — which is how a
`w1` outlived its session by hours. An entry with `pid: null` (a plain-shell
participant, never a traceable process) is never reaped: `alive` reads
`null` for it, not `false`, because there is nothing to check.

Handle identity (1.4.0): a re-announce of a live handle is checked against
what the broker already holds. When BOTH sides declare a `pid` that is the
comparison — same pid re-announces itself freely, a different pid that is
still running is refused as a hijack even when the declared metadata matches,
and a different pid whose recorded process is gone simply takes the name
(the assignment overwrites; nothing is reaped outside `roster`, which stays
the only reaping path). When either side has no pid it falls back to the
declared metadata: a different `effort`, or a different `model` where both
sides declared one. A pid-less participant is never refused by a rule it
cannot satisfy — ADR-0005 has it first-class. `force` bypasses all of it.

Drift (#13) is never computed or stored here: the broker holds no transcript
access (ADR-0003) and no opinion about what actually ran. `ccd ls` computes
it itself by reading the same transcript the dashboard does (ADR-0008) and
comparing to the declared `effort`/`model` — the roster is never mutated by
what it finds there.

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

import os
import threading
import time
from collections import deque
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# 1.3: model/pid/mapping replace the overloaded "model" (a resolved id now,
# not a slot alias — "slot" was tried and dropped before release, see the
# module docstring's Vocabulary section); roster reads lazily reap a dead
# pid (claude-code-delegation#13).
VERSION = "1.4.0"

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

    @staticmethod
    def _identity_refusal(handle, live: dict, effort, model, pid) -> Optional[dict]:
        """Is this re-announce a different session taking a live handle?

        Returns an error reply to refuse with, or None to allow. `force`
        bypasses this entirely — it is checked by the caller — which is what
        keeps every refusal here recoverable.

        A **pid is the strongest identity available** and is preferred when both
        sides have one, because it is the only signal an impostor cannot simply
        declare: effort and model are supplied by whoever is announcing, so
        matching metadata is evidence of two workers configured alike, not of
        one session returning.

        When either side has no pid the comparison falls back to the declared
        metadata, unchanged from before. That asymmetry is deliberate: a
        pid-less announce is the normal shape for a plain shell and for any
        backend that is not Claude Code, and ADR-0005 has those participants
        first-class. A rule they cannot satisfy would make them second-class
        and is not evidence of anything — the absence of a pid says nothing
        about who is announcing.
        """
        live_pid = live.get("pid")

        if live_pid is not None and pid is not None:
            # Same process re-announcing its own handle — the Esc-interrupted
            # worker reclaiming itself. Always allowed, whatever the declared
            # metadata now says: the process identity settles it, and a
            # session that legitimately changed effort mid-life is still that
            # session.
            if live_pid == pid:
                return None
            # A different pid, and the recorded one is still running: two live
            # processes want one name. This is the hijack the guard exists for,
            # and it is refused even when effort and model match, because
            # matching metadata is exactly what two workers of the same shape
            # look like — the collision a launcher's ordinal suffix exists for.
            if _proc_exists(live_pid):
                return _err(
                    f"handle '{handle}' is held by live pid {live_pid}; "
                    f"retire it first, or pass force"
                )
            # The recorded process is gone. The name is free, so the announce
            # takes it and the assignment below overwrites the entry. That is
            # not a second reaping path — nothing is deleted here; `roster`
            # remains the only place an entry is reaped. It matters because the
            # sweep runs on a `roster` read ONLY, so without this a dead
            # session's handle stays unusable until someone happens to run
            # `ccd ls` — which is the stale-handle failure #13 is about,
            # wearing its most confusing face.
            return None

        # Neither side can prove process identity, so fall back to what was
        # declared. A *different* effort means a different session took the
        # name, which is the accident worth catching. Forgery stays out of
        # scope (ADR-0006): an impostor announcing identical metadata is
        # indistinguishable from the real thing and always will be.
        if live["effort"] != effort:
            return _err(
                f"handle '{handle}' is already announced at effort "
                f"{live['effort']}; retire it first, or pass force"
            )
        # `model` is compared only when BOTH sides declare one. It is optional
        # and carried forward when omitted, so an announce that simply does not
        # repeat it must not read as a different session — the same reason
        # cwd/session are not part of this check. Two *declared* models that
        # disagree, though, is precisely "a different session took the name".
        live_model = live.get("model")
        if live_model is not None and model is not None and live_model != model:
            return _err(
                f"handle '{handle}' is already announced as model "
                f"{live_model}; retire it first, or pass force"
            )
        return None

    def _m_announce(self, args: dict, ctx: Optional[ClientContext]) -> Optional[dict]:
        handle = args.get("handle")
        if not _is_handle(handle):
            return _err("announce requires a non-empty string 'handle'")
        # Required — the declared identity a re-announce is checked against
        # below. No `slot` alongside it: dropped from this schema before
        # anything shipped (module docstring's Vocabulary section, #13) once
        # a live probe showed naming a model directly reaches it exactly as
        # well as routing through a slot alias.
        effort = _as_text(args.get("effort"))
        # The resolved model id (e.g. claude-sonnet-5, or an OpenRouter
        # slug) and which manifest entry produced this launch. Both optional
        # and unvalidated — nothing upstream can supply them yet (phase 2/4's
        # manifest and launcher work), and a hand-launched session simply
        # omits them. Carried forward on an omitted re-announce, same
        # rationale as cwd/session below: a plain re-announce that cannot
        # resupply them must not blank what a richer announcer once reported.
        model = _as_opt_text(args.get("model"))
        mapping = _as_opt_text(args.get("mapping"))
        # This announcer's own top-level process id ($CLAUDE_PID inside
        # Claude Code), for the liveness/reaping `roster` performs (module
        # docstring). Unlike model/mapping this is NOT carried forward when
        # omitted: a stale pid inherited from a *different* process that
        # happened to reuse this handle would misreport that process's
        # liveness, not this one's. Omitted (or unparsable) simply means "no
        # pid for this registration" — None, same as a plain shell that never
        # had one.
        pid = _as_opt_int(args.get("pid"))
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
        # effort is not the idempotent re-announce below but a genuine
        # collision: two workers of the same effort on the same issue and
        # phase is precisely the case the launcher's ordinal suffix exists for,
        # and the broker cannot tell the two apart on its own (there is no
        # session identity). Announcing without it stays idempotent, so an
        # Esc-interrupted worker still reclaims its own handle.
        exclusive = bool(args.get("exclusive"))
        with self._cond:
            live = self._roster.get(handle)
            if live is not None and exclusive and not force:
                return _err(
                    f"handle '{handle}' is already announced "
                    f"(effort {live['effort']})"
                )
            if live is not None and not force:
                refusal = self._identity_refusal(handle, live, effort, model, pid)
                if refusal is not None:
                    return refusal
            self._roster[handle] = {
                "handle": handle,
                "effort": effort,
                "model": model if model is not None else (live.get("model") if live else None),
                "pid": pid,
                "mapping": mapping if mapping is not None else (live.get("mapping") if live else None),
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
        """Lazily reap a dead pid: see "Liveness and reaping" in the module
        docstring. An entry is shown dead ONCE — the read that discovers
        it — then deleted before any later read can see it again.
        """
        with self._cond:
            dead: list[str] = []
            workers = []
            for handle, entry in sorted(self._roster.items()):
                e = dict(entry)
                pid = e.get("pid")
                if pid is None:
                    e["alive"] = None  # nothing to check — never reaped
                elif _proc_exists(pid):
                    e["alive"] = True
                else:
                    e["alive"] = False
                    dead.append(handle)
                workers.append(e)
            for handle in dead:
                self._roster.pop(handle, None)
                # A dead dispatcher's claims are exactly as stuck as one that
                # exited without retiring (_m_retire's cascade) — free them
                # the same way, rather than leaving workers held by a
                # claimant that will never release them itself.
                for entry in self._roster.values():
                    if entry.get("owner") == handle:
                        entry["owner"] = None
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


def _as_opt_int(value: Any) -> Optional[int]:
    """A pid, or None for a missing/empty/unparsable one.

    Same "" == absent convention as `_as_opt_text` (the CLI's flat wire
    encoding is all-strings), but a pid that fails to parse as an int is
    treated as absent too rather than raising — a malformed pid is not
    grounds for the whole announce to fail (#13: an accident this is meant
    to catch, not one it should itself cause).
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _proc_exists(pid: int) -> bool:
    """Is `/proc/<pid>` present? The one liveness check this module needs.

    Sound because ccd's socket is per-host and per-uid (PLAN §5.1): every
    handle a broker can hold was announced by a process on the *same* host
    under the *same* uid, so /proc is authoritative for it — no pid
    namespace or container boundary can put a foreign process's pid in view
    here. Does not distinguish a zombie (reaped by its own parent, not this
    module's concern) from a running process — out of scope for #13.
    """
    return os.path.exists(f"/proc/{pid}")


def serve(transport_factory: Callable[[Broker], Transport]) -> None:
    """Run a broker forever on the given transport.

    `transport_factory` is a class, not a registry key — v1 hardcodes
    `UnixSocketTransport` at the call site in `__main__`.
    """
    broker = Broker()
    transport = transport_factory(broker)
    transport.serve()
