# claude-code-delegation (ccd)

A small local message broker plus a thin CLI that lets interactive
Claude Code sessions on **different backends** — subscription/OAuth, an
OpenRouter model, a local ollama model, whatever launches the session — hand
tasks to each other and get results back, without either side burning tokens
while it waits. It deliberately does not use Claude Code's native
cross-session messaging (`SendMessage`/FleetView): that bus is
auth/config-dir coupled and cannot bridge sessions on different backends. ccd
is auth-agnostic instead — it never reads `ANTHROPIC_BASE_URL`,
`CLAUDE_CONFIG_DIR`, or any Claude-internal session state; the cross-backend
hop happens entirely in `ccd` tool calls to a small broker process. Both
sides stay ordinary interactive TUI sessions a human can attach to, watch,
and steer at any time.

Design record: [`PLAN-ccd-v2.md`](PLAN-ccd-v2.md) (the implementing design —
architecture, wire protocol, and the rationale for every decision below) and
[`PRD.md`](PRD.md) (the original product requirements).

## What this repo is not

No host-specific paths, credentials, handles, or launcher names live here.
This is the generic, public delegation model only. Wiring it onto any
particular machine (which launcher starts a worker, which model/effort tier
it gets, systemd units, ansible roles, personal handles) is a deployment
concern that belongs outside this repo.

The author's own deployment lives in a separate private, ansible-based
layer, and this is roughly the shape a comparable one takes — useful as a
sketch if you are building your own, since nothing here depends on it:

- a role that vendors `ccd_broker/` + the `ccd` CLI onto a host and runs the
  broker as a systemd `--user` unit, with `CCD_SOCKET` pinned to an explicit
  path rather than left to `${XDG_RUNTIME_DIR:-/tmp}` (a Claude Code Bash-tool
  subshell does not reliably inherit `XDG_RUNTIME_DIR`, and the fallback
  quietly starts a *second* broker that neither side reports as an error);
- a role that installs `skills/ccd-worker.md` as
  `~/.claude/skills/ccd-worker/SKILL.md` — the loader scans for a directory
  containing `SKILL.md`, and silently ignores a flat `.md` file;
- a thin launch wrapper that exports `CCD_HANDLE`/`CCD_MODEL`/`CCD_EFFORT`
  and then execs whichever backend launcher was named, so backends stay
  orthogonal to ccd, plus a local naming convention for handles.

Two deployment findings worth carrying over wherever you wire this up: a
handle must be unique among *live* sessions, since two sessions sharing one
would silently race for the same queue; and start ccd participants as
ordinary interactive (or tmux) sessions — `claude --bg` does not propagate
`CCD_HANDLE`/`CCD_MODEL`/`CCD_EFFORT` into the backgrounded session, so it
never announces onto the roster (issue #4).

It also does not ship: broker persistence (queues and the roster are
in-memory only — a broker restart empties both), multi-user auth, or any
transport other than a single Unix domain socket. See PLAN-ccd-v2.md §10 for
what's intentionally deferred.

## Install

There is no packaging yet (no `pyproject.toml`/`setup.py`) — this is a clone
and run repo:

```bash
git clone <this-repo> ~/ccd   # or wherever
export PATH="$HOME/ccd:$PATH"           # puts the `ccd` CLI on PATH
export PYTHONPATH="$HOME/ccd:$PYTHONPATH"   # so `python3 -m ccd_broker` resolves
```

Requires Python 3.9+ (stdlib only, no third-party dependencies) and a Linux
box with `AF_UNIX`/`SO_PEERCRED` support. `bash` is required for the `ccd`
CLI itself.

Start/stop the broker through the CLI rather than invoking the module
directly — `ccd broker start` handles `PYTHONPATH`, backgrounding, and the
pidfile for you:

```bash
ccd broker start     # nohup's `python3 -m ccd_broker`, waits for it to answer ping
ccd ping             # ok (ccd-broker 1.0.0)
ccd broker status     # up | down
ccd broker stop
```

## Environment variables

| var | used by | default | meaning |
|---|---|---|---|
| `CCD_SOCKET` | `ccd`, `ccd_broker` | `${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.sock` | Unix socket path the broker listens on and the CLI connects to. Created mode `0600`; the broker rejects connections from other uids (`SO_PEERCRED`). |
| `CCD_PIDFILE` | `ccd broker start/stop` | `${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.pid` | Where `ccd broker start` records the broker's pid so `ccd broker stop` can find and signal it. The broker's stdout/stderr log goes next to it, at the same path with `.log` in place of `.pid`. |
| `CCD_HANDLE` | `ccd recv`/`announce`/`ret` | *(none — required if `<handle>` isn't passed positionally)* | Default handle for `recv`/`announce`/`ret` so a worker's skill/script doesn't have to hardcode it. |
| `CCD_MODEL`, `CCD_EFFORT` | worker skill (`skills/ccd-worker.md`) convention, not read by `ccd` itself | — | Passed as the `model`/`effort` args to `ccd announce`, so the roster (`ccd ls`) shows other participants which capability tier each handle carries. Set by whatever launches the session. |

The broker itself takes no flags or config file — `$CCD_SOCKET` is its only
configuration surface (`ccd-broker -h` / `python3 -m ccd_broker -h` for the
one-line usage).

## Quick usage example

This mirrors `tests/ccd_smoke.sh`, run against a real broker on this machine.

Start the broker and confirm it's up:

```console
$ ccd broker start
ccd broker: started (pid 12345, socket /run/user/1000/ccd-alice.sock)
$ ccd ping
ok (ccd-broker 1.0.0)
```

Park a "worker" waiting for work (this is what a worker's last tool call of
each turn looks like — it blocks, no polling, no tokens spent while idle),
then from another shell send it a task:

```console
$ ccd recv w1 -t 30 &
[1] 12399
$ ccd send w1 "reply pong" -f disp
sent (id=m1)
$ wait
disp: reply pong
```

The parked `recv` unblocked the instant the message arrived and printed
`<from>: <msg>`.

Announce a handle onto the roster, list it, retire it:

```console
$ ccd announce w1 sonnet medium
announced w1 (sonnet/medium)
$ ccd ls
w1      sonnet  medium
$ ccd ret w1
retired w1
$ ccd ls
(no workers announced)
```

Stop the broker when done:

```console
$ ccd broker stop
ccd broker: stopped (pid 12345)
```

In practice a worker session sets `CCD_HANDLE`/`CCD_MODEL`/`CCD_EFFORT`,
loads the `skills/ccd-worker.md` skill, and repeats: announce once, then
`ccd recv "$CCD_HANDLE" -t 86400` → do the work → `ccd send <from> "<result>"`
→ `ccd recv` again, forever, until it retires on exit.

## Esc-interrupt while a worker is parked

A worker's blocking `ccd recv` is, from the TUI's point of view, an ordinary
pending Bash tool call — so it's interruptible the ordinary way:

- Pressing **Esc** interrupts the pending tool call, which kills the `ccd
  recv` client process. If the broker had already reserved a message for
  that call (dequeue-on-ack, PLAN-ccd-v2.md §5.1), it detects the dropped
  connection and **re-queues that message to the front of the queue** — so
  an interrupt never silently drops a task, and FIFO order for anything else
  waiting is preserved.
- With the tool call interrupted, the human can steer the worker by hand:
  run other tools, ask it something, redirect it — exactly as with any
  other interrupted Claude Code turn.
- When ready to wait again, just tell the worker to `ccd recv` once more. If
  a message had been re-queued, it returns immediately with that message;
  otherwise it parks again.

This is the intended mitigation for the one trade-off of this design's
"direct block" approach (PLAN-ccd-v2.md §3, §8): the worker is genuinely
*parked* while idle, not free to do something else unprompted — Esc always
gets a human back in control without losing work in flight.

## CLI reference

```
ccd send <to> <msg> [-f from]
ccd recv [<handle>] [-t timeout]      ($CCD_HANDLE is the default handle)
ccd announce [<handle>] <model> <effort>
ccd ret [<handle>]
ccd ls
ccd ping
ccd broker start|stop|status
```

Full protocol semantics (wire format, blocking/dequeue-on-ack, the
`Transport` seam) are documented in `ccd_broker/broker.py` and
`ccd_broker/transport_uds.py`, and summarized in
[`PLAN-ccd-v2.md`](PLAN-ccd-v2.md) §5.

## Worker/dispatcher skill and tests

- [`skills/ccd-worker.md`](skills/ccd-worker.md) — the Claude Code skill a
  worker (or a dispatcher, same shape) loads: announce on start, `ccd recv`
  as the last tool call every turn, reply-then-recv-again, retire on exit,
  and the Esc-interrupt note above.
- [`tests/ccd_smoke.sh`](tests/ccd_smoke.sh) — an end-to-end smoke test
  against a private, throwaway broker instance. Run it with
  `tests/ccd_smoke.sh`.
