# One transport, behind a seam, with no plugin system

The broker talks to clients over a single Unix domain socket. `Transport` exists as an abstraction boundary with exactly one implementation (`UnixSocketTransport`), and there is deliberately **no** plugin loader, no `--transport` flag, and no registry of implementations.

The seam is there because a future TCP, MQTT or Matrix transport is plausible and the boundary costs nothing to keep clean. The machinery to *select* between transports is not there because a second implementation does not exist yet, and building configuration for a choice nobody can make is how a small tool acquires a framework.

## Consequences

A reader who finds an interface with a single implementation should not treat it as an accident to be tidied away, nor as an invitation to build the loader. Adding a real second transport is a new class and a decision about selection — at which point this ADR should be revisited rather than assumed.
