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

## Who runs these commands

Every `$ ccd ...` block in this document is the same CLI call whether a human
types it at a login shell or a Claude session runs it as its own Bash tool
call — the invocation and its output look identical either way, which is why
both are shown the same console-block style throughout. Who actually runs
each one differs, though:

- **You, from any shell:** `ccd broker start/stop/status`, `ccd pick`, `ccd
  launch`, `ccd ls`, `ccd dashboard`, and Esc-interrupt steering. These are
  setup, starting sessions, and observability — things done from outside the
  agent sessions. `ccd pick` additionally needs a real terminal (see
  [`ccd pick` is interactive, deliberately](#ccd-pick-is-interactive-deliberately)). `ccd claim`/`ccd release` work
  from a shell too, but belong in the dispatcher's own window (see [Assigning
  workers to the dispatcher](#assigning-workers-to-the-dispatcher)), since
  they name a dispatcher and it is the obvious one.
- **The participant session itself**, once it has loaded the `ccd-worker`
  skill: `ccd announce`, `ccd recv`, `ccd send`, `ccd ret`. Sessions
  communicate only through their own `ccd` tool calls (`PLAN-ccd-v2.md` §0) —
  you never type these by hand; the skill (loaded by the trailing
  `-- "/ccd-worker"` in [Starting a dispatcher](#starting-a-dispatcher)) is
  what drives the loop turn after turn.

[The loop, end to end](#the-loop-end-to-end) below shows the second kind —
read `$ ccd send w1 "..." -f disp` there as *the dispatcher session's own tool
call*, not something you type at a shell.

## One-time: start the broker

```console
$ ccd broker start
ccd broker: started (pid 12345, socket /run/user/1000/ccd-alice.sock)
$ ccd ping
ok (ccd-broker 1.4.0) up 0m
```

The socket address is `$XDG_RUNTIME_DIR/ccd-<account>.sock`, and when that
variable is missing — Claude Code's Bash tool strips it — `ccd` now resolves
`/run/user/<uid>` from the account id instead. That is the same address the
broker computes for itself, so the common case agrees without your help.

It did not always. Earlier, a missing `XDG_RUNTIME_DIR` sent the client to
`/tmp`, where it **started a second broker**. Nothing errored: a handle
registered against one broker simply never saw messages sent to the other,
which is the single most common way a working setup appears to be broken. If
you are chasing that symptom, check that both sides resolve the same path.

`ccd launch` pins `CCD_SOCKET` into every session it starts, so sessions
launched that way are addressed correctly by construction. Pin it yourself for
a session you start by hand:

```bash
export CCD_SOCKET="/run/user/$(id -u)/ccd-$USER.sock"
```

## Starting a dispatcher

**Launch order is free** — a worker announces unowned and can be claimed
whenever it appears, so dispatcher-first and worker-first both work. This
walkthrough starts at the dispatcher because the *task* does.

A participant is an ordinary interactive Claude Code session. You start one by
**picking a mapping**, and everything else follows from that one choice:

```console
$ ccd pick
1. claude-sonnet-5/medium
2. claude-opus-5/high
3. kimi-k3/max
4. glm-5.3/high
choice [1-4]: 3
kimi-k3-max
```

A **mapping** names one way to start a session: which launcher runs, which
model it is told to use, and what effort it requests. `ccd pick` prints the
list and returns the id of the one you chose — here `kimi-k3-max`. Hand that
id to `ccd launch`:

```console
$ ccd launch kimi-k3-max --issue 119 --phase 3 -- "/ccd-worker"
```

That is the whole launch. You stated the model and effort **once**, by picking
them, and nothing asks you to repeat them.

### What `ccd launch` does for you

- **Derives the handle** as `<issue>-<phase>-<mapping-id>` plus the entry's
  billing tag when it has one — `119-3-kimi-k3-max-api` above. It prompts for
  the issue and phase when you do not pass `--issue`/`--phase`, and
  `--handle NAME` overrides the whole shape when you want a plain name.
- **Reserves that handle** with `ccd announce --exclusive` before starting
  anything, so two windows started from the same mapping cannot race for one
  queue. If the name is taken it appends an ordinal and takes the next one —
  a second `119-3-kimi-k3-max-api` becomes `119-3-kimi-k3-max-api-2`.
- **Pins `CCD_SOCKET`** into the launched session, so it addresses the same
  broker you are looking at.
- **Checks the broker version** and warns if it differs from the CLI's. This
  is advisory in both directions and never fatal: a client and broker deployed
  one commit apart should still be able to launch.
- **Execs the launcher the mapping names**, passing Claude Code's own
  `--name`, `--model` and `--effort`.

Anything after `--` is passed through to that launcher untouched. That is how
the trailing `"/ccd-worker"` above reaches the session — it loads the skill and
starts the loop immediately, instead of your having to tell the session by
hand afterwards. **Without it the session starts but never announces**, so it
never appears on the roster.

### One variable, so nothing can disagree

The launched session carries exactly one ccd variable describing what it is:

```
CCD_MAPPING=kimi-k3-max
```

`CCD_MAPPING` **replaces** `CCD_MODEL` and `CCD_EFFORT`, and `ccd launch`
actively removes both from the environment it hands on — so a stale pair
exported in your shell cannot follow a session in and contradict it. That is
the structural fix: with one variable there is nothing left to keep in sync,
and no second place for the same fact to be stated differently. (The bug that
started this: a worker announced itself as sonnet/medium while actually
running Opus, because the model and effort were typed twice and the two copies
drifted.)

`CCD_HANDLE` is set too, to the handle that was derived and reserved.

## Starting a worker

Exactly the same, with a different mapping and its own terminal or tmux
window:

```console
$ ccd launch claude-sonnet-5-medium --issue 119 --phase 3 -- "/ccd-worker"
```

Start as many as you want. Each gets its own derived handle, so nothing
collides. A dispatcher usually runs at a more capable mapping than the workers
it farms tasks out to, but nothing enforces that.

The skill is installed at `~/.claude/skills/ccd-worker/SKILL.md` — a
**directory containing `SKILL.md`**, which is the shape the loader scans for.
A flat `ccd-worker.md` file is silently never discovered.

> **Do not use `claude --bg` for either role.** It propagates `--name` but not
> `CCD_MAPPING`/`CCD_HANDLE`, so the session cannot announce; and a
> backgrounded session cannot resume itself across a usage limit, while an
> interactive one can. Both reasons and their evidence are in
> [ADR-0005](docs/adr/0005-participants-are-interactive-sessions.md).

## The mapping list, and where it comes from

`ccd pick` and `ccd launch` read a **manifest**: JSON at
`${XDG_CONFIG_HOME:-~/.config}/ccd/mappings.json`, or wherever `$CCD_MAPPINGS`
points. Each entry stores five things, three of them optional:

| key | required | meaning |
|---|---|---|
| `launcher` | yes | A bare command name, found on `$PATH`. |
| `model` | yes | The model id, passed verbatim to `--model`. |
| `effort` | no | Omitted for models that never receive one. |
| `billing` | no | A tag appended to derived handles, e.g. `api`, `sub`. |
| `notes` | no | For the operator; never shown as the label. |

The id and the label are **derived from those fields, never stored** — which
is the same principle as `CCD_MAPPING` one level down: a written-down name can
contradict the fields beside it, a computed one cannot. Ids keep dots, so
`glm-5.3` yields `glm-5.3-high`.

**This repo ships no manifest.** The format is public and the reader is here;
producing the file is a deployment concern
([ADR-0009](docs/adr/0009-a-launch-picks-one-named-mapping.md)), which is why
the entries above are a small illustration rather than anyone's real list.
A real one comes from whatever provisions your hosts. README's
[mapping manifest](README.md#the-mapping-manifest) section has the full
format.

### `ccd pick` is interactive, deliberately

`ccd pick` refuses when its input is not a terminal:

```console
$ ccd pick < /dev/null
1. claude-sonnet-5/medium
2. claude-opus-5/high
3. kimi-k3/max
4. glm-5.3/high
ccd pick: stdin is not a terminal; pick is interactive only — pass the mapping id directly instead
```

This is a choice, not a gap. A picker that might block invisibly inside a
script, a cron job or a CI run is worse than one that always refuses there.
**So there is no `ccd pick | ccd launch` pipeline** — for anything scripted,
name the mapping directly with `ccd launch <id>`, which needs no picking at
all.

Interactively, the listing and the prompt go to stderr and only the chosen id
to stdout, so capturing the choice works:

```bash
id=$(ccd pick) && ccd launch "$id" --issue 119 --phase 3 -- "/ccd-worker"
```

## Starting a session by hand

A participant that no manifest describes is fully supported — a plain shell, a
backend that is not Claude Code, a session you started yourself and want on the
roster. Set the handle and announce:

```console
$ export CCD_HANDLE=hand-1
$ ccd announce a-model low
announced hand-1 (a-model/low)
```

`ccd announce [<handle>] <model> <effort>` takes the handle from `$CCD_HANDLE`
when you omit it. These participants are first-class by design
([ADR-0005](docs/adr/0005-participants-are-interactive-sessions.md)) — they
simply do not get the handle derivation, the reservation or the single
variable, because there was no mapping to derive any of it from. Prefer
`ccd launch` where a mapping exists; this is the road for where one does not.

## Assigning workers to the dispatcher

A worker announces unowned; nothing routes to a dispatcher until it claims
one. Claiming is typed in the **dispatcher's own Claude Code window**, using
the `!` prefix that runs a command in that session — no second shell, and no
`CCD_HANDLE=` prefix, since `ccd claim` already defaults the dispatcher to
`$CCD_HANDLE`:

```console
!ccd claim w1
claimed w1 for disp
!ccd claim w2
claimed w2 for disp
```

You can also just tell the dispatcher in plain language — "claim w1 and w2" —
and the skill runs the same commands itself
([skills/ccd-worker.md](skills/ccd-worker.md) §3).

That is the whole assignment step: one `ccd claim` per worker, in any order,
however many you want. The ownership mechanics it sets up (what claiming
actually blocks, releasing, forced takeover) are covered in [Claiming
workers](#claiming-workers) below.

## Checking the roster

A participant's first action is to announce, which you can confirm from any
shell:

```console
$ ccd ls
disp    opus    high    -
w1      sonnet  medium  -
```

The fourth column is the worker's owner, `-` meaning unowned.

Handles must be **unique among live sessions**. Two sessions sharing one
handle race for the same queue, and each message goes to whichever calls
`recv` first.

## Claiming workers

A worker serves **one** dispatcher, and the dispatcher claims it rather than
the worker declaring itself — so workers can be started in any order, before
any dispatcher exists (that's the `ccd claim` step shown in [Assigning
workers to the dispatcher](#assigning-workers-to-the-dispatcher) above).

From then on another dispatcher's `send` to that worker is refused. Your own
`ccd send` from a shell is not — a sender that claims no workers is never
blocked, so you can always reach a worker by hand. `ccd release w1` frees it,
and retiring the dispatcher frees everything it held.

If a dispatcher dies without retiring, its claims survive it — the roster has
no liveness at all. `ccd claim w1 other-disp --force` takes the worker over.

## Fleet dashboard

`ccd ls` answers *who is announced*. `ccd dashboard` answers *what is the
fleet doing* — the same roster plus what each session is actually up to,
rendered as markdown and typed from any shell:

```console
$ ccd dashboard
# ccd fleet — 3 sessions (2026-01-02T03:04:05Z)

| handle | role | model/effort | owner | status | last activity | cost | tree |
|---|---|---|---|---|---|---|---|
| `disp` | dispatcher | opus/high | — | parked | 2m ago | 1.4M | tooling@main |
| `w1` | worker | sonnet/medium | `disp` | working | 8s ago | 902k | tooling@feat/x |
| `w2` | worker | haiku/low | `disp` | parked | 41m ago | 120k | site@main |

_Metadata is system-wide; content is scoped. Pass `--scope <handle>` for one
session's current line._
```

**Metadata is system-wide, content is scoped**
([ADR-0008](docs/adr/0008-dashboard-is-metadata-wide-content-scoped.md)). The
table above is every announced handle. Seeing what one of them is *working on*
means naming it:

```console
$ ccd dashboard --scope w1
… the same table …

## Scope: `w1` (worker)

**Last task** (#42) — disp: Summarise the failure modes in tests/ccd_smoke.sh
```

One bounded line: for a worker the last task it received, for a dispatcher the
goal it was given. No single rendered view ever contains two sessions' content,
which is the point — a view holding several clients' working material is
exactly the adjacency the rest of this design avoids creating.

`status` is read off the session's transcript: **parked** (blocked in `ccd
recv`, the zero-token idle state), **working** (a tool call outstanding, or a
prompt not yet answered), **idle** (turn finished, waiting on a human), or
**unknown** (no transcript to read — see below).

### Where the numbers come from

The broker holds no content and forgets a message the moment it is delivered
([ADR-0003](docs/adr/0003-dequeue-on-ack-read-receipt.md)), so status, cost and
content are read from each participant's **own Claude Code transcript**. The
only thing linking a handle to its transcript is what the session reported when
it announced: `ccd announce` sends its working directory and
`$CLAUDE_CODE_SESSION_ID`, and the transcript is then named deterministically
under `~/.claude/projects/`. Nothing about this is
verified — like `-f` on a send, it is self-asserted
([ADR-0006](docs/adr/0006-one-boundary-uid-authenticates-claims-authorize.md)).

A participant that reported neither — a session on a backend that keeps no such
transcript, or a handle announced by hand from a shell — still appears in the
table, with `unknown` status and an empty cost. That is the honest answer, not
a failure.

Cost is reported in tokens. A dollar figure needs prices, which are
provider-specific and go stale, so none ship here: pass your own table with
`--rates`, a JSON object of `{"<model>": {"input": …, "output": …,
"cache_read": …, "cache_creation": …}}` in USD per million tokens. Any model in
the fleet that your table does not price leaves the whole figure blank rather
than quietly undercounting.

### The JSON model, and where it may not be written

`--json` prints the machine-readable model the markdown is rendered from.
It is **ephemeral by default** — printed, never stored — because it is an
aggregate of several sessions' material. `--write <path>` puts it on disk, and
**refuses any path inside a git working tree**:

```console
$ ccd dashboard --scope w1 --write ./fleet.json
ccd dashboard: refusing to write inside the git working tree at /home/you/src/tooling: …
$ ccd dashboard --scope w1 --write "$XDG_RUNTIME_DIR/fleet.json"
wrote /run/user/1000/fleet.json
```

That refusal is the mechanism that stops one client's content being committed
into another client's repository — the realistic form of this leak, rather than
the dramatic one. There is no override flag.

### It cannot do anything

The dashboard is **read-only**: no stop, no retire, no redirect, and its only
broker call is `roster`. Giving a view write power would need an authorization
story and ADR-0006 leaves no principals to write one against. Control stays in
the participant sessions, where a human is already attached — Esc into the
session's own TUI, or `ccd send` it something from a shell.

## The loop, end to end

As covered in [Who runs these commands](#who-runs-these-commands): everything
below is a tool call the dispatcher or worker session makes itself, shown as
console output for readability — not something typed at a shell. The
dispatcher hands `w1` a task with its own `ccd send`:

```console
$ ccd send w1 "Summarise the failure modes in tests/ccd_smoke.sh" -f disp
sent (id=m1)
```

`w1`'s parked `recv` returns immediately and prints the message with its sender:

```
disp: Summarise the failure modes in tests/ccd_smoke.sh
```

The worker does the work, sends the result back to the handle it came from,
and parks again with its own tool calls — that last step is what keeps it
available:

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
| `'w1' is claimed by 'disp'` on send | Another dispatcher holds that worker. `ccd release w1`, or `ccd claim w1 <you> --force`. |
| A worker is stuck claimed by a dispatcher that no longer exists | Expected — claims have no liveness. Force the claim over. |
| `handle 'w1' is already announced at effort …` | A live handle, different effort. Retire it, or pass a different handle. Re-announcing the *same* effort is allowed, so Esc-interrupt recovery still works. |
| A task arrives twice | Known, unexplained — see issue #3. Every re-queue is logged to the broker's stderr with its reason; the log sits next to the pidfile. |
| `ccd dashboard` shows `unknown` status and no cost for a handle | That session announced no working directory or session id — announced by hand from a shell, or running on a backend that keeps no Claude Code transcript. Nothing is broken; there is simply nothing to read. |
| `ccd dashboard --write` refuses the path | It is inside a git working tree, deliberately and without an override. Write it to a state directory instead. |
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
