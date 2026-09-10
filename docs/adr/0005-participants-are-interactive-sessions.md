---
status: accepted
---

# ccd participants are interactive sessions, never `claude --bg`

A `ccd` dispatcher or worker must be started as an ordinary interactive session — a terminal, or a tmux window — and never with `claude --bg`. This looks backwards: a long-running worker that spends its life parked in `recv` is exactly the shape `--bg` seems designed for. Two independent findings say otherwise, and each alone is sufficient.

**A `--bg` session does not receive the delegation identity.** `--bg` propagates `--name` but not per-invocation environment variables, so `CCD_HANDLE`/`CCD_MODEL`/`CCD_EFFORT` never reach the launched session. It cannot announce and cannot be addressed. The failure is at least loud — every `ccd` entry point exits non-zero with an explicit message when no handle is set.

**A `--bg` session cannot resume itself across a usage limit.** Claude Code's `autoContinueAtUsageLimit` is gated on the session being interactive and not backgrounded; `--bg` sets `CLAUDE_CODE_SESSION_KIND=bg` and fails that gate, as does `--print`/headless. So a backgrounded participant parked across a limit stays parked instead of continuing. Auto-continue only ever rescues an attended terminal — which is precisely what an interactive participant is.

## Consequences

The second finding is what makes the interactive choice actively good rather than merely required: a participant left running unattended survives a usage limit *because* it is interactive. It also means the human-attach property (ADR-0002's Esc mitigation) is not a nicety bolted on — it falls out of the same decision.

This constraint lives outside this repo, in the harness, so it cannot be fixed here and should be re-checked when the harness changes.
