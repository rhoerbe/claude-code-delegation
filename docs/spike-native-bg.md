# Spike: what `claude --bg` already provides

**Run:** 2026-08-14 · **Binary:** `~/.local/share/claude/versions/2.1.232` · **Host:** Debian 13, no container

Every claim below is WORKS / DOES-NOT-WORK with the command that proved it. No conclusion
without a pasted result.

## S1 — Asynchronous dispatch, surviving the parent — **WORKS**

```
$ claude --bg --settings <f> --dangerously-skip-permissions --model haiku -n spike-w1 "<task>"
Starting background service…
backgrounded · 5e5afe67 · spike-w1
  claude agents             list sessions
  claude attach 5e5afe67    open in this terminal
  claude logs 5e5afe67      show recent output
  claude stop 5e5afe67      stop this session
```

Returns immediately, exit 0. The dispatching shell exited; the worker ran to completion.
Work is executed by a **daemon** (`~/.claude/daemon.log`, control socket at
`/tmp/cc-daemon-<uid>/<hash>/control.sock`), which pre-warms "spare" workers.

## S2 — Machine-readable state — **WORKS**

`claude agents --json` needs no TTY, exits 0. Background sessions appear only under `--all`.

Observed `status` values across a worker lifetime:

| value | meaning | observed when |
|---|---|---|
| `busy` | working | mid-task |
| `idle` | finished, session still alive | task complete |
| `waiting` | **blocked on a prompt** | `--permission-mode manual` hit a permission gate |
| `null` | settled (done/crashed/killed) | after `claude stop` |

Polled transitions, two concurrent workers:

```
19:38:43 spike-w1=busy spike-w2=busy
19:39:10 spike-w1=busy spike-w2=idle
19:39:47 spike-w1=idle spike-w2=idle
```

`waiting` vs `idle` distinguishes *blocked* from *done* without touching a terminal.

## S3 — Hooks in background sessions — **WORKS**

`--settings <file>` **is** applied to `--bg` sessions. Events fired: `SessionStart`,
`PostToolUse`, `Notification`, `SessionEnd` (on stop).

`Notification` payload is typed:

```json
{ "session_id": "9cc97df8-…", "cwd": "…/w2",
  "transcript_path": "/home/<user>/.claude/projects/…/9cc97df8-….jsonl",
  "hook_event_name": "Notification",
  "message": "Claude is waiting for your input",
  "notification_type": "idle_prompt" }
```

Forcing a permission gate yielded `"notification_type": "permission_prompt"`,
`"message": "Claude needs your permission"`.

Identifiers present in the binary: `agent_completed`, `agent_needs_input`, `auth_success`,
`computer_use_enter`, `computer_use_exit`, `elicitation_complete`, `elicitation_response`,
`idle_prompt`, `push_notification`, `worker_permission_prompt`.

**Consequence: blocked-detection is a typed event, not a TUI regex.**

## S4 — Transcripts are readable and structured — **WORKS**

`transcript_path` arrives in every hook payload. 41-line JSONL, `mode 0600`:

```
13 assistant · 10 attachment · 6 user · 2 agent-name · 2 mode · 1 system · …
```

A Reviewer can be handed the transcript path directly; no worker-authored summary required.

## S5 — Concurrency and credentials — **WORKS**

Three workers dispatched concurrently completed independently.
`~/.claude/.credentials.json` stayed valid JSON, **mtime unchanged** through the run —
the daemon refreshes auth centrally (`[supervisor] auth: token still valid (cross-process
refresh or not yet due)`), so concurrent workers do not race on the token file.

## S6 — Usage-limit behaviour — **NOT DETERMINED**

No `usage_limit` / `rate_limit` value appears in the notification-type identifiers.
Whether a limit surfaces as a `Notification`, as `status: "waiting"`, or as neither is
**unknown** and is the one fact the design still rests on. Tripwire pending.

---

## Gotchas that will bite a Runner

1. **Dispatch exit 0 does not mean the worker started.** A first attempt printed the full
   success banner, then: `bg settled 92c7ea35 (crashed): exit 1 before init — error:
   unknown option '--session-name'`. Dispatch must be followed by a liveness check.
2. **Two different session ids.** `agents --json` returns a full UUID; `stop`/`logs`/`attach`
   accept only the **8-char prefix**. Passing the UUID gives
   `No job matching '5e5afe67-…'`. Truncate before calling.
3. **`claude logs` needs a live daemon.** After `[supervisor] idle 5s with no clients —
   exiting`, logs fail with `connect ENOENT …/control.sock`. No post-mortem via `logs`;
   read the transcript instead.
4. **`-n/--name` is the flag; there is no `--session-name`.** The name surfaces as
   `session_title` in hook payloads. Without it, `name` in `agents --json` is the entire
   prompt text.
5. **The binary moved 2.1.224 → 2.1.225 → 2.1.232 during a single design session.**
   Any parser of this surface needs the startup self-check.
