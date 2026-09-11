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
  ccd announce [<handle>] <model> <effort> [--exclusive] [--force]
  ccd ret [<handle>]
  ccd claim <worker> [<dispatcher>] [--force]
  ccd release <worker> [--force]
  ccd ls
  ccd dashboard [--scope <handle>] [--json] [--write <path>] [--rates <file>]
  ccd pick
  ccd launch [<mapping-id>] [--issue N] [--phase N] [--handle NAME] [-- args]
  ccd ping
  ccd broker start|stop|status

env: CCD_SOCKET, CCD_HANDLE, CCD_PIDFILE, CCD_MAPPINGS"""


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
    force = False
    positional = []
    for arg in argv:
        if arg == "--exclusive":
            exclusive = True
        # `--force` is what three of the broker's own refusals tell the
        # operator to reach for ("retire it first, or pass force"), so the CLI
        # has to be able to send it — without this the advice named something
        # the CLI rejected as a usage error. Same shape as cmd_claim and
        # cmd_release, which have always taken it. No `-f` alias here: `ccd
        # send` already spends `-f` on the sender handle, and one letter
        # meaning two things across subcommands is worse than typing the word.
        elif arg == "--force":
            force = True
        else:
            positional.append(arg)

    if len(positional) == 3:
        handle, model, effort = positional
    elif len(positional) == 2:
        handle = _env_handle()
        model, effort = positional
    else:
        _err("usage: ccd announce [<handle>] <model> <effort> "
             "[--exclusive] [--force]  ($CCD_HANDLE is the default handle)")
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
    if force:
        args["force"] = "1"

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


# ---------------------------------------------------------------------------
# pick and launch (ADR-0009 in claude-code-delegation, hosting#131 phase 4)
#
# `ccd_mappings` is the reader; this is the one place its entries turn into a
# running session. `pick` only lists and reads a choice — it is deliberately
# useless for anything else, so it composes: `ccd launch $(ccd pick)`. `launch`
# does the whole thing: pick (or take an id directly), derive a handle,
# reserve it, and exec the launcher the entry names.
#
# CCD_MAPPING=<id> is the one environment variable a launched session carries
# for this (replacing CCD_MODEL/CCD_EFFORT from before the manifest existed) —
# with one variable there is structurally nothing to fall out of sync (#10).
# ---------------------------------------------------------------------------

def _load_manifest_or_die(command: str):
    """Load+validate the manifest, or print why and return None.

    Distinguishes "not set up" from "set up wrong" the same way the reader
    does: FileNotFoundError and ManifestError are different messages to a
    human, and both are handled the same way by a caller (print, return 1).
    """
    from ccd_mappings import manifest as m

    try:
        return m.load()
    except FileNotFoundError as exc:
        _err(f"ccd {command}: {exc}")
        return None
    except m.ManifestError as exc:
        _err(f"ccd {command}: {exc}")
        return None


def _list_mappings(doc) -> list:
    """(id, label) pairs in presentation order — what a picker shows."""
    from ccd_mappings import manifest as m

    return list(zip(m.ids(doc), m.labels(doc)))


def _prompt_choice(command: str, count: int):
    """Read a 1-based choice from stdin, or print why and return None.

    stdin not a terminal refuses outright rather than attempting a read: a
    pipe with no writer yet and a closed pipe are indistinguishable from here
    without trying, and a picker that might hang invisibly in a non-
    interactive context (a script, a cron job, a CI run) is worse than one
    that always refuses there. This means `ccd pick` cannot be driven by a
    non-interactive pipe at all — script around it with `ccd launch <id>`
    instead, which needs no picking.

    The listing and prompt go to stderr, never stdout: stdout carries only
    the final chosen id, so `id=$(ccd pick)` captures exactly that and
    nothing of the UI around it.
    """
    if not sys.stdin.isatty():
        _err(f"ccd {command}: stdin is not a terminal; pick is interactive "
             f"only — pass the mapping id directly instead")
        return None
    sys.stderr.write(f"choice [1-{count}]: ")
    sys.stderr.flush()
    raw = sys.stdin.readline()
    if raw == "":  # EOF (e.g. Ctrl-D) — a real, if unusual, tty condition
        _err(f"ccd {command}: no selection made")
        return None
    raw = raw.strip()
    if not raw:
        _err(f"ccd {command}: no selection made")
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= count):
        _err(f"ccd {command}: '{raw}' is not a valid choice (1-{count})")
        return None
    return int(raw)


def _prompt_field(command: str, label: str, flag: str):
    """Read one line of free text from stdin (issue/phase numbers), or print
    why and return None. Same stdin-must-be-a-terminal rule as
    `_prompt_choice`, for the same reason."""
    if not sys.stdin.isatty():
        _err(f"ccd {command}: stdin is not a terminal; pass {flag} explicitly")
        return None
    sys.stderr.write(f"{label}: ")
    sys.stderr.flush()
    raw = sys.stdin.readline()
    if raw == "":
        _err(f"ccd {command}: no {label} given")
        return None
    raw = raw.strip()
    if not raw:
        _err(f"ccd {command}: no {label} given")
        return None
    return raw


def _broker_version_note(broker_version: str, cli_version: str):
    """A one-line note for a version mismatch, or None when there is none.

    Advisory only: `cmd_launch` prints this and proceeds regardless of which
    side is ahead. An empty `broker_version` (a broker too old to report one,
    or a reply shaped unexpectedly) is treated as nothing to compare, not a
    mismatch — there is no "older"/"newer" to state without a value.
    """
    if not broker_version or broker_version == cli_version:
        return None
    return (f"broker is {broker_version}, this ccd expects {cli_version}; "
            "continuing anyway")


def cmd_pick(argv: list) -> int:
    if argv:
        _err("usage: ccd pick")
        return 2

    doc = _load_manifest_or_die("pick")
    if doc is None:
        return 1

    items = _list_mappings(doc)
    if not items:
        _err("ccd pick: the manifest has no mappings")
        return 1

    for i, (_ident, label) in enumerate(items, start=1):
        sys.stderr.write(f"{i}. {label}\n")
    choice = _prompt_choice("pick", len(items))
    if choice is None:
        return 2

    print(items[choice - 1][0])
    return 0


def cmd_launch(argv: list) -> int:
    positional = []
    issue = None
    phase = None
    handle_override = None
    passthrough: list = []
    rest = list(argv)
    while rest:
        arg = rest[0]
        if arg == "--":
            passthrough = rest[1:]
            rest = []
        elif arg == "--issue":
            if len(rest) < 2:
                return _needs_value("launch", "--issue")
            issue, rest = rest[1], rest[2:]
        elif arg == "--phase":
            if len(rest) < 2:
                return _needs_value("launch", "--phase")
            phase, rest = rest[1], rest[2:]
        elif arg == "--handle":
            if len(rest) < 2:
                return _needs_value("launch", "--handle")
            handle_override, rest = rest[1], rest[2:]
        elif arg.startswith("-"):
            _err(f"ccd launch: unknown argument: {arg}")
            return 2
        elif not positional:
            positional.append(arg)
            rest = rest[1:]
        else:
            _err(f"ccd launch: unexpected argument: {arg}")
            return 2

    from ccd_mappings import manifest as m

    doc = _load_manifest_or_die("launch")
    if doc is None:
        return 1

    if positional:
        ident = positional[0]
        entry = m.find(doc, ident)
        if entry is None:
            _err(f"ccd launch: no mapping '{ident}' in the manifest")
            return 2
    else:
        items = _list_mappings(doc)
        if not items:
            _err("ccd launch: the manifest has no mappings")
            return 1
        for i, (_id, label) in enumerate(items, start=1):
            sys.stderr.write(f"{i}. {label}\n")
        choice = _prompt_choice("launch", len(items))
        if choice is None:
            return 2
        ident, _label = items[choice - 1]
        entry = m.find(doc, ident)

    if handle_override:
        handle = handle_override
    else:
        # hosting's ADR-0002 (revised e782da4, after this file's earlier
        # <issue>-<phase>-<id> finding went to the user): the shape is
        # <issue>-<phase>-<mapping-id>[-<billing>] — billing appended only
        # when the picked entry has one, since it is optional in the
        # manifest (ccd_mappings, hosting#131 phase 4 follow-up). slotname
        # from the original ADR text still has no equivalent here and stays
        # dropped, unchanged from before: there is no "slot" for a remapped
        # entry like moonshotai/kimi-k3, and the derived mapping id already
        # carries the model+effort identity slotname+effort used to.
        if issue is None:
            issue = _prompt_field("launch", "issue", "--issue")
            if issue is None:
                return 2
        if phase is None:
            phase = _prompt_field("launch", "phase", "--phase")
            if phase is None:
                return 2
        handle = f"{issue}-{phase}-{ident}"
        if entry.get("billing"):
            handle += f"-{entry['billing']}"

    try:
        launcher_path = m.resolve_launcher(entry)
    except m.ManifestError as exc:
        _err(f"ccd launch: {exc}")
        return 1

    # Broker version check: mismatch is advisory, never fatal — the same
    # tolerant precedent hosting's ccd-launch follows for an unreachable
    # broker (below), extended to a live-but-different-version one. Refusing
    # here would mean a client and broker deployed one commit apart can no
    # longer launch anything, which is a worse failure than a stale field.
    from ccd_broker.broker import VERSION as CLI_VERSION

    try:
        ping_reply = rpc("ping")
    except Unreachable:
        # hosting's ccd-launch tolerates an unreachable broker and uses the
        # label unreserved rather than refuse to launch — followed here
        # unchanged: a launch still starts a working Claude Code session even
        # when ccd coordination is down, it just cannot join a queue.
        _err(f"ccd launch: broker unreachable at {socket_path()}; "
             f"launching {handle!r} without reserving it")
    else:
        note = _broker_version_note(str(ping_reply.get("version") or ""), CLI_VERSION)
        if note:
            _err(f"ccd launch: {note}")

        candidate = handle
        attempt = 1
        while True:
            try:
                announce_reply = rpc(
                    "announce",
                    handle=candidate,
                    effort=entry.get("effort") or "",
                    model=entry.get("model") or "",
                    mapping=ident,
                    cwd=os.getcwd(),
                    session=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
                    pid=os.environ.get("CLAUDE_PID", ""),
                    exclusive="1",
                )
            except Unreachable:
                # The broker answered ping a moment ago and is gone now —
                # same tolerant fallback as the up-front unreachable case.
                _err(f"ccd launch: broker unreachable at {socket_path()}; "
                     f"launching {candidate!r} without reserving it")
                handle = candidate
                break
            if announce_reply.get("ok"):
                handle = candidate
                break
            attempt += 1
            if attempt > 20:
                _err(f"ccd launch: could not reserve a handle after "
                     f"{attempt - 1} attempts starting from {handle!r}")
                return 1
            candidate = f"{handle}-{attempt}"

    env = dict(os.environ)
    # CCD_MAPPING replaces CCD_MODEL/CCD_EFFORT (ADR-0009): with one variable
    # there is structurally nothing left to fall out of sync (#10).
    env.pop("CCD_MODEL", None)
    env.pop("CCD_EFFORT", None)
    env["CCD_MAPPING"] = ident
    env["CCD_SOCKET"] = socket_path()
    env["CCD_HANDLE"] = handle

    # Same flag order hosting's ccd-launch exec line uses (--name, --model,
    # --effort, then passthrough) so the two stay easy to compare.
    exec_args = [launcher_path, "--name", handle, "--model", entry.get("model") or ""]
    if entry.get("effort"):
        exec_args += ["--effort", entry["effort"]]
    exec_args += passthrough

    os.execve(launcher_path, exec_args, env)
    _err(f"ccd launch: exec of {launcher_path!r} failed")  # pragma: no cover
    return 1  # pragma: no cover — os.execve does not return on success


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
    "pick": cmd_pick,
    "launch": cmd_launch,
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
