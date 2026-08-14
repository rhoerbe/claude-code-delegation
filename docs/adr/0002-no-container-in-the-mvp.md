---
status: accepted
---

# No container in the MVP; isolation delegates to Freigang

The original design made rootless Podman the primary isolation boundary: a persistent,
daemonized container acting as the execution pool for all delegated work. We removed it.
In the MVP, Worker Agents are plain host processes, each in its own git worktree, and
containment is delegated to the existing Freigang launcher when it is wired in.

(The tmux and pseudo-terminal layers of that same original stack were removed for
different reasons — see [ADR-0003](0003-native-background-sessions.md).)

## Why

Podman was removed because it is **already solved elsewhere**. Freigang
provides rootless Podman with `--userns=keep-id`, Cedar policy, systemd Quadlets and
per-agent Linux users. Rebuilding a weaker version here would have meant spending the first
weeks debugging mounts, OAuth token refresh against read-only credential mounts, and image
rebuilds on every `claude` update — none of which validates the delegation idea. The
original security section was also partly unimplementable as written: it mandated SELinux
context labelling on a host that has no SELinux.

## Consequences

* **Workers are as privileged as the user**, running fully autonomously. Blast radius is
  controlled only by one worktree per worker until Freigang is wired in. This is the
  accepted cost of the MVP, and the reason the Freigang integration is not optional
  long-term.
* **The dispatch mechanism must stay swappable.** The Runner treats "how to invoke a
  worker" as opaque data, so moving to Freigang — or to a routing proxy, should
  `hosting#88` adopt one — changes configuration rather than the Runner.
* **Sessions must be reaped explicitly.** The container teardown that would have collected
  orphaned workers no longer exists.
* **Freigang's vocabulary applies at the boundary.** A Worker Agent is a Freigang *Agent
  Instance* carrying a task; the two glossaries must not drift apart before integration.
