# claude-code-delegation

Delegate long-running work to supervised, independently-running agent processes — and
keep the run alive across usage limits without a human at the keyboard.

## The problem

Two failures make long agent runs unreliable:

* **A usage limit stalls the run** until someone presses a key.
* **Delegated work is invisible while it runs**, and its output lands in the delegating
  agent's context whether wanted or not.

The existing `/handoff` skill mitigates the first by *resuming from evidence* — rescuing
uncommitted work and respawning. It has to, because an in-process subagent does not exist
between model calls; there is no process to keep alive.

**A resident session is different: it survives the limit and waits.** Converting a
resume-from-evidence problem into a stay-alive problem is what this project is for.

## The shape

A **Planner** writes a Plan. A **Runner** — deliberately a program with no model — executes
it: dispatch, poll, branch on a verdict, checkpoint, reap. **Worker Agents** run one phase
each in their own git worktree. **Reviewers** are Worker Agents that judge other workers'
output, emitting prose for humans and a one-line verdict for the Runner.

The Runner never reads worker output. That is enforced by which paths it is given, not by
telling an agent what not to look at.

## Where to start

| | |
|---|---|
| [CONTEXT.md](CONTEXT.md) | The vocabulary. Read this first — the terms are used precisely. |
| [PRD.md](PRD.md) | The design: architecture, execution flow, model routing, open questions. |
| [docs/spike-native-bg.md](docs/spike-native-bg.md) | The evidence the design rests on, WORKS/DOES-NOT-WORK with pasted output. |
| [docs/adr/](docs/adr/) | Why it looks like this, including the approaches rejected. |

## Status

**Superseded — retained as a design record, deliberately not merged.**

This branch is the project's first design track (2026-08-07 → 2026-08-20). On 2026-08-29
the repo restarted on `main` from a fresh root commit with a different architecture —
`ccd`: a small socket broker over which *interactive*, human-attachable sessions on
different backends hand each other tasks. See [`PLAN-ccd-v2.md`](../../blob/main/PLAN-ccd-v2.md)
on `main`. The two histories share no merge base and the designs contradict each other,
so there is nothing here to merge.

`main`'s `PRD.md` is this branch's `PRD.md` pruned from 310 lines to 92, with the cuts
marked by inline *(Removed: …)* notes. Everything cut — the Runner/Planner/Attendant
model, the Verdict grammar, the quota-pool reasoning, the ADRs — survives only here.

What is still worth reading, independent of which design won:

* [`docs/spike-native-bg.md`](docs/spike-native-bg.md) — what `claude --bg`,
  `claude agents --json` and hooks actually provide, WORKS/DOES-NOT-WORK with pasted
  output (binary `2.1.232`).
* [`docs/spike-auto-continue.md`](docs/spike-auto-continue.md) — why a `--bg` worker
  **never** arms native `autoContinueAtUsageLimit` (binary `2.1.237`). This one is
  load-bearing for `main`'s design too: it is why `ccd` participants must be started as
  ordinary interactive or tmux sessions rather than with `--bg`.

**Both spikes are version-stale.** They were run against `2.1.232`/`2.1.237`; the binary
has since moved roughly thirty releases. ADR-0003 accepted that version-coupling risk on
the strength of a startup assertion that the Runner would make — never implemented,
because implementation never started. Re-run the spikes before relying on either.

The design's own open questions (PRD §8) were never closed: what a *real* usage limit
emits to a `--bg` worker (the `docs/spike-auto-continue.md` result came from a
token-auth mock and may be an artefact of it), whether workers survive a daemon restart,
and how a budget-guardrail layer would interact.

## Lineage

The first design followed [OnamSharma/claude-supervisor](https://github.com/OnamSharma/claude-supervisor)
and `claude-code-tmux`: a container holding tmux sessions, each wrapping a pseudo-terminal
supervisor that scraped the agent's TUI and injected keystrokes to answer dialogs.

**That approach was tested and dropped.** A spike found that the CLI already publishes as
structured data what the supervisor was going to screen-scrape — including a `waiting`
status distinct from `idle`, and typed hook notifications for blocked workers. Scraping a
renderer nobody promised to keep stable would have been a maintenance liability with a
silent failure mode; the binary moved three versions during the design session alone.

The reasoning, and the options weighed, are in
[ADR-0003](docs/adr/0003-native-background-sessions.md) and
[ADR-0002](docs/adr/0002-no-container-in-the-mvp.md). The ideas those projects contributed
— detached supervision, autonomous limit handling, human attach — all survived; only the
mechanism changed.
