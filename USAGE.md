# Using ccd — running a delegation session

How to actually run a dispatcher and a worker and get work done between them.
Installation, the environment-variable table and the per-command CLI reference
live in [`README.md`](README.md); this document is the operating walkthrough
that starts once `ccd ping` answers.

## The model in one minute

A **handle** is a name a session answers to. A session **announces** its handle
to put itself on the **roster** (`ccd ls`), and **retires** it on the way out.

Every participant — dispatcher and worker alike — runs the same loop: announce
once, then spend every turn ending in a blocking `ccd recv` on its own handle.
The block costs nothing while it waits (see [Verifying the
zero-token claim](#verifying-the-zero-token-claim)). What differs between the
two roles is only what you do with a message when one arrives: a worker treats
it as a task and replies with the result; a dispatcher treats it as either a
request to farm out or a result coming back.

There is no dispatcher/worker flag anywhere. The roles are a convention.

## One-time: start the broker

```console
$ ccd broker start
ccd broker: started (pid 12345, socket /run/user/1000/ccd-alice.sock)
$ ccd ping
ok (ccd-broker 1.0.0)
```

**Pin `CCD_SOCKET` explicitly** if anything will call `ccd` from inside an agent
session rather than a login shell. The default is
`${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.sock`, and a tool-invoked subshell does not
reliably inherit `XDG_RUNTIME_DIR` — when it is missing, `ccd` silently resolves
to `/tmp` and **auto-starts a second broker there**. Nothing errors. A handle
registered against one broker simply never sees messages sent to the other:

```bash
export CCD_SOCKET="/run/user/$(id -u)/ccd-$USER.sock"
```

This is the single most common way a working setup appears to be broken.

## Starting a participant

Each participant is an ordinary interactive Claude Code session, started in its
own terminal or tmux window, with its identity in the environment:

```bash
export CCD_SOCKET="/run/user/$(id -u)/ccd-$USER.sock"
export CCD_HANDLE=w1 CCD_MODEL=sonnet CCD_EFFORT=medium
claude          # or whichever launcher selects the backend you want
```

Then tell the session it is a ccd participant, so it loads the `ccd-worker`
skill and starts the loop. The skill is installed at
`~/.claude/skills/ccd-worker/SKILL.md` — a **directory containing `SKILL.md`**,
which is the shape the loader scans for. A flat `ccd-worker.md` file is silently
never discovered.

> **Do not use `claude --bg` for a participant.** It propagates `--name` but not
> `CCD_HANDLE`/`CCD_MODEL`/`CCD_EFFORT`, so the session cannot announce; and a
> backgrounded session cannot resume itself across a usage limit, while an
> interactive one can. Both reasons and their evidence are in
> [ADR-0005](docs/adr/0005-participants-are-interactive-sessions.md).

A worker's first action is to announce, which you can confirm from any shell:

```console
$ ccd ls
disp    opus    high
w1      sonnet  medium
```

Handles must be **unique among live sessions**. Two sessions sharing one handle
race for the same queue, and each message goes to whichever calls `recv` first.

## The loop, end to end

From the dispatcher, hand `w1` a task:

```console
$ ccd send w1 "Summarise the failure modes in tests/ccd_smoke.sh" -f disp
sent (id=m1)
```

`w1`'s parked `recv` returns immediately and prints the message with its sender:

```
disp: Summarise the failure modes in tests/ccd_smoke.sh
```

The worker does the work, sends the result back to the handle it came from, and
parks again — that last step is what keeps it available:

```console
$ ccd send disp "8 cases; only step 4 exercises re-queue-on-disconnect" -f w1
sent (id=m2)
$ ccd recv w1 -t 86400
```

Always give `recv` a long timeout. A short one just means falling out and
calling `recv` again for no benefit.

Pass `-f` on every `send`. It is optional, and a message sent without it arrives
attributed to `unknown`, leaving the receiver with no handle to reply to.

## Steering a worker by hand

A parked `recv` is an ordinary pending tool call, so **Esc** interrupts it and
gives you the session back — run tools, ask questions, redirect it entirely.

Nothing is lost. If the broker had reserved a message for that call, it detects
the dropped connection and re-queues it at the **front** of the queue. When you
want the worker waiting again, tell it to `ccd recv` once more: a re-queued
message returns instantly, otherwise it parks.

## Shutting down

```console
$ ccd ret w1
retired w1
```

Retire before ending a session, or the roster keeps advertising a handle that no
longer answers. A dispatcher sending to a retired handle gets no error — the
message simply queues for a session that will never collect it.

## Troubleshooting

| symptom | cause |
|---|---|
| `ccd recv: no handle given and $CCD_HANDLE not set` (exit 2) | Identity not in the environment. If the session was started with `claude --bg`, that is why — see above. |
| Messages sent but never received; both sides look healthy | Two brokers. Check `CCD_SOCKET` on **both** sides resolves to the same path. |
| `ccd ls` is empty but workers are running | The broker restarted. Queues and the roster are in-memory only, and neither side is told. Every participant must re-announce. |
| A task arrives twice | Known, unexplained — see issue #3. Every re-queue is logged to the broker's stderr with its reason; the log sits next to the pidfile. |
| The worker never loads the skill | It is installed as a flat `.md` instead of `ccd-worker/SKILL.md`. |
| `ccd broker start` says it is already running | A live socket exists. `ccd broker status`, and check for a stray second broker. |

## Verifying the zero-token claim

The claim that a parked worker costs nothing is worth checking on your own
setup rather than taking on trust:

1. Park a worker in `ccd recv` and leave it for a minute or so.
2. `ccd send` it something trivial; it wakes and replies.
3. Account the session's token spend for that window.

**Pass:** spend equals the announce turn plus the wake/reply turn, with nothing
attributable to the wait — no assistant turn is generated while the call blocks.
**Fail:** more than those two turns, meaning the session was generating while
parked.
