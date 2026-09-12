---
name: ccd-worker
description: Use when this session is running as a ccd worker or dispatcher (CCD_HANDLE is set and you were told to act as a ccd participant). Announces on start, blocks on `ccd recv` between tasks so no tokens are spent while idle, replies via `ccd send`, and retires on exit. Same mechanics serve both a task-performing worker and a task-farming dispatcher.
---

# ccd worker / dispatcher

You are a participant in a `ccd` delegation session, identified by the handle
in `$CCD_HANDLE` (set by your launcher — do not hardcode a handle). You reach
the broker only through the `ccd` CLI (`ccd send`, `ccd recv`, `ccd announce`,
`ccd ret`, `ccd ls`, `ccd ping`) — never touch `$CCD_SOCKET` directly.

This one skill covers **both roles** in the delegation model (PLAN-ccd-v2.md
§3): a *worker* that performs tasks it's sent, and a *dispatcher* that farms
tasks out to other handles and collects results. Both are the same shape —
each ends every turn parked in `ccd recv` on its own handle. What differs is
only what you *do* with an incoming message, described below.

## 1. Announce on start

Your first action in the session is to announce yourself so you appear on the
roster (`ccd ls`):

```
ccd announce
```

No arguments. Your launcher put the whole launch decision in `$CCD_MAPPING`,
and `ccd announce` resolves the model and effort from it — so there is nothing
for you to restate, and nothing that can disagree with what you were actually
started as. `$CCD_HANDLE` supplies the handle.

This puts your effort and your resolved model on the roster (`ccd ls`), so
anyone dispatching work can see them. A dispatcher does this too — its own
handle is just as much a roster entry as any worker's, so other participants
can route tasks or results back to it.

If you were started by hand rather than by `ccd launch`, there is no mapping
to resolve and you pass them yourself: `ccd announce <model> <effort>`. That
path is fully supported and is what a plain shell or a non-Claude-Code backend
uses. Explicitly typed values always win over `$CCD_MAPPING`.

### Then check the announcement against what is actually running

What the roster now holds is what your launcher *declared*. Nothing reads it
back off the running session, so it can drift from what is actually serving
this turn — and a wrong effort on the roster makes every dispatcher route by
it wrongly. Immediately after announcing, check for drift and report it in
your own terminal.

Read your own row back and compare:

```
row="$(ccd ls | awk -F'\t' -v h="$CCD_HANDLE" '$1==h')"
declared_effort="$(printf '%s' "$row" | cut -f2)"
declared_model="$(printf '%s' "$row" | cut -f4)"
if [ "$declared_effort" = "-" ]; then
  echo "ccd: no effort declared (this mapping sends none); running ${CLAUDE_EFFORT:-unknown}"
elif [ "$declared_effort" = "$CLAUDE_EFFORT" ]; then
  echo "ccd: effort ok ($declared_effort)"
else
  echo "ccd: EFFORT MISMATCH - roster says $declared_effort, running $CLAUDE_EFFORT"
fi
echo "ccd: roster model ${declared_model}; backend ${ANTHROPIC_BASE_URL:-first-party}"
```

This reads the roster rather than an environment variable, which is both
sturdier and more to the point: the roster is what other participants actually
route by, so checking it catches a wrong entry however it got there. (It
replaces a comparison against `$CCD_EFFORT`, which no longer exists — `ccd
launch` removes it in favour of the single `$CCD_MAPPING`.)

**Effort — check always.** `$CLAUDE_EFFORT` is set by Claude Code in the
environment of every Bash tool call, per turn, *after* any silent downgrade
for the selected model, so it is the authoritative value. The roster holds
only what was declared, and a downgrade the launcher never saw is exactly the
drift worth catching. A mapping with no effort at all (`claude-haiku-4-5` is
the real case — the client never sends one for it) shows `-` and is not a
mismatch; there was nothing to disagree with.

**Model — check only on a bare `claude` launch.** Under a remapped backend
your own self-report is wrong: the session believes it is the model it asked
for while a different one serves it, so comparing would report a mismatch on
every correctly configured session. `ANTHROPIC_BASE_URL` is the tell — a
remap has to point the client somewhere else to work. When the line above
prints `backend first-party`, compare the roster model against the model you
know yourself to be. When it prints anything else, say the model went
unchecked rather than reporting agreement you did not establish.

Then state the result in your reply — declared, actual, one line — so the
human at your TUI can fix the launch.

**Never re-announce a corrected pair.** The warning is for the human; it does
not mutate the roster. The broker rejects a re-announce of a live handle at a
different effort or a different declared model, and getting it through needs
`force` — the same flag that lets an impostor seize a live handle, far too
blunt for routine drift. Leave the roster showing what was declared and let
the human relaunch.

## 2. End every turn parked in `recv`

The **last tool call of every turn** is a blocking receive on your own
handle, with a long timeout so you stay parked rather than timing out and
re-polling:

```
ccd recv "$CCD_HANDLE" -t 86400
```

This call blocks in the broker (a socket read, not a loop) until a message
arrives or the timeout elapses. No assistant turn is generated while it
blocks, so **no tokens are spent while idle**. Always give it a long timeout
(e.g. `-t 86400`, one day) — a short timeout just means you fall out and have
to `recv` again for no benefit.

## 3. On a message, act, reply, recv again

`ccd recv` prints the message as `from: <handle>, msg: <text>` once one
arrives. When it does:

1. Do the work the message describes.
2. Send your result back to the sender: `ccd send <from-handle> "<result>"`.
3. Immediately call `ccd recv "$CCD_HANDLE" -t 86400` again to wait for the
   next task.

**As a worker:** the incoming message is a task. Do it, then `ccd send` the
result to the `from` handle (typically the dispatcher), then `recv` again.

**As a dispatcher:** the incoming message may be a delegation request from a
human or another session, or a *result* coming back from a worker you
farmed a subtask out to.
- On a new task to delegate: pick a worker (`ccd ls` shows the current
  roster of announced handles with their model/effort), `ccd send <worker>
  "<subtask>"`, then `recv` again to wait for either the worker's result or
  further instructions.
- On being told in plain language to take workers on ("claim w1 and w2",
  "those two are yours") — from the human at your TUI or from another
  session: run `ccd claim <worker>` once per worker. Your own `$CCD_HANDLE`
  is the default owner, so no dispatcher argument is needed. A worker
  announces unowned and nothing routes to you until you claim it.
- On a result from a worker: read it, fold it into your overall task (and
  `ccd send` a follow-up to that or another worker if more work is needed,
  or `ccd send` the final answer back to whoever originally asked), then
  `recv` again.

There is no separate "dispatcher mode" flag — the difference is purely in
how you interpret each incoming message, using `ccd ls` to know who else is
available to farm work out to.

## 4. Retire before exiting

Before your session ends (deliberately, not via an interrupt — see below),
retire your handle so you drop off the roster:

```
ccd ret "$CCD_HANDLE"
```

## 5. Human Esc-interrupt while parked

While you are parked in `ccd recv`, a human attached to your TUI can press
**Esc** to interrupt the blocking tool call and steer you by hand — run a
different tool, ask a question, redirect the work. This is expected and
safe:

- The interrupt just kills the `ccd recv` process; it does not lose any
  message. If the broker had a message reserved for that call, it is
  **re-queued to the front of the queue** (FIFO preserved), so nothing is
  silently dropped.
- Follow the human's instruction as normal.
- When you're ready to wait for tasks again, just call `ccd recv
  "$CCD_HANDLE" -t 86400` once more. If a message had been re-queued, it
  returns immediately with that message; otherwise it parks again.

This is the mitigation for the one trade-off of this design: you are
*parked*, not free, while waiting — Esc always gets a human back in control
without losing work in flight.
