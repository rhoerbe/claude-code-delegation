# Product Design Record (PDR): Containerized AI Agent Multiplexer

**Target Environment:** Debian 13 (Trixie), Podman (Rootless), tmux
**Core Focus:** Asynchronous Agent Delegation, PTY Supervision, State Introspection

## 1. Context & Executive Summary

Using Claude Code for long-running tasks with subagents suffers from 2 issues that cannot be remedied in the native tool alone.
- Arriving at a spending limit where the user wants to wait until a new time window opens, gets the agent stuck until manuaal intervention akss to resume.
- Subagent output is not visible.

## 2. System Requirements

To achieve robust asynchronous delegation, the architecture must satisfy the following constraints:

* **Context Isolation:** Subagents must execute in isolated environments. Their thought processes and intermediate outputs must not flood the parent agent's context window.
* **Rate-Limit Resilience:** The system must autonomously detect and navigate standard interactive API usage-limit prompts (e.g., answering "wait and resume") without human intervention or parent agent polling.
* **Live Introspection:** Human operators must be able to attach to, monitor, and detach from any running subagent session in real-time via standard terminal emulators without disrupting the agent's execution.
* **Asynchronous Dispatch:** The parent agent must be able to trigger a sub-task and immediately return to an idle state or pursue parallel tasks.
* **Standardized IPC:** Inter-process communication between the parent and subagents must rely on standardized, asynchronous file-based handoffs rather than direct stream piping.
* **Configurable Invocation**: The orchestration agent may choose from a predefined set of invocations with following parameters:
  * Invocation script (currently /home/r2h2/.local/bin/claude and /home/r2h2/.local/bin/claude-glm) 
  * Model tier (fable-custom, opus-custom, sonnet-custom, haiku-custom)
  * Effort

## 3. Architecture Design

The architecture relies on a nested execution model, abstracting the interactive TUI requirements of the agent away from the parent orchestrator.

* **Container Engine (Podman):** Acts as the primary isolation boundary. A persistent, daemonized container serves as the execution pool for all subagents, standardizing the environment and tooling available to the AI.
* **Session Broker (tmux):** Runs as the primary entrypoint within the container. It multiplexes the container's environment, allowing multiple subagents to run concurrently in detached sessions.
* **Supervisor Hook (PTY Wrapper):** Acts as the bridge between the headless tmux session and the interactive agent. It spawns a pseudo-terminal to fool the agent into rendering its TUI, allowing the supervisor to scrape standard output and inject keystrokes programmatically.
* **Agent Payload:** The target CLI agent (e.g., Claude Code) executing the specialized task.

## 4. Execution and Delegation Flow

The lifecycle of a delegated task moves through five distinct phases:

1. **Dispatch Phase:** The parent agent formulates a specialized task. It uses host-level command-line tools to instruct the container engine to spawn a new, detached multiplexer session inside the persistent container. The instruction includes a strict definition of the expected output artifact (e.g., a specific text file).
2. **Initialization Phase:** Inside the container, the multiplexer initializes the session. The supervisor wrapper takes control, establishing the virtual pseudo-terminal and launching the subagent. The parent agent's dispatch command returns a success code immediately, freeing the parent.
3. **Execution & Supervision Phase:** The subagent works through the prompt within its PTY. If it encounters a rate limit, the supervisor intercepts the dialog rendering in the output stream and automatically injects the required terminal input to pause and resume, keeping the subagent alive.
4. **Introspection Phase (Optional):** At any point, a human operator can query the container engine for active multiplexer sessions and attach their local terminal to the running subagent. The operator can view the real-time standard output, intervene if necessary, and detach without terminating the process.
5. **Reintegration Phase:** Upon task completion, the subagent writes a concise summary of its work to a shared volume mount. The parent agent, either through periodic polling or a subsequent prompt, reads this artifact file, absorbs the findings into its context, and triggers the termination of the detached multiplexer session to free resources.

## 5. Security & Resource Considerations

Implementing this architecture requires strict adherence to security boundaries and resource management, particularly given the autonomous execution capabilities of the agents.

* **Rootless Execution Boundaries:** The container engine must operate entirely in rootless mode to prevent potential privilege escalation from an autonomous agent executing arbitrary shell commands. User namespace mapping must strictly align with the unprivileged host user.
* **Credential Management:** API tokens and OAuth session data must not be baked into the container image. They should be mounted dynamically as read-only volumes from the host environment or passed via securely managed environment variables, ensuring that session revocation on the host immediately propagates to the agents.
* **Mandatory Access Control (MAC):** Volume mounts bridging the host and the container must utilize proper SELinux context labeling. Inter-process communication (the output artifacts) should be restricted to heavily scoped, designated workspace directories to prevent unauthorized file modifications on the host by subagents.
* **Resource Exhaustion & Lifecycle Management:** Unchecked subagent spawning can lead to CPU/memory starvation or rapid API quota depletion. The architecture must include a lifecycle management strategy—such as host-level cron jobs or parent-agent cleanup mandates—to detect and aggressively terminate orphaned pseudo-terminals and lingering multiplexer sessions after the output artifacts have been consumed.
* **Monitoring**: using a plausible naming convention monitoring of the containers explains the context (typically the supervisong agnet's issue), phase and model/effort

## 6. Deployment

* **Host OS:** Debian 13 providing the foundation for rootless containerization.
