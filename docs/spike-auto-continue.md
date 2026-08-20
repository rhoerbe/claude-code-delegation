# Spike: does auto-continue apply to Worker Agents?

**Run:** 2026-08-20 · **Binary:** `2.1.237` · Answers issue #1 §4 and PRD §8 open question 1

## The question

2.1.234 shipped `autoContinueAtUsageLimit`, on by default: it parks a session at a usage
limit and resumes it in-process when the limit resets. The MVP dispatches every Worker
Agent with `claude --bg`. Issue #1 could not resolve statically whether a `--bg` session
arms that waiter or falls under the documented *"this session moved to the background, so
the task will not resume on its own"* case.

## Answer — **NO. A `--bg` Worker Agent never arms auto-continue.**

The gate is structural and sits upstream of everything else:

```js
function PP(){  return vr.host.launchOptions.isInteractive() }
function hs(){  return Rje()==="bg" }
function Rje(){ let e=q.CLAUDE_CODE_SESSION_KIND;
                if(e==="bg"||e==="daemon"||e==="daemon-worker") return e; return }

function g7f(){ return PP() && !hs() }      // interactive AND NOT background
function VjS(){ return g7f() && KYn() }     // ...AND not killswitched/disabled
```

`--bg` sets `CLAUDE_CODE_SESSION_KIND=bg`, so `hs()` is true and `g7f()` is false
regardless of the setting, the subscription, or the limit type.

Two corollaries fall out of the same gate:

* **`--print` / headless is also excluded**, via `PP()` — auto-continue requires an
  interactive session. So option B in [ADR-0003](adr/0003-native-background-sessions.md)
  would not have gained it either.
* **Auto-continue only ever rescues an attended terminal.** In this architecture that
  means a Planner or Supervisor session a human is sitting in front of — never a worker.

## Empirical corroboration

A local endpoint returned a genuine rate-limit rejection (`ANTHROPIC_BASE_URL` pointed at
it), carrying the headers the client actually reads:

```
HTTP 429  anthropic-ratelimit-unified-status: rejected
          anthropic-ratelimit-unified-representative-claim: five_hour
          anthropic-ratelimit-unified-reset: <now+75s>
          retry-after: 75
```

A `claude --bg` worker dispatched against it:

```
21:52:25  REQUEST  reset_in=63s        <- one request, rejected
21:52:42  status=idle  requests=1
...
21:54:36  status=idle  requests=1      <- 68s past reset, no second request
hooks:    SessionStart, Notification(idle_prompt)
```

No retry after the reset, no `quota_auto_resume_*` notification, and status went to
`idle` — **never `waiting`**.

**Weight this as corroboration, not proof.** The mock authenticates with a token rather
than claude.ai OAuth, so a negative result alone would be confounded by auth type. The
static gate above is the load-bearing evidence; the run confirms the gate behaves as read.

## Incidental findings

* **Auto-continue has a 24-hour horizon.** A fifth non-survival case not in issue #1's
  table: `horizon-exceeded` — *"the usage limit now resets more than 24 hours out; the
  task will not resume on its own"*. So even for attended sessions it covers 5-hour
  limits and only those weekly limits already inside 24h of reset.
* **Rate-limit state is header-driven** (`anthropic-ratelimit-unified-*`), which is what
  made the mock possible. The class exposes `addExceededLimit(type, hours)` and
  `setEarlyWarning(window, util, hours)`, but no reachable CLI caller was found.
* **The notification enum gained** `quota_auto_resume_fired`, `_stale`, `_disabled` in
  2.1.234 — worth adding to the Attendant's event table even though workers never emit
  them, since the Runner itself may run attended during development.

## Method note

`pkill -f '<pattern>'` matches the shell's own command line when the pattern appears in
it, killing the shell mid-script (observed: exit 144 = SIGTERM, silently skipping every
later step). Kill background helpers by recorded PID or by listener lookup instead.
