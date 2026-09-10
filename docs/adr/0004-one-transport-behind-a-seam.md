---
status: amended by ADR-0007
---

# One transport, behind a seam, with no plugin system

The broker talks to clients over a single Unix domain socket. `Transport` exists as an abstraction boundary with exactly one implementation (`UnixSocketTransport`), and there is deliberately **no** plugin loader, no `--transport` flag, and no registry of implementations.

The seam is there because a future TCP, MQTT or Matrix transport is plausible and the boundary costs nothing to keep clean. The machinery to *select* between transports is not there because a second implementation does not exist yet, and building configuration for a choice nobody can make is how a small tool acquires a framework.

## Consequences

A reader who finds an interface with a single implementation should not treat it as an accident to be tidied away, nor as an invitation to build the loader. Adding a real second transport is a new class and a decision about selection — at which point this ADR should be revisited rather than assumed.

## Amendment (ADR-0007)

The principle above — keep configuration out of the broker — stands, but it is
not "keep *policy* out of the broker". [ADR-0007](0007-affiliation-is-claimed-not-declared.md)
puts worker affiliation there deliberately, because it is the only component
that can refuse a misrouted task rather than detect one after the fact.

The line now drawn is between mechanism and configuration. Affiliation adds one
field and one comparison, with no file, flag or registry to maintain. A
transport registry or a `--transport` flag would add all three, and remains
refused.
