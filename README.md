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

Design record: [`docs/adr/`](docs/adr/) (the load-bearing decisions, one per
file — start here if you are wondering *why* something looks the way it does),
[`PLAN-ccd-v2.md`](PLAN-ccd-v2.md) (the full implementing design — architecture,
wire protocol, and the rationale for every decision below) and
[`PRD.md`](PRD.md) (the original product requirements).

## What this repo is not

No host-specific paths, credentials, handles, or launcher names live here.
This is the generic, public delegation model only. Wiring it onto any
particular machine (which launcher starts a worker, which model/effort slot
it gets, systemd units, ansible roles, personal handles) is a deployment
concern that belongs outside this repo.

The author's own deployment lives in a separate private, ansible-based
layer, and this is roughly the shape a comparable one takes — useful as a
sketch if you are building your own, since nothing here depends on it:

- a role that installs a copy of `ccd_broker/` + the `ccd` CLI onto a host and
  runs the broker as a systemd `--user` unit, with `CCD_SOCKET` pinned to an
  explicit path rather than left to `${XDG_RUNTIME_DIR:-/tmp}` (a Claude Code
  Bash-tool subshell does not reliably inherit `XDG_RUNTIME_DIR`, and the
  fallback quietly starts a *second* broker that neither side reports as an
  error);
- a role that installs `skills/ccd-worker.md` as
  `~/.claude/skills/ccd-worker/SKILL.md` — the loader scans for a directory
  containing `SKILL.md`, and silently ignores a flat `.md` file;
- a thin launch wrapper that derives the handle once, maps one model-slot name
  onto *both* the `CCD_MODEL`/`CCD_EFFORT` it exports and the
  `--model`/`--effort` it passes through, reserves the handle with `ccd
  announce --exclusive`, and then execs whichever backend launcher was named,
  so backends stay orthogonal to ccd. Deriving both pairs from one input is
  the point: hand-matching them is how a session ends up running an effort it
  never declared, or a launch failing in the backend's own words instead of
  ccd's (issue #10). `CCD_MODEL` (the model-slot name) stays a real
  environment variable this wrapper exports and the worker skill's own
  self-check compares against, but it does not reach the roster —
  `announce`/`ccd ls` carry `effort` (and, once a manifest exists, a resolved
  `model` and a `mapping` id) rather than a slot name, since a live probe
  found the slot itself added nothing a resolved model doesn't already say
  (claude-code-delegation#13). See [`USAGE.md`](USAGE.md), *What a launch
  wrapper should do for you*.

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

One distribution, three console scripts:

```bash
uv tool install git+<this-repo>@<tag>    # ccd, ccd-broker, ccd-dashboard
```

Installing a copy of the files onto a host by hand is what this replaces. A
packaged install carries its own contents and cannot be half a version — the
file-copy approach once put a wrapper on a host from a checkout too old to
contain it, which is the failure that is no longer possible here.

To run from a checkout instead, with nothing installed, `./ccd` at the repo
root is a stub that calls the same code:

```bash
git clone <this-repo> ~/ccd && ~/ccd/ccd ping
```

Requires Python 3.11+ (stdlib only, no third-party dependencies) and a Linux
box with `AF_UNIX`/`SO_PEERCRED` support.

Start/stop the broker through the CLI rather than invoking the module
directly — `ccd broker start` handles backgrounding and the pidfile for you,
and waits for the broker to answer before reporting success:

```bash
ccd broker start     # spawns `ccd-broker`, waits for it to answer ping
ccd ping             # ok (ccd-broker 1.3.0) up 0m
ccd broker status    # up | down
ccd broker stop
```

## Environment variables

| var | used by | default | meaning |
|---|---|---|---|
| `CCD_SOCKET` | `ccd`, `ccd_broker` | `$XDG_RUNTIME_DIR`, else `/run/user/<uid>`, else `/tmp`, as `ccd-<account>.sock` | Unix socket path the broker listens on and the CLI connects to. Created mode `0600`; the broker rejects connections from other uids (`SO_PEERCRED`). |
| `CCD_PIDFILE` | `ccd broker start/stop` | the socket path with `.pid` in place of `.sock` | Where `ccd broker start` records the broker's pid so `ccd broker stop` can find and signal it. The broker's stdout/stderr log goes next to it, at the same path with `.log` in place of `.pid`. |
| `CCD_HANDLE` | `ccd recv`/`announce`/`ret` | *(none — required if `<handle>` isn't passed positionally)* | Default handle for `recv`/`announce`/`ret` so a worker's skill/script doesn't have to hardcode it. |
| `CCD_TRANSCRIPT_ROOT` | `ccd dashboard` | `${CLAUDE_CONFIG_DIR:-~/.claude}/projects` | Where Claude Code keeps per-project transcript directories. The dashboard is the one component that reads them (ADR-0008) — the broker and the rest of the CLI stay backend-agnostic and read no Claude-internal state at all. |
| `CLAUDE_CODE_SESSION_ID` | `ccd announce` | *(set by Claude Code inside a session; empty elsewhere)* | Passed through to the broker so the dashboard can find that session's transcript. Announcing from a plain shell sends nothing and leaves whatever the session already reported. |
| `CCD_MAPPINGS` | `ccd` (manifest reader) | `${XDG_CONFIG_HOME:-~/.config}/ccd/mappings.json` | The mapping manifest — see [The mapping manifest](#the-mapping-manifest). Read only by the CLI; the broker never sees it. Absent is not an error until something asks to pick from it. |
| `CCD_EFFORT` | worker skill (`skills/ccd-worker.md`) convention, not read by `ccd` itself | — | Passed as the `effort` arg to `ccd announce`, so the roster (`ccd ls`) shows other participants which effort each handle carries. Nothing reads it back off the running session, so the `ccd-worker` skill compares it against `$CLAUDE_EFFORT` and warns on a mismatch. Set by whatever launches the session. |
| `CCD_MODEL` | worker skill (`skills/ccd-worker.md`) convention, not read by `ccd` itself | — | The launcher's model-slot name (e.g. `sonnet`). Local only: it does **not** reach `ccd announce` (broker 1.3 dropped the roster's slot field, claude-code-delegation#13 — a resolved model id and a manifest `mapping` id are its replacements, once the manifest and `ccd launch` work exist). Still exported for the `ccd-worker` skill's own self-check, which compares it against the model the session believes itself to be. Set by whatever launches the session. |

The broker itself takes no flags or config file — `$CCD_SOCKET` is its only
configuration surface (`ccd-broker -h` / `python3 -m ccd_broker -h` for the
one-line usage).

## The mapping manifest

A **mapping** is one named launch: which launcher runs, which model it is told
to use, and what effort it requests. A human picks one entry and every other
value — the id, the label — is derived from it, so there is no second place to
state the same fact and no way for two statements of it to disagree
([ADR-0009](docs/adr/0009-a-launch-picks-one-named-mapping.md)).

The manifest is JSON at `${XDG_CONFIG_HOME:-~/.config}/ccd/mappings.json`,
overridable with `$CCD_MAPPINGS`. **This repo ships no manifest** — populating
one is a deployment concern, like everything else in *What this repo is not*.
What is public is the shape, the validation rules, and a reference reader
([`ccd_mappings/`](ccd_mappings/)) that `ccd` calls.

### Name the model, not a slot

An entry names the model **directly**, as the provider writes it, and that
string is passed verbatim to `claude --model`. There is no `slot` field and no
`fable`/`opus`/`sonnet`/`haiku` indirection: a full provider slug works as a
`--model` argument, so the slot was only ever one way to reach a model, and the
direct name reaches any model the provider offers rather than the handful a
launcher happens to have been configured with. [ADR-0009](docs/adr/0009-a-launch-picks-one-named-mapping.md)
records the probe that settled it.

**effort** is a separate field with one value — `low`, `medium`, `high`,
`xhigh` or `max` — and it is **optional**, because for some models it does not
apply at all (see below).

### Effort is requested, not guaranteed — and sometimes not sent

The `effort` in a manifest entry is what will be **asked for**. It is not a
promise about what runs, and nothing in `ccd` pretends otherwise:

- For some models it is **never sent**. Claude Code drops effort when the model
  rejects it and stops sending it on subsequent turns. Every `claude-haiku-4-5`
  transcript on the machine this was checked on records `effort: null` —
  including sessions launched with an explicit `--effort low` and `--effort
  xhigh`. **Omit `effort` for those entries**: one claiming a value would be
  fiction.
- Claude Code emits effort as a thinking budget, and that budget can be
  **silently downgraded** server-side for some models.
- A non-Anthropic backend **reinterprets** it against its own scale. Models
  reached that way generally advertise a `reasoning`/`reasoning_effort`
  control, and a provider may cross-map a token budget onto it — but whether
  an Anthropic-compatible `/v1/messages` hop performs that translation is
  undocumented, and this project does not guess.

Each entry's effort is therefore set **by hand** by whoever renders the
manifest, from their own benchmark reading, and left out where it does not
apply. No component infers it. What a
session *actually* got is a separate fact, read from `$CLAUDE_EFFORT` (which
Claude Code sets per turn, after any downgrade) and compared against the
declared value — requested and observed never collapse into one number.

### Schema

Top level is an object, so the file can be versioned:

| key | type | meaning |
|---|---|---|
| `schema` | integer | Schema version. `1` today. A reader refuses a file newer than it understands rather than guessing. |
| `mappings` | array | The entries, in the order a picker should present them. Must not be empty. |

Each entry:

| key | required | meaning |
|---|---|---|
| `launcher` | yes | A **bare command name**, resolved on `$PATH` at launch. Not a path, not a name with arguments. It must accept Claude Code's own `--model`/`--effort`/`--name` flags, since that is how the picked entry reaches the session. Not derivable from anything else: one model is often reachable through more than one launcher. |
| `model` | yes | The model id, as its provider writes it, passed **verbatim** to `--model` — `claude-sonnet-5`, `moonshotai/kimi-k3`, `deepseek/deepseek-v4.1-flash`. May carry a `[1m]` context suffix, see below. This is also what the roster advertises and what an observed-vs-declared check compares against. |
| `effort` | no | `low` \| `medium` \| `high` \| `xhigh` \| `max`. Requested, not guaranteed — and **omitted entirely** for models that never receive it. |
| `billing` | no | `sub` \| `api` — subscription seat or metered per-token access. **Stored, not derived** (see below); omitted where it does not apply. |
| `notes` | no | Free text for the operator — why this effort, what the benchmark said. Shown as an aside, never as the label. |

That is the whole stored shape. Two keys carry a launch, two qualify it where
they apply, and the last is for the human.

**Why `billing` is stored, unlike everything else optional here.** It names a
fact about the *route*, not the model: the same model can sit behind a
subscription launcher or a metered one, and no model string can yield which
one a given entry uses — only the deployment layer that rendered the
launcher knows that (the same layer this schema already defers to for
launcher names; see [What this repo is not](#what-this-repo-is-not) above).
That is the opposite situation from the retired `slot`: a stored slot
could *contradict* the model beside it, which is issue #10's defect; a stored
`billing` cannot contradict anything here, because nothing here could have
computed it in the first place. hosting's own session-label convention names
it as `sub`/`api` (lowercase), and this schema matches that spelling rather
than inventing a second one.

**Derived, never stored: the `id` and the label.** Both come from `model` and
`effort` only — `billing` plays no part in either, so two entries agreeing on
launcher, model and effort but differing only in billing are still a true
duplicate, not two things for billing to tell apart. See
[The id is derived](#the-id-is-derived) and [The label is derived](#the-label-is-derived).

Unknown keys, at either level, are ignored; **retired keys are refused**.
Those are two halves of one rule, not an exception to it. Ignoring an unknown
key buys *forward* compatibility — an older reader survives a newer producer
that has learned a field. `slot`, `id` and `display` are the backward case:
known-dead keys from an *older* producer, where silence would let a stale
manifest validate clean while the intent written into it is dropped on the
floor. So an entry carrying one is refused, naming the key and saying to
regenerate the file.

### The `[1m]` context suffix

Any model outside Claude Code's own catalog — which is every third-party slug —
makes the session assume a **200k context window** for auto-compaction,
however it was reached. Appending `[1m]` to the model name asks for 1M
instead, and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` sets an exact value.

Because `model` is passed verbatim, the suffix belongs in that string rather
than in a field of its own:

```
{ "launcher": "claude-openrouter", "model": "moonshotai/kimi-k3[1m]", "effort": "max" }
```

Omitting it is silent: nothing fails, long sessions simply compact early
against a window smaller than the model actually has. The suffix also survives
into the derived id (`kimi-k3-1m-max`), so an entry with it and one without are
distinct mappings rather than a collision.

### The id is derived

An entry's id comes from its **model and effort** — `kimi-k3-max`,
`deepseek-v4.1-flash` for an entry with no effort — lowercased into one
shell-safe word, since it becomes `$CCD_MAPPING` in the launched session.
There is no `id` key: a hand-written one can say `kimi-k3-max` on an entry
running `high`, which is the same defect as a hand-written label.

Where two entries reach the same model at the same effort through **different
launchers**, both ids carry their launcher (`kimi-k3-max-claude-openrouter`).
Both, not just the second — so reordering the file cannot rename an entry.
Two entries agreeing on launcher, model *and* effort are a true duplicate, and
that is a validation error.

### The label is derived

A picker shows each entry as a label, computed from `model` and `effort` by
`ccd_mappings.labels()` — there is no stored field for it and no override. A
stored label is free text that can disagree with the fields beside it, which is
issue #10's defect one level down; deriving it makes that disagreement
unrepresentable rather than merely discouraged. One function means the picker
and any other renderer cannot diverge either.

| entry | label |
|---|---|
| model `moonshotai/kimi-k3`, effort `max` | `kimi-k3/max` |
| model `deepseek/deepseek-v4.1-flash`, no effort | `deepseek-v4.1-flash` |
| model `claude-sonnet-5`, effort `medium` | `claude-sonnet-5/medium` |

**Model ids are shown as their provider writes them**, shortened to the last
path segment and otherwise untouched. Restyling them would need no table but
would invent a name — `glm-5.3-flash` is not `Glm-5.3-Flash` to anyone — and
the label would stop matching the string a reader meets everywhere else: the
manifest's own `model`, `ccd ls`, the transcript. Rendering it verbatim keeps
the label greppable and needs no knowledge of model families.

An entry with no effort shows the model alone, which is the honest rendering:
there is no value to display because none is sent.

**Labels disambiguate exactly as ids do.** A label exists so a human can choose
from it, so two identical rows in a picker mean the choice cannot be made from
the label at all. Where entries share a label, each member of that group
carries its launcher — all of them, so file order cannot change what an entry
is called, and the label stays in lockstep with the id:

```
kimi-k3/max                            kimi-k3-max                      (alone)
kimi-k3/max (claude-openrouter)        kimi-k3-max-claude-openrouter    (colliding)
kimi-k3/max (claude-alt)               kimi-k3-max-claude-alt           (colliding)
```

Appending the launcher *always* was rejected: it would make every label
noisier — `claude-sonnet-5/medium (claude)` — to fix a case that usually does
not arise. That is why the label is derived over the whole file rather than
from one entry; the id already needs the file for the same reason.

If an entry needs a human aside, that is what `notes` is for — and `notes` is
visibly not the label, which is the difference that matters.

### Worked example

Three first-party entries (bare `claude`, billed as a subscription seat), one
of them with no effort because its model never receives one, and two reached
through a remapped launcher billed per token — one carrying the `[1m]`
suffix. `claude-openrouter` here is an illustrative name only; real launcher
names live in the deployment layer, not in this repo:

```json
{
  "schema": 1,
  "mappings": [
    {
      "launcher": "claude",
      "model": "claude-sonnet-5",
      "effort": "medium",
      "billing": "sub"
    },
    {
      "launcher": "claude",
      "model": "claude-opus-5",
      "effort": "high",
      "billing": "sub"
    },
    {
      "launcher": "claude",
      "model": "claude-haiku-4-5",
      "billing": "sub",
      "notes": "no effort: this model never receives one, so claiming a value would be fiction"
    },
    {
      "launcher": "claude-openrouter",
      "model": "moonshotai/kimi-k3[1m]",
      "effort": "max",
      "billing": "api",
      "notes": "effort set by hand from benchmark reading; [1m] lifts the assumed 200k window"
    },
    {
      "launcher": "claude-openrouter",
      "model": "deepseek/deepseek-v4.1-flash",
      "billing": "api"
    }
  ]
}
```

Nothing in that file states an id or a label; both are derived, and neither
shows `billing` — it identifies the route, not the model. A picker
renders it as:

```
1. claude-sonnet-5/medium
2. claude-opus-5/high
3. claude-haiku-4-5
4. kimi-k3[1m]/max
5. deepseek-v4.1-flash
```

with ids `claude-sonnet-5-medium`, `claude-opus-5-high`, `claude-haiku-4-5`,
`kimi-k3-1m-max` and `deepseek-v4.1-flash`.

### Validation, and where it lives

**In the reader, never in the broker.** The broker treats roster text as opaque
and learns no Claude Code vocabulary ([ADR-0004](docs/adr/0004-one-transport-behind-a-seam.md)),
which is what lets it stay agnostic about backends that do not exist yet.
`ccd` checks, when it loads the file:

- `launcher` and `model` are present and non-empty; `launcher` is a bare name,
  with no path separator and no leading `-`.
- `effort`, when present, is one of the five, case-sensitive. Absent is fine.
- `notes` is a string if given; no entry carries a retired key (`slot`, `id`,
  `display`).
- every `model` yields a usable id, and no two entries derive the same one —
  which, since ids are derived, means no two entries agree on launcher, model
  and effort.
- `schema` is an integer no newer than the reader.

Every problem is reported at once, not just the first, because a
machine-rendered file is fixed by regenerating it, not by a round trip per
fault. Two things are deliberately *not* load-time errors: a **missing** file
(that is "not set up yet", distinct from "set up wrong", and only matters when
something asks to pick from it), and a launcher that is **not installed** —
shape is host-independent, so availability is resolved at launch instead, which
lets a controller validate a manifest it renders for a host whose launchers it
does not have.

## Quick usage example

For the full operating walkthrough — starting participants, the dispatcher/worker
loop end to end, steering by hand, and troubleshooting — see
[`USAGE.md`](USAGE.md). The example below is the minimum that proves the wiring.

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
w1      medium  -       -       -       -
$ ccd ret w1
retired w1
$ ccd ls
(no workers announced)
```

`ccd ls`'s columns are handle/effort/owner/model/pid/status, then a trailing
drift marker (empty here — nothing to compare against without a transcript).
The `sonnet` in `announce`'s own confirmation line is cosmetic only: that
positional does not reach the roster (see the `CCD_MODEL` row above).

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
ccd claim <worker> [<dispatcher>] [--force]   ($CCD_HANDLE is the dispatcher)
ccd release <worker> [--force]
ccd ls
ccd dashboard [--scope <handle>] [--json] [--write <path>] [--rates <file>]
ccd ping
ccd broker start|stop|status
```

A worker announces **unowned**; a dispatcher `claim`s it, exclusively. The
broker then refuses another *dispatcher's* `send` to that worker — but never
refuses a sender that claims nothing, so you can always reach any worker by
hand from a third shell. Retiring a dispatcher releases everything it held.
Sender identity is self-asserted, so this stops a confused dispatcher, not a
dishonest one: see
[ADR-0007](docs/adr/0007-affiliation-is-claimed-not-declared.md) and
[ADR-0006](docs/adr/0006-one-boundary-uid-authenticates-claims-authorize.md).

`ccd dashboard` is the read-only fleet view: metadata (handle, role, model,
effort, claim graph, working tree, status, cost) for **every** announced handle,
and one bounded content line — a dispatcher's goal, a worker's last task — for
the **one** handle named by `--scope`, so no rendered view ever holds two clients'
working material. Content and cost come from each session's own transcript,
never from the broker, which stores none
([ADR-0008](docs/adr/0008-dashboard-is-metadata-wide-content-scoped.md)). See
[USAGE.md](USAGE.md#fleet-dashboard).

Full protocol semantics (wire format, blocking/dequeue-on-ack, the
`Transport` seam) are documented in `ccd_broker/broker.py` and
`ccd_broker/transport_uds.py`, and summarized in
[`PLAN-ccd-v2.md`](PLAN-ccd-v2.md) §5.

## Worker/dispatcher skill and tests

- [`skills/ccd-worker.md`](skills/ccd-worker.md) — the Claude Code skill a
  worker (or a dispatcher, same shape) loads: announce on start, check the
  declared effort (and, on a bare launch, the model) against what is
  actually running, `ccd recv` as the last tool call every turn,
  reply-then-recv-again, retire on exit, and the Esc-interrupt note above.
- [`tests/ccd_smoke.sh`](tests/ccd_smoke.sh) — an end-to-end smoke test
  against a private, throwaway broker instance. Run it with
  `tests/ccd_smoke.sh`.
- [`tests/test_affiliation.py`](tests/test_affiliation.py),
  [`tests/test_deliver_ack.py`](tests/test_deliver_ack.py),
  [`tests/test_dashboard.py`](tests/test_dashboard.py),
  [`tests/test_mappings.py`](tests/test_mappings.py) — claim-based
  affiliation, dequeue-on-ack, the dashboard, and the mapping manifest
  contract. Each is a plain script with no test framework; run it directly.
  They define no `test_*` functions, so a `pytest` invocation collects nothing
  and passes quietly.
