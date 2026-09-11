"""`ccd` — the CLI in front of ccd-broker (PLAN-ccd-v2.md §5.2).

Subcommands: send, recv, announce, ret, claim, release, ls, dashboard, ping,
broker start|stop|status.

The surface here is frozen: flags, positional-overrides-environment defaults,
exit codes and output text are all characterised in `tests/golden/` and
compared by `tests/characterize_cli.sh`. Exit codes are 0 for success, 1 for a
refusal or an unreachable broker, 2 for a usage error.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .rpc import (
    Unreachable,
    log_path,
    pidfile_path,
    rpc,
    socket_path,
    source_checkout_root,
)

USAGE = """usage: ccd <subcommand> [args]

  ccd send <to> <msg> [-f from]
  ccd recv [<handle>] [-t timeout]      ($CCD_HANDLE is the default handle)
  ccd announce [<handle>] <model> <effort> [--exclusive]
  ccd ret [<handle>]
  ccd claim <worker> [<dispatcher>] [--force]
  ccd release <worker> [--force]
  ccd ls
  ccd dashboard [--scope <handle>] [--json] [--write <path>] [--rates <file>]
  ccd ping
  ccd broker start|stop|status

env: CCD_SOCKET, CCD_HANDLE, CCD_PIDFILE"""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _unreachable(command: str) -> int:
    _err(f"ccd {command}: broker unreachable at {socket_path()}")
    return 1


def _ok_or_err(reply: dict, okmsg: str) -> int:
    if reply.get("ok"):
        print(okmsg)
        return 0
    _err("error: " + str(reply.get("reason", "unknown error")))
    return 1


def _env_handle() -> str:
    return os.environ.get("CCD_HANDLE") or ""


def _needs_value(command: str, flag: str) -> int:
    """A flag given without its value.

    The bash implementation this replaces looped forever here (`shift 2` with
    one argument left fails quietly), so there is no frozen behaviour to
    preserve — a usage error is the only sane answer.
    """
    _err(f"ccd {command}: {flag} needs a value")
    return 2


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_send(argv: list) -> int:
    if len(argv) < 2:
        _err("usage: ccd send <to> <msg> [-f from]")
        return 2
    to, msg = argv[0], argv[1]
    rest = argv[2:]
    sender = ""
    while rest:
        if rest[0] == "-f":
            if len(rest) < 2:
                return _needs_value("send", "-f")
            sender = rest[1]
            rest = rest[2:]
        else:
            _err(f"ccd send: unknown argument: {rest[0]}")
            return 2

    try:
        reply = rpc("send", to=to, msg=msg, **{"from": sender})
    except Unreachable:
        return _unreachable("send")
    if reply.get("ok"):
        print("sent (id=" + str(reply.get("id")) + ")")
        return 0
    _err("ccd send: " + str(reply.get("reason", "error")))
    return 1


def cmd_recv(argv: list) -> int:
    handle = ""
    timeout = ""
    rest = list(argv)
    while rest:
        arg = rest[0]
        if arg == "-t":
            if len(rest) < 2:
                return _needs_value("recv", "-t")
            timeout = rest[1]
            rest = rest[2:]
        elif arg.startswith("-"):
            _err(f"ccd recv: unknown argument: {arg}")
            return 2
        elif not handle:
            handle = arg
            rest = rest[1:]
        else:
            _err(f"ccd recv: unexpected argument: {arg}")
            return 2

    handle = handle or _env_handle()
    if not handle:
        _err("ccd recv: no handle given and $CCD_HANDLE not set")
        return 2

    args = {"handle": handle}
    if timeout:
        args["timeout"] = timeout
    try:
        reply = rpc("recv", **args)
    except Unreachable:
        return _unreachable("recv")

    if reply.get("ok"):
        print(str(reply.get("from")) + ": " + str(reply.get("msg")))
        return 0
    if reply.get("reason") == "timeout":
        _err("ccd recv: timeout")
        return 1
    _err("ccd recv: " + str(reply.get("reason", "error")))
    return 1


def cmd_announce(argv: list) -> int:
    exclusive = False
    positional = []
    for arg in argv:
        if arg == "--exclusive":
            exclusive = True
        else:
            positional.append(arg)

    if len(positional) == 3:
        handle, model, effort = positional
    elif len(positional) == 2:
        handle = _env_handle()
        model, effort = positional
    else:
        _err("usage: ccd announce [<handle>] <model> <effort>  "
             "($CCD_HANDLE is the default handle)")
        return 2

    if not handle:
        _err("ccd announce: no handle given and $CCD_HANDLE not set")
        return 2

    # Where this session lives, and which transcript is its own. Both are
    # optional and unvalidated; they are the only thing correlating a handle to
    # the transcript `ccd dashboard` reads its status/cost/content from
    # (ADR-0008). $CLAUDE_CODE_SESSION_ID is set inside a Claude Code session
    # and empty anywhere else, in which case the broker keeps whatever it
    # already had.
    #
    # `model` — the CLI's positional model-slot argument (fable/opus/sonnet/
    # haiku) — is deliberately sent NOWHERE on the wire
    # (claude-code-delegation#13, second pass): the broker's `slot` field it
    # used to feed was dropped before release (a live probe showed naming a
    # model directly reaches it exactly as well), and stuffing a slot name into
    # the broker's `model` field instead would just reintroduce the same
    # conflation this whole change exists to end — "sonnet" is not a resolved
    # model id. The outward CLI surface stays frozen (ccd_smoke.sh depends on
    # the 3-positional shape), so the positional stays and is still used in the
    # status message below; it simply has no wire destination until phase 4
    # gives `ccd launch` a manifest to resolve it through. $CLAUDE_PID is the
    # announcing session's own top-level process id, set inside Claude Code and
    # empty anywhere else — the broker treats an empty pid as "no pid", exactly
    # like a plain shell participant, so it is never reaped for staleness.
    args = {
        "handle": handle,
        "effort": effort,
        "cwd": os.getcwd(),
        "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "pid": os.environ.get("CLAUDE_PID", ""),
    }
    if exclusive:
        args["exclusive"] = "1"

    try:
        reply = rpc("announce", **args)
    except Unreachable:
        return _unreachable("announce")
    return _ok_or_err(reply, f"announced {handle} ({model}/{effort})")


def cmd_ret(argv: list) -> int:
    if len(argv) > 1:
        _err("usage: ccd ret [<handle>]  ($CCD_HANDLE is the default handle)")
        return 2
    handle = (argv[0] if argv else "") or _env_handle()
    if not handle:
        _err("ccd ret: no handle given and $CCD_HANDLE not set")
        return 2
    try:
        reply = rpc("retire", handle=handle)
    except Unreachable:
        return _unreachable("ret")
    return _ok_or_err(reply, f"retired {handle}")


def cmd_claim(argv: list) -> int:
    force = False
    positional = []
    for arg in argv:
        if arg in ("--force", "-f"):
            force = True
        else:
            positional.append(arg)

    if len(positional) == 2:
        worker, owner = positional
    elif len(positional) == 1:
        worker, owner = positional[0], _env_handle()
    else:
        _err("usage: ccd claim <worker> [<dispatcher>] [--force]  "
             "($CCD_HANDLE is the default dispatcher)")
        return 2

    if not owner:
        _err("ccd claim: no dispatcher given and $CCD_HANDLE not set")
        return 2

    args = {"handle": worker, "owner": owner}
    if force:
        args["force"] = "1"
    try:
        reply = rpc("claim", **args)
    except Unreachable:
        return _unreachable("claim")
    return _ok_or_err(reply, f"claimed {worker} for {owner}")


def cmd_release(argv: list) -> int:
    force = False
    positional = []
    for arg in argv:
        if arg in ("--force", "-f"):
            force = True
        else:
            positional.append(arg)

    if len(positional) != 1:
        _err("usage: ccd release <worker> [--force]")
        return 2

    worker = positional[0]
    args = {"handle": worker, "owner": _env_handle()}
    if force:
        args["force"] = "1"
    try:
        reply = rpc("release", **args)
    except Unreachable:
        return _unreachable("release")
    return _ok_or_err(reply, f"released {worker}")


# ccd ls: the roster, plus what the broker cannot know on its own.
#
# Columns: handle, effort, owner, then model/pid/status/drift appended — NOT
# handle/slot/effort/owner as originally shipped in this same 1.3.0: a live
# probe (claude-code-delegation#13, second pass) showed naming a model directly
# reaches it exactly as well as routing through a slot alias, so `slot` was
# dropped from the schema before anything shipped, and this column goes with it
# rather than printing something with nothing left to hold.
#
# Deliberately NO header row: a column-index consumer here treats every
# non-blank, non-"(no workers announced)" line as one worker's data, so a
# header would misparse as a fake worker literally named "HANDLE".
#
# pid/status come straight from the roster reply (the broker already resolved
# liveness). drift is NOT the broker's to know (ADR-0003: it holds no
# transcript access) — it is computed here, the same way `ccd dashboard` does,
# by reading the announced session's own transcript (ADR-0008) and comparing
# what it shows against what was declared. The roster itself is never touched
# by what is found.
def cmd_ls(argv: list) -> int:
    try:
        reply = rpc("roster")
    except Unreachable:
        return _unreachable("ls")

    if not reply.get("ok"):
        _err("ccd ls: " + str(reply.get("reason", "error")))
        return 1

    workers = reply.get("workers") or []
    if not workers:
        print("(no workers announced)")
        return 0

    from ccd_dashboard import transcript as tx

    for worker in workers:
        handle = str(worker.get("handle"))
        effort = worker.get("effort") or "-"
        owner = worker.get("owner") or "-"
        model = worker.get("model") or "-"
        pid = worker.get("pid")
        if pid is None:
            pid_col, status = "-", "-"
        else:
            pid_col, status = str(pid), ("up" if worker.get("alive") else "dead")

        # Observed values come from the session's own transcript, never from
        # the broker (ADR-0003/0008) — located the same way `ccd dashboard`
        # does. `!` never fires on a field that was never declared: an
        # undeclared `model` is not a discrepancy, it is simply unknown.
        drift = ""
        located = tx.locate(worker.get("cwd"), worker.get("session"))
        if located:
            facts = tx.parse(located.path)
            declared_effort = worker.get("effort") or None
            declared_model = worker.get("model") or None
            if facts.effort and declared_effort and facts.effort != declared_effort:
                drift = "!"
            elif facts.model and declared_model and facts.model != declared_model:
                drift = "!"

        print("\t".join([handle, effort, owner, model, pid_col, status, drift]))
    return 0


# ccd dashboard [--scope <handle>] [--json] [--write <path>] [--rates <file>]
#
# Read-only (ADR-0008): metadata for every announced handle, and the one
# content line of the scoped handle only. The roster comes from the broker;
# everything richer than the roster comes from each session's own transcript,
# never from the broker, which holds no content at all (ADR-0003).
def cmd_dashboard(argv: list) -> int:
    passthrough = []
    rest = list(argv)
    while rest:
        arg = rest[0]
        if arg in ("--scope", "--write", "--rates"):
            if len(rest) < 2:
                _err(f"ccd dashboard: {arg} needs a value")
                return 2
            passthrough += [arg, rest[1]]
            rest = rest[2:]
        elif arg == "--json":
            passthrough.append("--json")
            rest = rest[1:]
        elif arg in ("-h", "--help"):
            print("usage: ccd dashboard [--scope <handle>] [--json] "
                  "[--write <path>] [--rates <file>]")
            return 0
        else:
            _err(f"ccd dashboard: unknown argument: {arg}")
            return 2

    try:
        reply = rpc("roster")
    except Unreachable:
        return _unreachable("dashboard")

    from ccd_dashboard.__main__ import main as dashboard_main

    # The renderer reads the roster reply on stdin — that is the seam that lets
    # it be driven from a file in tests, with no transport of its own.
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(reply) + "\n")
    try:
        return dashboard_main(passthrough)
    finally:
        sys.stdin = saved


def cmd_ping(argv: list) -> int:
    try:
        reply = rpc("ping")
    except Unreachable:
        _err(f"down (broker unreachable at {socket_path()})")
        return 1

    if not reply.get("ok"):
        _err("ccd ping: " + str(reply.get("reason", "error")))
        return 1

    line = "ok (ccd-broker " + str(reply.get("version", "?")) + ")"
    started = reply.get("started_at")
    if started:
        up = max(0, int(time.time() - float(started)))
        hours, rem = divmod(up, 3600)
        line += " up " + (str(hours) + "h" if hours else "") + str(rem // 60) + "m"
    print(line)
    return 0


# ---------------------------------------------------------------------------
# broker process management
# ---------------------------------------------------------------------------

def _broker_answers() -> bool:
    try:
        reply = rpc("ping")
    except Unreachable:
        return False
    return bool(reply.get("ok"))


def _broker_start() -> int:
    if _broker_answers():
        print(f"ccd broker: already running (socket {socket_path()})")
        return 0

    pidfile = Path(pidfile_path())
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    root = source_checkout_root()
    if root is not None:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{root}:{existing}" if existing else str(root)

    log = open(log_path(), "wb")
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "ccd_broker"],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, start_new_session=True,
        )
    finally:
        log.close()
    pidfile.write_text(f"{child.pid}\n", encoding="utf-8")

    # Poll-wait for it to come up, not a fixed sleep (avoids racing slow starts
    # and avoids wasting time on fast ones).
    for _ in range(50):  # up to ~5s
        if _broker_answers():
            print(f"ccd broker: started (pid {child.pid}, socket {socket_path()})")
            return 0
        time.sleep(0.1)

    _err(f"ccd broker: launched pid {child.pid} but it did not answer ping "
         f"within 5s (see {log_path()})")
    return 1


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _broker_stop() -> int:
    pidfile = Path(pidfile_path())
    if not pidfile.is_file():
        if _broker_answers():
            _err(f"ccd broker: running but no pidfile at {pidfile} — "
                 "cannot stop automatically")
            return 1
        print("ccd broker: not running")
        return 0

    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if not pid or not _alive(pid):
        pidfile.unlink(missing_ok=True)
        print("ccd broker: not running (stale pidfile removed)")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    for _ in range(50):  # up to ~5s
        if not _alive(pid):
            break
        time.sleep(0.1)

    if _alive(pid):
        _err(f"ccd broker: pid {pid} did not exit within 5s after SIGTERM")
        return 1
    pidfile.unlink(missing_ok=True)
    print(f"ccd broker: stopped (pid {pid})")
    return 0


def _broker_status() -> int:
    if _broker_answers():
        print("up")
        return 0
    print("down")
    return 1


def cmd_broker(argv: list) -> int:
    sub = argv[0] if argv else ""
    if sub == "start":
        return _broker_start()
    if sub == "stop":
        return _broker_stop()
    if sub == "status":
        return _broker_status()
    _err("usage: ccd broker start|stop|status")
    return 2


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

COMMANDS = {
    "send": cmd_send,
    "recv": cmd_recv,
    "announce": cmd_announce,
    "ret": cmd_ret,
    "claim": cmd_claim,
    "release": cmd_release,
    "ls": cmd_ls,
    "dashboard": cmd_dashboard,
    "ping": cmd_ping,
    "broker": cmd_broker,
}


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        _err(USAGE)
        return 2

    cmd, rest = argv[0], argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    handler = COMMANDS.get(cmd)
    if handler is None:
        _err(f"ccd: unknown subcommand: {cmd}")
        _err(USAGE)
        return 2

    try:
        return handler(rest)
    except BrokenPipeError:  # `ccd ls | head` and friends
        return 0
    except KeyboardInterrupt:
        # An Esc-interrupted `ccd recv` is expected and safe: the broker
        # re-queues anything it had reserved for the dropped connection.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
