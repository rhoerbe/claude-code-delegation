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
ccd announce "$CCD_HANDLE" "$CCD_MODEL" "$CCD_EFFORT"
```

This tells anyone dispatching work which handle maps to which model/effort
tier. A dispatcher does this too — its own handle is just as much a roster
entry as any worker's, so other participants can route tasks or results back
to it.

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
