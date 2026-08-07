# Product Design Record: AI Agent Delegation

**Status:** design record. The implementing design is [`PLAN-ccd-v2.md`](PLAN-ccd-v2.md)
(option 2: direct-block, socket-brokered). This PDR captures the requirements and
original architecture rationale for the delegation pattern; parts rendered
obsolete by current Claude Code behavior have been **removed** (see the inline
"Removed" notes — the spending-limit-stuck behavior and the PTY machinery it
spawned are moot now).

## 1. Context & Executive Summary

Using Claude Code for long-running tasks with subagents suffers from one issue
that cannot be remedied in the native tool alone: **subagent output is not
visible** to the operator in real time. This PDR addresses supervised,
human-steerable delegation across agent instances — an operator can attach,
monitor, and intervene, and a delegating agent can dispatch a sub-task and
return to idle or parallel work.

*(Removed: the earlier "arriving at a spending limit where the agent gets stuck
until manual intervention" motivation — this no longer happens in current Claude
Code, so the machinery built to cope with it is moot.)*

## 2. System Requirements

* **Context Isolation:** A delegated task's intermediate output must not flood
  the delegating agent's context window. (In the implementing design this is a
  property of the transport — the worker's text is never streamed into the
  dispatcher's context — not of a namespace or container.)
* **Live Introspection:** Human operators must be able to attach to, monitor, and
  detach from any running agent session in real time via standard terminals
  without disrupting execution.
* **Asynchronous Dispatch:** The delegating agent must be able to trigger a
  sub-task and immediately return to an idle state or pursue parallel tasks.
* **Standardized IPC:** Inter-process communication between the delegating agent
  and workers must rely on standardized, asynchronous message handoffs rather
  than direct stream piping.
* **Configurable Invocation:** The orchestrating agent may choose from a
  predefined set of invocations with parameters: invocation script (launcher),
  model tier, and effort.

*(Removed: "Rate-Limit Resilience" — the autonomous spend-limit resume
requirement. Moot; see §1.)*

## 3. Architecture

The architecture relies on a nested execution model, abstracting the interactive
TUI requirements of the agent away from the parent orchestrator.

* **Session Broker (tmux):** Multiplexes the environment, allowing multiple
  workers to run concurrently in detached sessions with named handles; supports
  live human attach.
* **Agent Payload:** The target CLI agent (e.g., Claude Code) executing the
  specialized task.

*(Removed: the Podman rootless container and the PTY-supervisor / keystroke-
injection wrapper. The container was the isolation boundary; isolation is now a
transport property (§2). The PTY wrapper existed only to intercept and auto-
resume spend-limit dialogs — moot. Container-level security and MAC labeling are
a v2 follow-up, not v1.)*

## 4. Execution and Delegation Flow

1. **Dispatch Phase:** The delegating agent formulates a specialized task and
   starts a detached worker (named handle, chosen launcher/model/effort), with a
   strict definition of the expected output artifact.
2. **Execution & Supervision Phase:** The worker works through the task within its
   session.
3. **Introspection Phase (Optional):** A human operator attaches to the running
   worker, views real-time output, intervenes if necessary, and detaches without
   terminating the process.
4. **Reintegration Phase:** Upon task completion, the worker posts a concise
   summary; the delegating agent reads it into its context and the worker retires
   to free resources.

*(Removed: the "Initialization / PTY setup" step and the "rate-limit resume"
step from the original five-phase flow.)*

## 5. Security & Resource Considerations

* **Credential Management:** API tokens and OAuth session data must not be baked
  into any image; they should be mounted dynamically as read-only volumes from
  the host or passed via securely managed environment variables, ensuring
  revocation on the host propagates immediately to agents.
* **Resource & Lifecycle Management:** Unchecked worker spawning can starve
  resources; the architecture includes lifecycle management to detect and
  terminate orphaned sessions after output artifacts are consumed.
* **Monitoring:** A plausible naming convention explains the context (the
  supervising agent's issue), phase, and model/effort of each worker.

*(Removed: the rootless-execution / Podman / MAC-labeling specifics — v2
follow-up when multi-user or hostile-task isolation is needed.)*

## 6. Deployment

The implementing design and code live in [`PLAN-ccd-v2.md`](PLAN-ccd-v2.md).
