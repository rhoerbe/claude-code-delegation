# Context: Claude Code Delegation

Ubiquitous language for this project. Glossary only — no implementation detail.

## Worker Agent

A complete, independently-invoked coding-agent process running one delegated task
in its own detached terminal session, with its own context window.

A Worker Agent is **not** a [Subagent](#subagent). It is a separate OS process with
separate credentials-scoped state, addressable and attachable by a human while it runs.

A Worker Agent never schedules other Worker Agents. Dispatch is the
[Supervisor Agent](#supervisor-agent)'s job alone.

## Supervisor Agent

The agent that decides *which* Worker Agents to start, with what prompt, model and
effort, and in what order. Runs the delegation skill.

Deliberately a cheap model tier (haiku/sonnet): it is a **scheduler, not a reviewer**.
The Supervisor Agent does not read, judge, or verify a Worker Agent's output. When
output must be judged, the Supervisor Agent dispatches a further Worker Agent to do
the judging and acts only on that worker's verdict.

This is the defining constraint of the design: the Supervisor Agent's context window
must never fill with worker output, because a Supervisor Agent that runs out of
context stops scheduling.

## Subagent

Reserved for Claude Code's *native* Task-tool subagent: an in-process agent sharing
the parent's process, credentials and session, whose result is returned directly into
the parent's context.

Never use "subagent" for a [Worker Agent](#worker-agent).

## Runner

The deterministic loop that carries out the [Plan](#plan): dispatches Worker Agents,
waits, matches a [Verdict](#verdict), posts checkpoints, reaps sessions.

The Runner has **no model**. It is the mechanical half of what the
[Supervisor Agent](#supervisor-agent) was originally imagined to be, and having no
model is the point: it has no context window to exhaust, no usage limit to hit, and
no capacity to improvise something nobody asked for.

## Reviewer

A [Worker Agent](#worker-agent) whose task is to judge another Worker Agent's output.
Always a capable model tier — judging is the step that genuinely needs capability.

A Reviewer writes **two** artifacts: a prose review for humans and for a retrying
worker, and a one-line [Verdict](#verdict) for the [Runner](#runner).

## Verdict

The single line a [Reviewer](#reviewer) emits for the [Runner](#runner) to branch on:
`PASS`, `FAIL <reason>`, or `BLOCKED <reason>`. Fixed grammar, matched without being
understood.

The prose review lives in a *separate file whose path the Runner is never given*.
This is the project's standing rule: **isolation is a property of the filesystem
layout, never of a prompt.** An agent told "do not read this" eventually will.

## Attendant

The component that detects a Worker Agent being *blocked awaiting input* — a usage
limit, a permission prompt, an expired login — and unblocks it without a human.

Named for what it does, not how. It is not an agent: no model, no reasoning. Whether
it drives a pseudo-terminal or subscribes to harness events is an implementation
question, deliberately not fixed by this definition.

## Profile

A named way of invoking the agent tool: which launcher, which credentials, which
config directory, and therefore which models the ordinary tier names resolve to.

A Profile is **a namespace, not merely a setting**. Session registries are scoped per
config directory, so a session id is only meaningful *within* its Profile — every
dispatch, poll, stop and attach carries one.

## Quota Pool

The allowance a [Profile](#profile) draws down. Profiles sharing a Pool contend with
each other; Profiles in different Pools do not.

The Pool — not the worker count — is the unit of concurrency limits, and it is the
only real axis of parallelism available: two workers on one Pool exhaust it twice as
fast and then block together, whereas two workers on different Pools are genuinely
independent.

## Tier

The capability slot a phase asks for — `fable`, `opus`, `sonnet`, `haiku` — resolved
to a concrete model by the [Profile](#profile).

A [Plan](#plan) names a Tier, never a concrete model. Model slots get retuned; plans
that named a model would rot with them.

## Plan

The machine-readable schedule the [Runner](#runner) executes: an ordered set of
phases, each with a prompt, model, effort, and whether its output requires a
[Reviewer](#reviewer).

Authored by a capable model or a human — never by the Runner. The Plan is what makes
a crashed Runner restartable, and it is the contract between the party that thinks
and the party that executes.

A phase may also declare that it *tolerates substitution* — that running it on a
different [Profile](#profile) is acceptable if its own is exhausted. That permission
is granted at planning time and never inferred at run time: judging whether a weaker
model will do is exactly the judgement the [Runner](#runner) does not have.
