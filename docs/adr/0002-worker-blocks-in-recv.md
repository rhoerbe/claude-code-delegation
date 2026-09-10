# A worker blocks in `ccd recv` rather than delegating the wait to a helper

A worker's last tool call of every turn is a blocking `ccd recv`, which parks the session inside a pending Bash call until a message arrives. The alternative was a `--bg` helper subagent that blocks on the worker's behalf and wakes it via `SendMessage`, leaving the worker free between tasks.

We chose the direct block because it costs nothing while idle and needs no second process: no assistant message is generated during the wait, so no tokens are billed for it. This was the load-bearing claim of the design and was verified against real transcripts, not assumed.

## Consequences

The worker is genuinely **parked** while idle — not free to do something else unprompted. That is the one real cost of this choice, and the mitigation is that a blocking tool call is interruptible the ordinary way: **Esc** returns control to a human without losing the message, because a dropped connection re-queues whatever was reserved (see ADR-0003).

Lifting the wait into a `--bg` helper remains a possible upgrade, but it is not a free swap — a `--bg` session cannot carry the delegation identity and cannot resume itself across a usage limit (ADR-0005).
