# Plan — ccd v1: local cross-backend agent delegation (option 2: direct-block)

Status: design, ready to implement. **Supersedes `PLAN-ccd-v1.md`** (the file-inbox
+ skill-polling design — dropped: per-turn polling is coarser and burns a turn
per idle cycle; a blocking `recv` is strictly better and is now verified safe).

This repo (`claude-code-delegation`) holds the **generic, public delegation
model** — no host-specific paths, credentials, handles, or launcher names.
Deployment onto specific hosts via ansible roles, and the wiring of personal
launchers/handles, lives in a separate **private deployment repo** (§9).
Anyone should be able to use this repo without exposure to the author's
settings.

The decisions in this plan that are hard to reverse, surprising without context,
and the result of a real trade-off are also recorded one-per-file in
[`docs/adr/`](docs/adr/). This document stays the full implementing design; the
ADRs are the short answers to "why is it like this?".

## 0. What this is, in one paragraph

A small standalone message broker (`ccd-broker`) plus a thin CLI (`ccd`) that
lets a human-steerable interactive **dispatcher** session delegate tasks to
human-steerable interactive **worker** sessions running on a *different LLM
backend*, and collect results — without either side burning tokens while idle.
It is auth-agnostic: it does not read `ANTHROPIC_BASE_URL`,
`CLAUDE_CONFIG_DIR`, or any Claude-internal session bus. The cross-backend hop
happens in tool calls to the broker, not via Claude's native peer messaging
(which is auth/config-dir coupled and could not bridge an OAuth dispatcher to an
OpenRouter worker — the failure that motivated this).

## 1. Context / problem

Claude Code's native cross-session messaging (`/list-agents`, `SendMessage`,
FleetView) could not be made to bridge a subscription/OAuth dispatcher to a
3rd-party-backend worker (OpenRouter, local ollama). The exact gating was never
fully determined (three theories, all inconclusive; the investigation is
recorded in the deployment repo). Rather than fight native FleetView, ccd provides its own
auth-agnostic transport. The dispatcher and workers stay **interactive TUI
sessions** (human can attach/confirm/take over) — that requirement is native
FleetView's strength, and ccd preserves it by not touching the TUI at all:
sessions communicate only through `ccd` tool calls.

The author's working reference: 6 interactive side-by-side `claude` sessions
on one backend, native SendMessage, dispatcher delegates. ccd reproduces that
shape **across backends** by replacing the bus with the broker.

## 2. The verified primitives (why option 2 is safe)

Tested 2026-08-27 on a worker backend (full log in the deployment repo's
session transcript). All three passed:

1. **Blocking-tool-call survival (Fork 1):** a `claude --bg` subagent running a
   long-blocking Bash tool call (`sleep 180`, `timeout=200000`) is **not killed**
   at 60s/120s; it stays `busy` through the full block and completes
   (`busy`→`idle`). → A worker's `ccd recv` Bash call can block for an
   arbitrary wait without the session dying.
2. **Symmetric coexistence (Fork 2):** two such blocking subagents (120s each,
   simultaneous) both survived and completed. → Dispatcher and worker can both
   block on recv at once.
3. **Push + delivery (bg→interactive):** a `--bg` subagent `ListAgents`'d +
   `SendMessage`'d an interactive session (`success:true`); the message arrived
   at the receiver's turn boundary. → Same-bus push works (relevant for the
   option-1 upgrade path, §10, not v1).

**Token-spend claim (reasoned, not yet measured):** while a session is parked
inside a pending tool call, no assistant message is generated, so no tokens are
billed for the wait (the deployment repo's token-usage accounting counts only
`message.usage` on assistant turns). v1 smoke test step §7 verifies this directly rather than
asserting it.

## 3. Pinned decisions (v1)

- **Option 2 — direct block, no helper subagent.** The worker's last tool call
  each turn is `ccd recv <handle>`; it blocks (0 tokens) until a task arrives,
  then the turn continues. No `--bg` peer, no relay, no native `SendMessage` in
  the wake path → no cross-session permission check, no per-message Deliver/Deny
  hold. Trade-off accepted: the session is *parked* in the blocking call while
  idle (not free for a parallel human chat mid-wait). Mitigation: **Esc**
  interrupts the blocking call to steer by hand, then re-`recv` (§8).
- **Symmetric.** Dispatcher and worker are the same shape: each ends its turn
  with `ccd recv <own-handle>`. A "dispatcher" is just a worker whose incoming
  tasks are delegation requests and whose skill tells it to farm subtasks out.
- **Auth-agnostic.** The broker never reads Claude env vars. Backend selection
  is entirely by which launcher starts the session (a deployment concern, §9).
- **Transport seam, not a plugin system.** One abstraction boundary
  (`Transport` interface: `serve` / `recv_request`), one concrete impl
  (`UnixSocketTransport`). v1 ships UDS only. TCP/Matrix/MQTT *could* be added
  later by writing a second concrete class — **do not build a plugin registry,
  loader, or config-driven backend selection now.** The interface stays stable;
  that is the entire future-proofing.
- **Worker lifecycle.** A worker announces itself on start (handle + model +
  effort) and retires on exit, so the dispatcher's roster emerges from messages
  rather than being hand-maintained. This serves the "model defined by session
  handle" model: the handle carries the capability.
- **Dropped from the PRD:** Podman isolation, PTY-supervisor/keystroke-injection
  for rate-limit resume (the 5h-spend-limit behavior is obsolete in current
  Claude Code), and 5h-spend-limit handling itself. Container security/MAC
  (PRD §5) is real but v2+, not v1.

## 4. Architecture

```
                        ccd-broker (python, persistent process, UDS)
                        ┌─────────────────────────────────────────┐
                        │  queues:  <handle> -> [msg, msg, ...]    │
                        │  roster:  <handle> -> {model, effort}    │
                        │  Transport seam (UDS impl; swap-ready)   │
                        └─────────────────────────────────────────┘
             send/recv/announce/retire  ▲              ▲  send/recv/announce/retire
                                       │              │
  dispatcher (interactive, backend A)   │              │   worker (interactive, backend B)
    - end of turn: ccd recv disp        │              │     - on start: ccd announce <h> <caps>
    - on task: farm out, ccd send <W>   │              │     - end of turn: ccd recv <h>  (blocks, 0 tok)
    - on result: read, ccd recv disp    │              │     - on task: work, ccd send disp <result>
    - human: attach TUI, confirm, steer │              │     - on exit: ccd ret <h>
```

The broker is the only long-lived process. Dispatcher and workers are ordinary
interactive Claude sessions; they reach the broker via `ccd` Bash tool calls.
The broker does not know which backend a session uses.

## 5. Components (this repo)

### 5.1 `ccd-broker` — `ccd_broker/broker.py` (python, stdlib only)

A persistent UDS server. Line-delimited JSON request/response, one logical
request per connection (a `recv` connection is held open by the broker until a
message is available or the client disconnects/times out).

Request model (all field names stable — they are the transport-stable interface):

| method | args | reply | blocking? |
|---|---|---|---|
| `send` | `{to, msg, from?}` | `{ok:true, id}` | no |
| `recv` | `{handle, timeout?}` | `{ok:true, from, msg}` or `{ok:false, reason:"timeout"}` | **yes** (until msg or timeout) |
| `announce` | `{handle, model, effort}` | `{ok:true}` | no |
| `retire` | `{handle}` | `{ok:true}` | no |
| `roster` | `{}` | `{ok:true, workers:[{handle,model,effort}]}` | no |
| `ping` | `{}` | `{ok:true, version}` | no |

Semantics notes:
- **Atomic dequeue-on-ack.** `recv` reserves a message for that call; it is only
  removed from the queue when the response is fully written to that client. If
  the client disconnects (Esc-interrupt kills the `ccd recv` process) before
  ack, the reserved message is **re-queued** so it is not lost (§8).
- **No persistence.** Queues and roster are in-memory. Broker restart = empty
  queues; workers re-`announce` on their next start. Un-delivered in-flight tasks
  are lost on broker crash — acceptable for v1 (interactive human-in-loop use);
  documented. A WAL/journal is a v2 follow-up, not v1.
- **Socket path.** `$CCD_SOCKET` (default `${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.sock`).
  Created mode 0600; broker refuses connections from other uids (SO_PEERCRED
  check on Linux). No auth token in v1 — peer-uid + filesystem perms suffice for
  single-user interactive use; multi-user is v2 (PRD §5 MAC territory).

**Transport seam (the only abstraction):**
```python
class Transport(Protocol):
    name: str
    def serve(self) -> None: ...        # accept loop
    def handle(self, req: dict) -> dict | None: ...  # one request; None = wait-more

class UnixSocketTransport(Transport):
    name = "uds"
    # v1 sole impl. read/write line-JSON over AF_UNIX SOCK_STREAM.
```
The broker's queue/roster logic calls `Transport`, never `AF_UNIX` directly. A
future `TCPTransport`/`MatrixTransport` is a new class, no other change. **No
plugin loader, no `--transport` flag, no registry dict.** v1 hardcodes
`UnixSocketTransport`.

### 5.2 `ccd` CLI — `ccd` (bash; thin client wrappers)

Each subcommand opens a short-lived connection to `$CCD_SOCKET`, sends one JSON
request, prints the reply human-readably. `recv` blocks in the broker (the bash
process just waits on the socket read — this is the zero-token park).

| subcommand | maps to broker method |
|---|---|
| `ccd send <to> <msg> [-f from]` | `send` |
| `ccd recv <handle> [-t timeout]` | `recv` (prints `from: msg`) |
| `ccd announce <handle> <model> <effort>` | `announce` |
| `ccd ret <handle>` | `retire` |
| `ccd ls` | `roster` |
| `ccd ping` | `ping` |
| `ccd broker start|stop|status` | manage the daemon process (start = nohup `ccd-broker` if not running; status = ping) |

`CCD_HANDLE` env (set per-session by the deploying launcher/role, §9) is the
default `<handle>` for `recv`/`announce`/`retire` so the skill doesn't hardcode it.

### 5.3 Worker skill — `skills/ccd-worker.md` (generic; parametrized by env)

Distributed as a Claude Code skill (or an `--append-system-prompt` string the
deploying role injects). Teaches, in generic terms:

> You are a worker identified by `$CCD_HANDLE` (set by your launcher). On start,
> announce yourself: `ccd announce "$CCD_HANDLE" "$CCD_MODEL" "$CCD_EFFORT"`.
> At the **end of every turn**, make your last tool call `ccd recv "$CCD_HANDLE"`
> with a long timeout (e.g. `-t 86400`). It blocks until a task arrives; you do
> not spend tokens while it blocks. When a task arrives (`from: <h>, msg: ...`),
> do the work, then `ccd send <h> "<result>"`, then `ccd recv` again to wait for
> the next. Before you exit, run `ccd ret "$CCD_HANDLE"`.
>
> The message `from` field names the sender's handle; reply to it. A delegation
> request will name you as the worker and include the task; results go back to
> the sender's handle.
>
> A human may interrupt your blocking `recv` (Esc) to steer you by hand — follow
> their instruction, then `ccd recv` again. An interrupted `recv` does not lose
> a task (the broker re-queues it).

The dispatcher runs the **same skill** (symmetric) — it just has different
*content* for what to do with an incoming task (farm out, collect) and what to do
on done (read result, continue). The skill text can carry both modes, or the
deploying role injects a dispatcher-mode variant.

### 5.4 Smoke test — `tests/ccd_smoke.sh`

Generic, no host specifics. Uses two throwaway handles (`t-disp`, `t-work`):

1. `ccd broker start`; `ccd ping` → `ok:true`.
2. Background a `ccd recv t-work -t 10` (simulates a parked worker).
3. From another process, `ccd send t-work "hello" -f t-disp` → `ok:true`.
4. The backgrounded `recv` unblocks and prints `t-disp: hello`.
5. `ccd announce t-work dummy-model low`; `ccd ls` → includes `t-work`.
6. `ccd ret t-work`; `ccd ls` → excludes `t-work`.
7. **Token-spend verification** (§7) — needs a real Claude session, run
   manually; documented as step 7, not asserted by the script.
8. `ccd broker stop`.

## 6. File layout (this repo)

```
ccd_broker/
  __init__.py
  broker.py          # queue + roster + dispatch loop (transport-agnostic)
  transport_uds.py   # UnixSocketTransport (v1 sole impl)
  __main__.py         # entry: ccd-broker -> serve(Uds)
ccd                  # bash CLI (send/recv/announce/ret/ls/ping/broker)
skills/
  ccd-worker.md      # generic worker/dispatcher skill
tests/
  ccd_smoke.sh
README.md            # public-facing: what it is, install, use
PLAN-ccd-v2.md       # this file
PRD.md               # the original PDR (kept as design record)
```

No host-specific paths, no hostnames, no backend/provider names, no personal
handles. `$CCD_SOCKET`, `$CCD_HANDLE`, `$CCD_MODEL`, `$CCD_EFFORT` are the only
configuration surface — all set by the deploying environment (the deployment
repo's ansible role, §9).

## 7. Token-spend verification (the load-bearing claim)

Step 7 of the smoke test, run manually with a real worker session:

1. Start a worker on any backend, `ccd announce`, then let it `ccd recv`
   (blocking). Leave it parked for ~60s.
2. `ccd send` it a trivial task ("reply pong"); it wakes, replies.
3. Run the deployment repo's token-usage accounting script (`--since today`)
   on that session's transcript.
4. **Pass:** the token spend equals the announce turn + the wake/reply turn
   only — **zero** attributable to the ~60s parked wait (no assistant
   `message.usage` entry during the block). **Fail:** spend > the two turns
   (would mean the parked session was polling/generating — the design is broken;
   fall back to option 1's helper subagent).

That script lives in the deployment repo (not this one); the test is
documented here, executed against whichever host deploys ccd.

## 8. Esc-interrupt + manual steering (usage note, documented not coded)

A parked worker (`ccd recv` blocking) is interruptible:

- **Esc** in the worker's TUI interrupts the pending Bash tool call (kills the
  `ccd recv` client process). The broker detects the dropped connection and
  **re-queues** any message it had reserved for that call (atomic
  dequeue-on-ack, §5.1) — so an interrupt never silently swallows a task.
- The human can now steer the worker by hand (run tools, edit, ask it to do
  something else). When ready to wait again, instruct the worker to `ccd recv`
  once more; if a task was re-queued, it returns immediately.
- Edge case to handle in `broker.py`: a `recv` that reserved a message but whose
  client died must restore that message to the *front* of the queue (FIFO
  preserved) before any later `recv` on the same handle sees it.

This is documented in the skill (§5.3) and the README usage section. It is the
mitigation for option 2's one trade-off (session parked, not free, while idle).

## 9. Deployment (out of scope for this repo)

This repo is generic. Deployment onto the author's hosts is tracked in a
separate **private deployment repo**, and consists of:

1. A new ansible role (sibling of the existing per-backend launcher role) that:
   - Installs `ccd-broker` + `ccd` CLI from this repo (pip-install `ccd_broker`
     as a user package, or vendor the files into `~/.local/`).
   - Runs `ccd-broker` as a **systemd `--user` unit** (persistent, restarts on
     crash). This is the natural home for the long-lived broker; tmux is not
     needed for the broker (tmux stays only for human-attachable worker
     sessions, if the launcher convention uses it).
   - Injects `$CCD_HANDLE`/`$CCD_MODEL`/`$CCD_EFFORT` into each worker launcher
     (e.g. an OpenRouter launcher becomes a `<launcher>` arg + the ccd env).
   - Installs the `ccd-worker` skill into `~/.claude/skills/` (or wires it as
     `--append-system-prompt`).
2. **README correction** in the deployment repo's launcher documentation: the
   "feature-flag check" framing of `/list-agents` is now moot (ccd bypasses
   native messaging entirely). Rewrite that section to state ccd is the
   cross-backend transport and native FleetView is not used.
3. The esc-interrupt usage note (§8) mirrored into that repo's usage doc.

The deployment issue references this plan and this repo. The session
implementing *this* repo does not touch deployment.

## 10. Follow-ups (not v1)

- **Option-1 upgrade** (lift the blocking `recv` into a `--bg` helper subagent so
  the worker stays *free* while waiting): a localized change — the broker, send
  path, lifecycle, and skill's send semantics are identical; only the recv step
  moves from the worker's own turn to a `bypassPermissions`-matched `--bg` peer
  that blocks on `recv` and `SendMessage`s the worker. No fundamental redesign.
  **Caveat before building it:** `PRD.md` calls the spend-limit stall moot, but
  that only holds for *attended* sessions — `--bg` sets
  `CLAUDE_CODE_SESSION_KIND=bg`, which fails the `isInteractive() && !isBg()`
  gate on `autoContinueAtUsageLimit`, so a `--bg` helper parked on `recv` across
  a limit never resumes itself (evidence:
  `docs/spike-auto-continue.md` on the `design/runner-over-native-bg` branch;
  #4 is the same conclusion reached from the env-var side). Moving `recv` into a
  `--bg` peer therefore reintroduces a failure the current shape does not have.
- Other `Transport` impls (TCP, Matrix, MQTT) — new classes only, no broker
  change. Do not build the plugin system until a second impl is actually needed.
- Broker persistence (WAL/journal) for crash-safe in-flight tasks.
- Multi-user / hostile-task isolation (Podman + MAC, PRD §5).
- Slug-based backend detection in the deployment repo's token-usage accounting
  (a deployment follow-up) so 3rd-party turns under a unified `~/.claude`
  aren't mislabeled `oauth`.

## Refs
- The private deployment repo: the alternative-LLM launcher work (the launcher
  ccd deploys, and a `<launcher>` arg to ccd dispatch), the earlier OpenRouter
  wrapper and its ansible-ization (the deployment precedent), and cost-aware
  routing/proxy (orthogonal, not needed here).
- `PRD.md` (the original Containerized AI Agent Multiplexer PDR — design record)
- `PLAN-ccd-v1.md` (superseded — file-inbox + per-turn polling, dropped)
- `design/runner-over-native-bg` branch (**superseded design track, retained not
  merged**) — the earlier, unrelated-history line this repo restarted from on
  2026-08-29: a Runner (a program with no model) executing a Plan over `claude
  --bg` workers, with per-phase git worktrees and a Reviewer verdict grammar.
  `PRD.md` here is that branch's PRD pruned from 310 lines to 92 (the inline
  *(Removed: …)* notes mark the cuts), so the branch is the only place the full
  rationale, the ADRs, and two evidence spikes survive —
  `docs/spike-native-bg.md` (what `claude --bg` / `agents --json` / hooks
  actually provide, binary 2.1.232) and `docs/spike-auto-continue.md` (why a
  `--bg` worker never arms native auto-continue, binary 2.1.237). Both spikes
  predate the current binary by ~30 releases; re-run before relying on either.
  Not mergeable: no merge base, and the two designs contradict.
