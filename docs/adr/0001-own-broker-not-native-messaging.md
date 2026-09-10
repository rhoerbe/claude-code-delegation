# A private message broker, not Claude Code's native cross-session messaging

Claude Code already has cross-session messaging (`SendMessage`/FleetView), and using it would have made this repo unnecessary. We built a broker anyway, because that bus is armed by per-session environment variables (`CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`) and is coupled to the session's auth and config directory — so it cannot bridge a subscription/OAuth dispatcher to a worker running on a different backend, which is the entire point of the project.

`ccd` is auth-agnostic instead: it never reads `ANTHROPIC_BASE_URL`, `CLAUDE_CONFIG_DIR`, or any Claude-internal session state. The cross-backend hop happens entirely in `ccd` tool calls to a broker process that knows nothing about Claude.

## Considered options

* **Native `SendMessage`/FleetView.** Free, already there, and the obvious first choice. Rejected after investigation: three separate theories about what gated it were all inconclusive, and the mechanism turned out to be per-session env vars rather than anything a config could align. Chasing it further would have been fighting the harness.
* **Share one `CLAUDE_CONFIG_DIR` between backends** so both sessions land on the same bus. Rejected: config-dir sharing does not put two sessions on one bus — each session gets its own socket regardless.

## Consequences

Native `/list-agents`, `SendMessage` and `claude agents` are **not used** for the cross-backend hop, and a reader should not expect them to be. Both sides stay ordinary interactive TUI sessions, which is a property we keep deliberately — see ADR-0005.
