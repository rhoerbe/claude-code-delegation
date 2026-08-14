---
status: accepted
---

# The Runner is a program, not an agent

In a project about agent delegation, the obvious design makes the orchestrator an agent —
a cheap model that reads worker output, judges it, and decides what to run next. We
deliberately did the opposite: the Runner has **no model at all**. Every step it performs
is deterministic (read a Plan, dispatch, poll a status field, match a Verdict token, post
a checkpoint, retry once, stop), and the one step that genuinely needs capability —
judging a worker's output — is delegated to a Reviewer worker.

## Why

The trigger was an observed failure: the `/handoff` orchestrator accumulated context until
it became the bottleneck it was meant to prevent. Diagnosing it produced a constraint —
*the scheduling party must never read worker output* — and that constraint, followed
honestly, specifies a program rather than an agent. An orchestrator with no context window
cannot exhaust one, cannot hit a usage limit, and cannot improvise something nobody asked
for.

This resolves an apparent regress. If a Reviewer judges a worker, something must read the
Reviewer's judgement — which looks like it puts comprehension back in the loop. It doesn't,
because a Reviewer emits **two** artifacts: prose for humans and for a retrying worker, and
a one-line Verdict (`PASS` / `FAIL <reason>` / `BLOCKED <reason>`) for the Runner to branch
on without understanding it. On a retry the Runner passes the review's *path* into the next
worker's prompt rather than summarising it.

## Consequences

* **Isolation is enforced by filesystem layout, never by prompt.** The prose review lives
  in a file whose path the Runner is not given. An agent told "do not read this" eventually
  will, especially after a compaction; a program not given a path cannot.
* **The Verdict grammar is now load-bearing.** Every Reviewer must emit it, forever.
  Changing it later means revisiting every worker prompt — which is what makes this
  decision expensive to reverse and worth recording.
* **The Plan must carry ready-to-dispatch prompts.** There is nobody at run time to turn a
  phase description into a prompt.
* **The Runner can be hosted by systemd rather than a session**, gaining restart-on-crash
  and boot persistence, and removing the irony of an orchestrator that dies of the same
  usage limit it exists to survive.
