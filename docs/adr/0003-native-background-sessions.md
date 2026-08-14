---
status: accepted
---

# Detached execution and blocked-detection: native background sessions, not a tmux/PTY supervisor

Two capabilities decide this project's shape: how a Worker Agent runs detached from
whoever dispatched it, and how the Runner learns that a worker is **blocked awaiting
input** rather than working. The original design answered both with a tmux session
wrapping a pseudo-terminal supervisor that scraped the agent's rendered TUI and injected
keystrokes. We chose native background sessions instead: `claude --bg` for dispatch,
`claude agents --json` for state, and hooks for events.

## Considered options

### A. tmux + PTY supervisor (the original design)

A persistent tmux session per worker; a wrapper owning a pseudo-terminal so the agent
renders its TUI; the wrapper matches patterns in the rendered output and injects
keystrokes to answer dialogs.

*For:* works against any interactive CLI agent, including ones with no scripting surface.
Provider-agnostic. Gives human attach via `tmux attach` for free.

*Against:* the blocked/working distinction is inferred from **pixels**, not stated. The
pattern table is a maintained artifact coupled to a renderer nobody promised to keep
stable — during this design session alone the binary moved 2.1.224 → 2.1.225 → 2.1.232.
Its failure mode is silent and expensive: a blocked worker read as busy stalls forever,
and a mistimed keystroke into a session that was merely thinking can accept something
nobody saw.

### B. Headless `claude -p` with an external retry loop

Run each worker non-interactively and re-invoke it on failure.

*For:* simplest possible model — a process that exits, an exit code, no session state.

*Against:* it discards the property this project exists for. A headless process does not
exist between model calls, so a usage limit terminates it and the only recovery is
resume-from-evidence — precisely the `/handoff` behaviour we set out to improve on. It
also gives no live introspection.

### C. Native background sessions — **chosen**

`claude --bg` dispatches detached; `claude agents --json` reports state; hook events carry
typed notifications; `claude attach` gives live human introspection.

*For:* every signal is **stated rather than inferred**. A spike
([docs/spike-native-bg.md](../spike-native-bg.md), 2026-08-14) verified: dispatch returns
immediately and the worker outlives its dispatching shell; `agents --json` needs no TTY
and distinguishes `busy`, `idle`, `waiting` (blocked) and `null` (settled); a blocked
worker independently emits a hook payload with
`"notification_type": "permission_prompt"`. The session stays resident, so a limit-blocked
worker waits rather than dying — the stay-alive property option B forfeits.

*Against:* it binds this project to one vendor's CLI surface, and to a surface that moves.
Accepted, with mitigations below.

## Why C

Option A was designed to extract, by screen-scraping, information the tool already
publishes as structured data. Once the spike established that `waiting` is a first-class
status and that blocked-detection arrives as a typed event, A's entire justification
disappeared — and what remained was a maintenance liability with a silent failure mode.
Option B was never viable: it trades away the one property that distinguishes this project
from the skill it replaces.

## Consequences

* **A version-coupling risk replaces a rendering-coupling risk.** Smaller and louder, but
  real. The Runner asserts at startup that `agents --json` still carries the fields it
  parses and refuses to start otherwise — a loud refusal at boot rather than a silent
  misparse at 3am. Worker binaries are pinned to an explicit version path.
* **Provider-agnosticism is given up.** A non-Claude agent tool would need option A. The
  Runner treats "how to invoke a worker" as opaque data partly to keep that door open.
* **Live introspection is `claude attach`, not `tmux attach`**, and post-mortem output is
  the transcript JSONL rather than `claude logs`, which requires a daemon that exits five
  seconds after its last client.
* **The Attendant survives as a role, not as a pseudo-terminal.** What it does — unblock a
  worker without a human — is unchanged; how it detects and acts is now events and status.
  Its design still waits on one unanswered question: what a usage limit actually emits.
