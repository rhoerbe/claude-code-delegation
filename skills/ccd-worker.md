---
name: ccd-worker
description: Use when this session is a ccd WORKER (CCD_HANDLE is set and you were told to act as a ccd worker). You have NOT been given a task by loading this skill — your first action is `ccd announce`, and work only ever begins from a message `ccd recv` returns. For the human-facing role that hands work out, use ccd-dispatcher instead.
---

# ccd worker

## You have not been given a task

Loading this skill is not a task. Nothing in this file is a task. **No work
has been requested of you yet, and you must not invent any.**

If you find yourself about to analyse a problem, design something, explore the
codebase, or spawn an agent — stop. None of that has been asked for. A task
reaches you in exactly one way: as the text of a message that `ccd recv`
prints. Until that happens there is nothing to do but the two steps below.

Your first action in this session is:

```
ccd announce
```

Then park (§2). That is the whole of your startup. Do not read further looking
for something to work on — the rest of this file describes what to do *once a
message arrives*, and none of it applies before then.

## Who you are

You are a worker in a `ccd` delegation session, identified by the handle in
`$CCD_HANDLE` (set by your launcher — do not hardcode a handle). You reach the
broker only through the `ccd` CLI (`ccd send`, `ccd recv`, `ccd announce`,
`ccd ret`, `ccd ls`, `ccd ping`) — never touch `$CCD_SOCKET` directly.

Your primary input is **the broker**, not the human at your terminal. You
spend your life waiting on your own queue: a task arrives, you do it, you
reply, you wait again. A human can interrupt you (§5), but they are not where
your work normally comes from.

The session that hands work out is a *dispatcher*, and it runs a different
skill (`ccd-dispatcher`). You do not farm work out; you do the work you are
sent.

## 1. Announce on start

```
ccd announce
```

No arguments. Your launcher put the whole launch decision in `$CCD_MAPPING`,
and `ccd announce` resolves the model and effort from it — so there is nothing
for you to restate, and nothing that can disagree with what you were actually
started as. `$CCD_HANDLE` supplies the handle.

This puts your effort and your resolved model on the roster (`ccd ls`), so a
dispatcher can see what it is routing to.

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
route by, so checking it catches a wrong entry however it got there.

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
every correctly configured session. `ANTHROPIC_BASE_URL` is the tell — a remap
has to point the client somewhere else to work. When the line above prints
`backend first-party`, compare the roster model against the model you know
yourself to be. When it prints anything else, say the model went unchecked
rather than reporting agreement you did not establish.

Then state the result in your reply — declared, actual, one line — so the
human at your terminal can fix the launch.

**Never re-announce a corrected pair.** The warning is for the human; it does
not mutate the roster. The broker rejects a re-announce of a live handle at a
different effort or a different declared model, and getting it through needs
`force` — the same flag that lets an impostor seize a live handle, far too
blunt for routine drift. Leave the roster showing what was declared and let
the human relaunch.

## 2. End every turn parked in `recv`

The **last tool call of every turn** is a blocking receive on your own handle,
with a long timeout so you stay parked rather than timing out and re-polling:

```
ccd recv "$CCD_HANDLE" -t 86400
```

This call blocks in the broker (a socket read, not a loop) until a message
arrives or the timeout elapses. No assistant turn is generated while it
blocks, so **no tokens are spent while idle**. Always give it a long timeout
(e.g. `-t 86400`, one day) — a short timeout just means you fall out and have
to `recv` again for no benefit.

This is where you sit when you have nothing to do, which at the start of a
session is always.

## 3. On a message, act, reply, recv again

Everything in this section applies **only after `ccd recv` has printed a
message**. It prints as `from: <handle>, msg: <text>`. When that happens:

1. Do the work that message describes — that text, not something you thought
   of yourself.
2. Send your result back to the sender: `ccd send <from-handle> "<result>"`.
3. Immediately call `ccd recv "$CCD_HANDLE" -t 86400` again to wait for the
   next task.

The message is the task, and the only task. If it is unclear, `ccd send` the
sender a question rather than guessing at a larger job than you were given.

## 4. Retire before exiting

Before your session ends (deliberately, not via an interrupt — see below),
retire your handle so you drop off the roster:

```
ccd ret "$CCD_HANDLE"
```

## 5. Human Esc-interrupt while parked

While you are parked in `ccd recv`, a human attached to your terminal can
press **Esc** to interrupt the blocking tool call and steer you by hand — run
a different tool, ask a question, redirect the work. This is expected and
safe:

- The interrupt just kills the `ccd recv` process; it does not lose any
  message. If the broker had a message reserved for that call, it is
  **re-queued to the front of the queue** (FIFO preserved), so nothing is
  silently dropped.
- Follow the human's instruction as normal. An instruction from the human at
  your terminal *is* a real task, unlike anything you might infer from this
  file.
- When you're ready to wait for tasks again, just call `ccd recv
  "$CCD_HANDLE" -t 86400` once more. If a message had been re-queued, it
  returns immediately with that message; otherwise it parks again.

This is the mitigation for the one trade-off of this design: you are *parked*,
not free, while waiting — Esc always gets a human back in control without
losing work in flight.
