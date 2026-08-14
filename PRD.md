# Product Design Record: Claude Code Delegation

**Target environment:** Debian 13 (Trixie), no container in the MVP
**Core focus:** durable delegation of long-running agent work, limit survival, cost-aware routing
**Vocabulary:** see [CONTEXT.md](CONTEXT.md) — terms below are used in their glossary sense
**Evidence base:** [docs/spike-native-bg.md](docs/spike-native-bg.md) (2026-08-14, binary 2.1.232)

## 1. Problem

Long-running delegated work fails in two ways that the `/handoff` skill cannot fix from
inside a session:

* **A usage limit stalls the run until a human presses a key.** Hours are lost to an
  agent sitting at a dialog nobody is watching.
* **Delegated work is invisible while it runs**, and its result lands in the delegating
  agent's context whether wanted or not.

`/handoff` mitigates the first by *resuming from evidence*: it rescues uncommitted work
and respawns. It has to, because a Task-tool Subagent does not exist between model
calls — there is no process to keep alive.

**A resident session is different: it survives the limit and waits.** The whole point of
this project is to convert a resume-from-evidence problem into a stay-alive problem.

Cost is the second driver. Measured in `hosting#49`, delegated work is **60–70% of output
tokens** — so routing *worker* traffic to a cheaper backend captures most of the spend.

## 2. Requirements

* **Context isolation.** A Worker Agent's reasoning and intermediate output must never
  enter the scheduling party's context. Enforced by *filesystem layout* — the Runner is
  not given paths it must not read — never by instructing an agent not to look.
* **Limit resilience.** A Worker Agent blocked awaiting input must be detected and
  unblocked without a human.
* **Live introspection.** A human must be able to attach to a running Worker Agent,
  watch it, intervene, and detach without disrupting it.
* **Asynchronous dispatch.** Dispatch returns immediately.
* **Restartability.** The Runner must reconstruct full state after a crash from durable
  sources alone. No in-memory state is authoritative.
* **Configurable invocation.** A Plan selects a Profile and a Tier per phase.
* **Routing attestation.** The model that actually served a phase must be verified
  against what the Plan asked for.

## 3. Architecture

Four parties, one of which deliberately has no model.

| Party | Model | Role |
|---|---|---|
| **Planner** | capable, interactive | Writes the Plan. Not part of the MVP runtime. |
| **Runner** | **none** | Executes the Plan: dispatch, poll, branch on Verdict, checkpoint, reap. |
| **Worker Agent** | per phase | `claude --bg`, one per phase, in its own git worktree. |
| **Reviewer** | capable | A Worker Agent that judges another's output. |

The Runner is a program, not an agent — see [ADR-0001](docs/adr/0001-runner-is-a-program-not-an-agent.md).
It has no context window to exhaust, no usage limit, and no capacity to improvise.

**There is no container, no tmux, and no pseudo-terminal.** The spike established that
`claude --bg` already provides detached dispatch, `claude agents --json` provides
TTY-free machine state, `claude attach` provides live introspection, and blocked-detection
arrives as a *typed* hook event rather than a screen-scrape — see
[ADR-0003](docs/adr/0003-native-background-sessions.md). Containment is delegated to
Freigang — see [ADR-0002](docs/adr/0002-no-container-in-the-mvp.md).

### Worker state

`claude agents --json` reports `busy` (working), `idle` (finished, session alive),
`waiting` (**blocked on a prompt**), `null` (settled). `waiting` versus `idle` separates
*blocked* from *done* without touching a terminal.

**Polling is authoritative; hooks are the fast path and the audit trail.** A crashed
Runner rebuilds state from one `agents --json` call, whereas a missed hook event is gone
forever. The recoverable channel is always the authoritative one.

### The Verdict contract

A Reviewer writes two artifacts: a prose review, and a one-line Verdict —
`PASS` / `FAIL <reason>` / `BLOCKED <reason>`. The Runner matches the Verdict without
comprehending it. **The prose lives in a file whose path the Runner is never given.**
On a retry the Runner passes the review's *path* into the retried worker's prompt; it
never summarises what it cannot judge.

## 4. Execution flow

1. **Dispatch** — one phase, one worktree, one `claude --bg` under the phase's Profile.
   *Dispatch exit 0 does not mean the worker started* (see §7); a liveness check follows.
2. **Poll** — `agents --json` in the phase's Profile namespace until `idle` or `waiting`.
3. **Attest** — assert the transcript's served models match the Plan's Tier. A mismatch
   is a `BLOCKED` verdict: the work may be fine, but the routing promise broke, and that
   is a spend decision for a human.
4. **Review** — for phases marked for it, dispatch a Reviewer and branch on its Verdict.
5. **Checkpoint** — post the outcome to the GitHub issue.
6. **On FAIL** — one retry with the review path in the prompt, then stop and wait for a
   human. A second failure means the task was mis-specified, which the Runner cannot fix.

**State lives in the GitHub issue.** It survives the Runner dying, the host rebooting,
and the laptop closing. The Plan is a YAML fence inside the plan comment: one artifact,
two audiences — human-readable in the issue, machine-parseable by the Runner.

## 5. Model routing

Realises the **per-subagent** routing unit of `hosting#88`, whose other units
(per-session ergonomics, budget guardrails) remain that issue's concern.

* A **Plan names a Tier on a Profile** (`sonnet` on `glm`), never a raw model string.
  Slots get retuned — `hosting#49` already did it once — and plans naming models rot with
  them.
* Tier vocabulary reuses `fable`/`opus`/`sonnet`/`haiku`, because `claude-glm` already
  exports `ANTHROPIC_DEFAULT_*_MODEL` so those aliases resolve to GLM models. The
  translation layer exists; a neutral vocabulary would only undo it.
* **A Profile is a namespace, not a setting.** Session registries are scoped per
  `CLAUDE_CONFIG_DIR` — verified disjoint, 5 sessions against 2, zero overlap. Every
  dispatch, poll, stop and attach carries its Profile's environment.
* The Runner treats a Profile as an **opaque "how to invoke" record**. It never
  interprets the fields. If `hosting#88` later adopts a routing proxy, `profiles.yaml`
  is rewritten and nothing else is.
* **Concurrency caps apply per Quota Pool**, not globally: one worker on the Anthropic
  subscription pool, N on OpenRouter. Two workers on one pool exhaust it twice as fast
  and then block together; two workers on different pools are genuinely independent.
  Quota pools are the only real parallelism available.
* **No automatic cross-Profile substitution.** A phase may declare that it tolerates it;
  otherwise the Runner waits. `hosting#69` documents a silent opus-fallback leak — silent
  cross-provider substitution is a known hazard here, not a hypothetical one.

## 6. Security and resource management

* **Isolation is Freigang's job.** This repo builds no containment. Blast radius in the
  MVP is controlled by one git worktree per Worker Agent.
* **Credentials are untouched.** The daemon refreshes auth centrally; three concurrent
  workers left `.credentials.json` byte-identical.
* **Reaping.** Sessions persist at `idle` after finishing and must be stopped explicitly
  once their artifacts are consumed.
* **Version drift is a live hazard.** The binary moved 2.1.224 → 2.1.232 during the
  design session. The Runner asserts at startup that `agents --json` still carries the
  fields it parses, and refuses to start otherwise — a loud refusal at boot instead of a
  silent misparse at 3am.

## 7. Known traps

Each of these is a bug the Runner would otherwise ship with — all observed in the spike:

1. **Dispatch exit 0 ≠ worker started.** A dispatch printed the full success banner, then
   `crashed: exit 1 before init — error: unknown option '--session-name'`.
2. **Two different session ids.** `agents --json` returns a UUID; `stop`/`logs`/`attach`
   accept only the **8-char prefix**.
3. **`claude logs` needs a live daemon**, which exits 5s after its last client. Post-mortems
   read the transcript instead.
4. **Background sessions appear only under `agents --json --all`.**
5. **`-n/--name`**, not `--session-name`; otherwise `name` is the entire prompt text.

## 8. Open questions

* **What a usage limit actually emits.** No `usage_limit` value exists among the
  notification-type identifiers. A tripwire (`~/.claude/limit-probe.log`) captures the
  next natural limit. **The Attendant cannot be designed until this is answered.**
* **Whether workers survive a daemon restart.** Workers outlived their dispatching shell;
  killing the daemon under load was not tested.
* **Budget guardrail interaction.** If `hosting#88` lands a layer that downgrades or
  refuses a dispatch, that is a new blocked state.

## 9. MVP acceptance

One real multi-phase issue runs to completion unattended, **across at least one usage-limit
reset**, with every phase checkpointed to the issue and at least one Reviewer verdict acted
on. Surviving the limit is the product; a run that never meets one proves nothing.
