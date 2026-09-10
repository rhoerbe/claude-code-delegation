---
status: accepted
---

# The fleet dashboard is system-wide in metadata, scoped in content

One view should show every live session at once, and it should show enough to be useful — not just handles, but what each session is actually working on. Those two goals conflict as soon as more than one client's work is running, because a single rendered page containing both is exactly the adjacency the rest of this design avoids creating.

The resolution is to split them: **metadata is system-wide, content is shown for one scope at a time.** Handle, working tree, model, effort, status, cost and the claim graph (ADR-0007) are all metadata and appear for every session. Seeing what a session is *doing* means selecting a scope, so no single rendered artifact ever contains two clients' working material.

The dashboard is **read-only**. Giving it the ability to stop, retire or redirect a session would require an authorization story, and under ADR-0006 there are no principals to write one against. Control stays in the participant sessions, where a human is already attached.

## What "content" is

Role-dependent, one bounded line each: for a dispatcher, the first substantive message — the goal it was given; for a worker, the last task it received. Together those answer "what is this session working on" without becoming a live window onto anyone's work. Widening this to recent turns or transcript excerpts should be a deliberate later decision, not a drift.

## Where content comes from

**From transcripts, which the renderer already reads for cost — never from the broker.** Having the broker retain delivered messages so the dashboard could ask it would be simpler, and is rejected: it turns a router into a content store holding every project's task text in one process, rebuilding the cross-scope aggregation point one layer down where it is far less visible. Keeping the broker content-free also keeps ADR-0003 honest — after delivery it owes the message nothing.

## Consequences

The renderer's data model is JSON, rendered to markdown for reading. That JSON is a machine-readable aggregate of several sessions' material, so it is ephemeral by default, written to an explicit path only when asked, and **the writer refuses to write inside any git working tree** — which is what stops one client's content being committed into another client's repository, the realistic form of this leak rather than the dramatic one.

Note the mechanism reads more than it displays: the transcript parser returns the first message and an issue tag whether or not they are wanted. Content staying out of a view is therefore a property that has to be maintained deliberately, not one the code enforces by construction.
