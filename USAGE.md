# Using ccd — running a delegation session

How to actually run a dispatcher and a worker and get work done between them.

The three walkthroughs come first, because what you do depends on what you are
starting from. Everything they use is explained once, in full, in the
[Reference](#reference) below them; installation, the environment-variable
table and the per-command CLI listing live in [`README.md`](README.md).

- [(a) One issue, a dispatcher and some workers](#use-case-a-one-issue-a-dispatcher-and-some-workers)
- [(b) An epic spanning several issues](#use-case-b-an-epic-spanning-several-issues)
- [(c) No issue yet, and a worker to argue with](#use-case-c-no-issue-yet-and-a-worker-to-argue-with)

## The model in one minute

A **handle** is a name a session answers to. A session **announces** its handle
to put itself on the **roster** (`ccd ls`), and **retires** it on the way out.
Messages are addressed to handles.

There are two roles, and they are **not** the same loop:

- A **worker** lives on its queue. It announces, then ends every turn blocked
  in `ccd recv` on its own handle. A message arriving is the only thing that
  starts work. The block costs nothing while it waits (see [Verifying the
  zero-token claim](#verifying-the-zero-token-claim)).
- A **dispatcher** lives with you. Its primary input is the human at its
  keyboard, and it reads its own queue to collect results rather than to wait
  for instructions. A dispatcher that parked on `recv` every turn would be
  unable to hear you.

The broker does not know the difference — there is no role flag on the wire,
and `ccd claim` is the only thing that records a relationship between two
handles. What makes a session one or the other is which skill it loaded:
`/ccd-dispatcher` or `/ccd-worker`.

## One-time: start the broker

```console
$ ccd broker start
ccd broker: started (pid 12345, socket /run/user/1000/ccd-alice.sock)
$ ccd ping
ok (ccd-broker 1.8.0) up 0m
```

One broker per host per account serves every session. If messages are sent but
never arrive, read [The socket, and the two-broker
trap](#the-socket-and-the-two-broker-trap) — it is the most common way a
working setup looks broken.

## Use case (a): one issue, a dispatcher and some workers

You have an issue to work on, and you want to choose the model for each
session rather than accept a default.

A participant is an ordinary interactive Claude Code session. You start one by
**picking a mapping**, and everything else follows from that one choice.

**1. Pick a mapping for the dispatcher.** A mapping names one way to start a
session: which launcher runs, which model it is told to use, what effort it
asks for.

```console
$ ccd pick
1. claude-sonnet-5/medium
2. claude-opus-5/high
3. kimi-k3/max
4. glm-5.3/high
choice [1-4]: 2
claude-opus-5-high
```

`ccd pick` prints the list and returns the id of the one you chose.

**2. Launch the dispatcher with it**, naming the issue and the phase you are
in:

```console
$ ccd launch claude-opus-5-high --issue 119 --phase 3 -- "/ccd-dispatcher"
```

That is the whole launch. You stated the model and effort **once**, by picking
them, and nothing asks you to repeat them. The handle is derived —
`119-3-claude-opus-5-high-sub` — and reserved before the session starts. The
trailing `-sub` is the mapping's optional billing tag; entries without one
derive a handle that simply ends at the mapping id.

**3. Launch the workers**, in their own terminals or tmux windows. Same
command, different mapping, and `/ccd-worker` instead:

```console
$ ccd launch claude-sonnet-5-medium --issue 119 --phase 3 -- "/ccd-worker"
$ ccd launch glm-5.3-high --issue 119 --phase 3 -- "/ccd-worker"
```

Start as many as you want; each derives its own handle, so nothing collides. A
dispatcher usually runs at a more capable mapping than the workers it farms
tasks out to, but nothing enforces that. Launch order is free — a worker announces unowned and can be claimed whenever
it appears, so workers-first works just as well.

**4. Check they are all up**, from any shell:

```console
$ ccd ls
119-3-claude-opus-5-high-sub       high    -  claude-opus-5    31041  up
119-3-claude-sonnet-5-medium-sub   medium  -  claude-sonnet-5  31122  up
119-3-glm-5.3-high-api             high    -  z-ai/glm-5.3     31150  up
```

**5. Give the workers to the dispatcher.** In the dispatcher's own window, `!`
runs a command in that session:

```console
!ccd claim 119-3-claude-sonnet-5-medium-sub
!ccd claim 119-3-glm-5.3-high-api
```

Or just tell it in plain language — "claim both workers" — and it runs the
same commands itself. That is the whole assignment step: one `ccd claim` per
worker, in any order, however many you want. The ownership mechanics it sets up
are in [Claiming workers](#claiming-workers).

**6. Now work in the dispatcher's window and talk to it normally.** It hands
tasks out and collects the results; you do not type `ccd send` yourself. When
you are done, each session retires its own handle on the way out.

## Use case (b): an epic spanning several issues

Same as (a), except the work is not one issue and you want the sessions
labelled by the epic instead.

`--issue` is free-form text, not a number — so the epic name goes straight in:

```console
$ ccd launch claude-opus-5-high --issue auth-rewrite --phase 2 -- "/ccd-dispatcher"
```

That derives `auth-rewrite-2-claude-opus-5-high-sub`. `--phase` is equally
free-form, so it can carry the issue number within the epic if that is what
you want to see on the roster:

```console
$ ccd launch claude-sonnet-5-medium --issue auth-rewrite --phase 119 -- "/ccd-worker"
```

→ `auth-rewrite-119-claude-sonnet-5-medium-sub`.

**Spaces and capitals are fine to type; the handle is normalised.**
`--issue "Auth Rewrite" --phase "Round 2"` derives
`auth-rewrite-round-2-claude-sonnet-5-medium-sub`. Issue and phase are
components of a derived name, and they get the same treatment the mapping id
beside them already gets — one id-safe spelling, so a handle never arrives with
a space in it that every later caller has to quote. A value with nothing usable
in it at all (`--issue "   "`) is a usage error rather than a silent omission.

If you want the handle to say something the derivation cannot, `--handle`
overrides the entire shape:

```console
$ ccd launch claude-opus-5-high --handle auth-rewrite-lead -- "/ccd-dispatcher"
```

`--handle` is **validated rather than normalised** — it is the whole name, and
you chose it deliberately, so handing you back a different one would be the
surprise. Whitespace is refused (exit 2); mixed case and punctuation are yours
to use.

You lose the model identity from the name when you do that, so the roster no
longer tells you what a session is running — `ccd ls`'s model column still
does. Everything else in (a) is unchanged.

## Use case (c): no issue yet, and a worker to argue with

You have an idea, not an issue. You want to think it through out loud and have
someone push back on it.

**Issue and phase are optional**, so omit both:

```console
$ ccd launch claude-opus-5-high -- "/ccd-dispatcher"
$ ccd launch glm-5.3-high -- "/ccd-worker"
```

The handles are just the mapping and its billing tag —
`claude-opus-5-high-sub` and `glm-5.3-high-api`. (`--phase` without `--issue`
is refused: a phase subdivides an issue, so it needs one to subdivide.)

**Pick a different mapping for the critic than for yourself.** A second
opinion from the same model at the same effort is the weakest form of this;
a different provider disagrees for different reasons. This is the case where
the mapping list earns its keep.

Claim it, then work in the dispatcher's window as usual. The contrarian part
is **in the message text, not in the tooling** — there is no critic role, no
flag, and no second skill. A worker does what the message it receives says, so
the dispatcher sends something like:

```console
$ ccd send glm-5.3-high-api "Here is a design I am considering: <the idea>. Argue
against it. Give me the strongest case that this is the wrong approach, the
failure modes I have not named, and what you would do instead."
```

and the reply comes back to the dispatcher's own queue. You can keep the
exchange going for as many rounds as you want; the worker parks again after
each reply.

Two things to know before relying on this:

- **The worker has no memory of your session.** It sees only the text you send
  it. An idea that needs context needs that context in the message, or the
  contrarian view will be aimed at something you did not mean.
- **A worker will not push back unprompted.** Its skill tells it to do what the
  message describes and nothing more — which is what stops a fresh session
  inventing work, and also means "be critical" has to be asked for every time.

## Reference

Everything the walkthroughs use, in full.

### Who runs which command

Every `$ ccd ...` block in this document is the same CLI call whether a human
types it at a login shell or a Claude session runs it as its own Bash tool
call — the invocation and its output look identical either way, which is why
both are shown the same console-block style throughout. Who actually runs each
one differs, though:

- **You, from any shell:** `ccd broker start/stop/status`, `ccd pick`, `ccd
  launch`, `ccd ls`, `ccd dashboard`, and Esc-interrupt steering. These are
  setup, starting sessions, and observability — things done from outside the
  agent sessions. `ccd pick` additionally needs a real terminal (see
  [`ccd pick` without `--list` is interactive,
  deliberately](#ccd-pick-without---list-is-interactive-deliberately)).
  `ccd claim`/`ccd release` work from a shell too, but belong in the
  dispatcher's own window, since they name a dispatcher and it is the obvious
  one.
- **The participant session itself**, once it has loaded its skill
  (`ccd-dispatcher` or `ccd-worker`): `ccd announce`, `ccd recv`, `ccd send`,
  `ccd ret`. Sessions communicate only through their own `ccd` tool calls
  (`PLAN-ccd-v2.md` §0) — you never type these by hand; the skill, loaded by
  the trailing `-- "/ccd-dispatcher"` on the launch, is what drives the loop
  turn after turn.

[Sending and receiving](#sending-and-receiving) below shows the second kind —
read `$ ccd send w1 "..."` there as *the dispatcher session's own tool call*,
not something you type at a shell.

### The socket, and the two-broker trap

The socket address is `$XDG_RUNTIME_DIR/ccd-<account>.sock`, and when that
variable is missing — Claude Code's Bash tool strips it — `ccd` resolves
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

### What `ccd launch` does for you

- **Derives the handle.** `issue` and `phase` are each optional and `billing`
  comes from the mapping, giving six shapes:
  `<issue>-<phase>-<mapping-id>[-<billing>]`, `<issue>-<mapping-id>[-<billing>]`,
  or `<mapping-id>[-<billing>]`. At a terminal it offers a prompt for whichever
  of `--issue`/`--phase` you did not pass; a blank answer means *omit it*, not
  *ask again*. `--phase` without `--issue` is a usage error (exit 2), not a
  prompt. Issue and phase are slugged into the handle the same way the mapping
  id already is (`"Round 2"` → `round-2`); one that slugs to nothing is a usage
  error. `--handle NAME` overrides the whole shape and is validated instead of
  slugged — whitespace refused, everything else yours.
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
the trailing `"/ccd-dispatcher"` reaches the session — it loads the skill and
puts the session to work immediately, instead of your having to tell it by hand
afterwards. **Without it the session starts but never announces**, so it never
appears on the roster.

### The two skills

The trailing prompt is how you choose a role: `/ccd-dispatcher` for the session
you work in, which hands tasks out and collects results, and `/ccd-worker` for
a session that performs tasks it is sent. They are different jobs, not one job
with two settings — a dispatcher's input is you at the keyboard, a worker's is
its own queue.

Each is installed at `~/.claude/skills/<name>/SKILL.md` —
`ccd-dispatcher/SKILL.md` and `ccd-worker/SKILL.md`, each a **directory
containing `SKILL.md`**, which is the shape the loader scans for. A flat
`ccd-worker.md` file is silently never discovered.

Both open by telling the session that loading a skill is not a task and that
it has not been given one. That is deliberate: a worker on a weak model once
read an earlier version, invented a design task on a topic that appears
nowhere in this project, and spent a long answer on it before announcing
anything.

> **Do not use `claude --bg` for either role.** It propagates `--name` but not
> `CCD_MAPPING`/`CCD_HANDLE`, so the session cannot announce; and a
> backgrounded session cannot resume itself across a usage limit, while an
> interactive one can. Both reasons and their evidence are in
> [ADR-0005](docs/adr/0005-participants-are-interactive-sessions.md).

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

### The mapping manifest

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
the entries in the walkthroughs are a small illustration rather than anyone's
real list. A real one comes from whatever provisions your hosts. README's
[mapping manifest](README.md#the-mapping-manifest) section has the full format.

### Listing the mappings without choosing one

`ccd pick --list` prints the manifest and returns, with no prompt and no
terminal required:

```console
$ ccd pick --list
claude-sonnet-5-medium	claude-sonnet-5/medium
claude-opus-5-high	claude-opus-5/high
kimi-k3-max	kimi-k3/max
glm-5.3-high	glm-5.3/high
```

One line per mapping, `id<TAB>label`, on stdout. **The first column is the id,
and the id is what `ccd launch` takes** — the label is for reading. There is
no header and no numbering, the same shape `ccd ls` uses, so every non-blank
line is one record.

This is what a dispatcher session uses: it cannot run the interactive picker
(below), so without a listing it would have to ask you what exists or already
know an id.

### `ccd pick` without `--list` is interactive, deliberately

Bare `ccd pick` is for a human at a keyboard, and refuses when its input is
not a terminal:

```console
$ ccd pick < /dev/null
ccd pick: stdin is not a terminal; picking is interactive only — pass the mapping id directly instead, or use `ccd pick --list`
```

That is a choice, not a gap. A picker that might block invisibly inside a
script, a cron job or a CI run is worse than one that always refuses there.
**So there is no `ccd pick | ccd launch` pipeline** — for anything scripted,
list the mappings and name one with `ccd launch <id>`, which needs no picking
at all. `ccd launch` with no id picks too, and refuses the same way for the
same reason.

Interactively, the listing and the prompt go to stderr and only the chosen id
to stdout, so capturing the choice works:

```bash
id=$(ccd pick) && ccd launch "$id" --issue 119 --phase 3 -- "/ccd-worker"
```

### Starting a session by hand

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

### The roster

A participant's first action is to announce, which you can confirm from any
shell:

```console
$ ccd ls
119-3-claude-opus-5-high-sub      high    -       claude-opus-5    31041   up
119-3-claude-sonnet-5-medium-sub  medium  119-3-claude-opus-5-high-sub  claude-sonnet-5  31122  up
```

Seven tab-separated columns, no header: **handle**, **effort**, **owner**
(`-` when unclaimed), **model**, **pid**, **liveness** (`up`, `dead`, or `-`
for a participant that declared no pid), and a **drift marker**, which is empty
unless what the session actually ran disagrees with what it declared.

A session announced by hand shows `-` for its model even when you passed one.
The model positional deliberately never reaches the wire: what a human types
there is historically a slot name like `sonnet`, which is not a resolved model
id, and recording it as one would reintroduce the exact conflation this project
exists to end — a roster saying `sonnet` for a session running Opus. `model` is
populated only when a mapping resolved it.

Handles must be **unique among live sessions**. Two sessions sharing one
handle race for the same queue, and each message goes to whichever calls
`recv` first.

A handle may not contain a tab, a newline or any other control character — the
broker refuses one at `announce`, because the rows above are tab-separated and
one record per line, so such a handle would render as extra columns or as a
phantom record. A space is still allowed here: it breaks no output format, it
only forces callers to quote, and a hand-announced participant is first-class
rather than held to `ccd launch`'s conventions. `ccd launch` slugs its own
derived handles regardless.

`ccd ls --json` gives the same roster as data rather than columns: every field
the broker holds, including the `mapping` id a session was launched from, the
drift marker, and `pending` — how many messages that handle has been sent and
not yet collected. That is what a script reads when it needs to relaunch the
same work on a different backend — the text rows carry no mapping and
deliberately never will, because they have no header and their consumers count
fields.

### Queued is not delivered

`ccd send` answering `sent (id=m4)` means the broker **accepted** the message.
It has never meant anything received it. The message sits on a queue until some
session calls `recv` on that handle, and `pending` above is how many are
waiting.

The case worth watching for is a queue with no session behind it at all:

```console
$ ccd ls
disp	high	-	claude-opus-5	31041	up
ccd ls: 1 message(s) queued for 1 handle(s) not on the roster: w2 (1)
ccd ls: nothing will collect these until a session announces that handle and calls recv.
```

That warning goes to **stderr**, so a piped `ccd ls` is byte-for-byte what it
always was; `ccd ls --json` carries the same thing as an `orphans` object.

A queue with no roster entry is not automatically an error — sending to a
worker *before* it announces is a supported pattern, and the message is waiting
correctly. It becomes a problem when nothing ever announces that handle: a
typo'd handle, a session that retired with work still queued, or a session
whose process died and was reaped. In each case `ccd send` said `sent` and
nothing was wrong at the time.

**A broker restart discards all of it** — queues and roster alike are in memory
only. That is the failure behind
[#39](https://github.com/rhoerbe/claude-code-delegation/issues/39): a deploy
restarted the broker mid-run, two accepted messages went with it, and the
dispatcher went on believing work was in flight. The `ccd` ansible role now
refuses to deploy while any session is announced, for exactly that reason.

### Claiming workers

A worker serves **one** dispatcher, and the dispatcher claims it rather than
the worker declaring itself — so workers can be started in any order, before
any dispatcher exists.

Claiming is typed in the **dispatcher's own Claude Code window**, using the `!`
prefix that runs a command in that session — no second shell, and no
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
([skills/ccd-dispatcher.md](skills/ccd-dispatcher.md) §3).

From then on another dispatcher's `send` to that worker is refused. Your own
`ccd send` from a shell is not — a sender that claims no workers is never
blocked, so you can always reach a worker by hand. `ccd release w1` frees it,
and retiring the dispatcher frees everything it held.

If a dispatcher dies without retiring, its claims survive it — the roster has
no liveness at all. `ccd claim w1 other-disp --force` takes the worker over.

### Sending and receiving

As covered in [Who runs which command](#who-runs-which-command): everything
below is a tool call the dispatcher or worker session makes itself, shown as
console output for readability — not something typed at a shell. The
dispatcher hands `w1` a task with its own `ccd send`:

```console
$ ccd send w1 "Summarise the failure modes in tests/ccd_smoke.sh"
sent (id=m1)
```

`w1`'s parked `recv` returns immediately and prints the sender, then the text:

```
disp: Summarise the failure modes in tests/ccd_smoke.sh
```

The worker does the work, sends the result back to the handle it came from,
and parks again with its own tool calls — that last step is what keeps it
available:

```console
$ ccd send disp "8 cases; only step 4 exercises re-queue-on-disconnect"
sent (id=m2)
$ ccd recv w1 -t 86400
```

Always give `recv` a long timeout. A short one just means falling out and
calling `recv` again for no benefit.

`-f` is optional and a session rarely needs it: `send` defaults the sender to
`$CCD_HANDLE`, which `ccd launch` sets, so a participant's identity travels
without anyone remembering a flag. From a plain shell with no `$CCD_HANDLE`
there is nothing to default to, and the message arrives attributed to `unknown`
— fine for a poke you do not expect an answer to, and `unknown` is a reserved
name the broker refuses as a destination, so a receiver cannot reply into a
queue nobody drains. Pass `-f` when you want a shell send to be answerable.

### Fleet dashboard

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

#### Where the numbers come from

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

#### The JSON model, and where it may not be written

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

#### It cannot do anything

The dashboard is **read-only**: no stop, no retire, no redirect, and its only
broker call is `roster`. Giving a view write power would need an authorization
story and ADR-0006 leaves no principals to write one against. Control stays in
the participant sessions, where a human is already attached — Esc into the
session's own TUI, or `ccd send` it something from a shell.

### Steering a worker by hand

A parked `recv` is an ordinary pending tool call, so **Esc** interrupts it and
gives you the session back — run tools, ask questions, redirect it entirely.

Nothing is lost. If the broker had reserved a message for that call, it detects
the dropped connection and re-queues it at the **front** of the queue. When you
want the worker waiting again, tell it to `ccd recv` once more: a re-queued
message returns instantly, otherwise it parks.

### Shutting down

```console
$ ccd ret w1
retired w1
```

Retire before ending a session, or the roster keeps advertising a handle that no
longer answers. A dispatcher sending to a retired handle gets no error — the
message simply queues for a session that will never collect it.

### Troubleshooting

| symptom | cause |
|---|---|
| `ccd recv: no handle given and $CCD_HANDLE not set` (exit 2) | Identity not in the environment. If the session was started with `claude --bg`, that is why — see above. |
| Messages sent but never received; both sides look healthy | Two brokers. Check `CCD_SOCKET` on **both** sides resolves to the same path. |
| A worker never replies, and `ccd send` reported `sent` | `sent` means accepted, not delivered. Run `ccd ls`: a warning on stderr names any handle with messages queued and no session behind it. |
| `ccd ls` warns about a handle you do not recognise | A typo'd destination, or a session that retired with work still queued. `ccd recv <that-handle> -t 1` drains one message so you can see what it was. |
| `ccd ls` is empty but workers are running | The broker restarted. Queues and the roster are in-memory only, and neither side is told. Every participant must re-announce. |
| `'w1' is claimed by 'disp'` on send | Another dispatcher holds that worker. `ccd release w1`, or `ccd claim w1 <you> --force`. |
| A worker is stuck claimed by a dispatcher that no longer exists | Expected — claims have no liveness. Force the claim over. |
| `handle 'w1' is already announced at effort …` | A live handle, different effort. Retire it, or pass a different handle. Re-announcing the *same* effort is allowed, so Esc-interrupt recovery still works. |
| A task arrives twice | Known, unexplained — see issue #3. Every re-queue is logged to the broker's stderr with its reason; the log sits next to the pidfile. |
| `ccd dashboard` shows `unknown` status and no cost for a handle | That session announced no working directory or session id — announced by hand from a shell, or running on a backend that keeps no Claude Code transcript. Nothing is broken; there is simply nothing to read. |
| `ccd dashboard --write` refuses the path | It is inside a git working tree, deliberately and without an override. Write it to a state directory instead. |
| `'unknown' is not a handle` on send | The message you are replying to arrived from a sender that claimed nothing, so there is no address to answer. Reply to a handle named in the message text, or ask the human who sent it. |
| A worker's reply never reaches the dispatcher | Before v1.8.0 a `send` without `-f` arrived as `unknown` and the reply queued there silently. Both hosts run 1.8.0 or later; if you see this, check the versions agree. |
| A participant never loads its skill | It is installed as a flat `.md` instead of `ccd-worker/SKILL.md` or `ccd-dispatcher/SKILL.md`. |
| A fresh session invents a task instead of announcing | It loaded a skill older than #27, which described handling tasks without saying none had been given. Both skills now open by saying so. |
| `ccd broker start` says it is already running | A live socket exists. `ccd broker status`, and check for a stray second broker. |

### Verifying the zero-token claim

The claim that a parked worker costs nothing is worth checking on your own
setup rather than taking on trust:

1. Park a worker in `ccd recv` and leave it for a minute or so.
2. `ccd send` it something trivial; it wakes and replies.
3. Account the session's token spend for that window.

**Pass:** spend equals the announce turn plus the wake/reply turn, with nothing
attributable to the wait — no assistant turn is generated while the call blocks.
**Fail:** more than those two turns, meaning the session was generating while
parked.
