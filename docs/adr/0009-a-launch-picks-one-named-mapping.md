---
status: accepted
---

# A launch picks one named mapping, not a slot and an effort

Starting a participant needs several facts to agree: which launcher runs, which model slot it asks Claude Code for, what effort it requests, and what the roster should then advertise. Asking the human for those facts separately is what issue #10 was filed about — four values retyped per window, and a roster advertising a model slot the session was not running at. The fix is not better advice about keeping them in sync. It is to **remove the second place they can be stated**: the human picks one entry from a list, and every derived value comes from that entry.

The entries live in a JSON manifest at `${XDG_CONFIG_HOME:-~/.config}/ccd/mappings.json`, overridable with `$CCD_MAPPINGS`. Each carries `id`, `slot`, `effort`, `launcher`, `model`, `display` and optional `notes`. A launch therefore reduces to one choice, and the launched session carries one environment variable naming it — with one variable there is structurally nothing to fall out of sync.

Stating slot and effort separately at the prompt was the obvious alternative and is rejected on the same grounds the issue reports: any interface that accepts two independent values accepts two that disagree, and no amount of validation downstream can tell an intended unusual pairing from a typo. A picked name cannot disagree with itself.

## Why the slot carries no effort

A **model slot** is exactly one of `fable`, `opus`, `sonnet`, `haiku` — the four names Claude Code's `--model` accepts — and it carries no effort. Effort is its own field with exactly one value per entry. Writing a slot as `opus/high` was considered and rejected outright: it puts an effort inside a name, so the name and the `effort` field can then contradict each other. That is issue #10's defect one level up, and moving it from the environment into a config file would make it permanent rather than fixing it.

This is a vocabulary decision as much as a schema one. "Tier" previously meant the slot, the effort, and the pair of them depending on the sentence, which is why it no longer appears anywhere in this repo.

## Effort is requested, never guaranteed

The manifest records what will be **asked for**, and the documentation has to say so plainly rather than implying the value is what runs. Claude Code emits effort as a thinking budget, that budget can be silently downgraded server-side for some models, and a non-Anthropic backend reinterprets it against its own scale. Every model the deployment layer currently maps advertises `reasoning`/`reasoning_effort` on its provider, and that provider cross-maps a token budget to an effort level — but whether the Anthropic-compatible `/v1/messages` hop performs that translation is undocumented, and was deliberately not tested rather than guessed at.

So no component infers an effort. The operator sets each entry's effort by hand from their own benchmark reading, `ccd` reports what was requested, and the observed value (`$CLAUDE_EFFORT`, which Claude Code sets per turn *after* any downgrade) is compared against it and shown as drift. Requested and observed are two different facts and the tooling never collapses them into one.

## Why the id is derived from the real model

An entry's `id` is derived from the model that actually serves the session and its effort — `kimi-k3-max`, not `openrouter-opus`. The obvious alternative, `<launcher>-<slot>`, was rejected because it names the disguise rather than the thing: under a remapped backend the slot is what Claude Code is *told*, and several entries can share one slot while being served by entirely different models. Ids built from the slot would therefore collide precisely where the distinction matters most, and would have to be disambiguated with a suffix that carries no meaning.

Deriving from the real model also makes the id survive a re-pointing of the slot. If an entry later asks for `sonnet` instead of `opus` because that is what the backend maps cleanly, `kimi-k3-max` still names the same thing, so anything that recorded the id — a roster entry, a transcript, an issue comment — stays true.

The id is a stable handle for humans and tooling, not a display string; `display` carries the human-readable form and the picker shows the authoritative `slot`, `effort` and `model` beside it, so a `display` that drifts is visible rather than believed.

## Why the deployment layer produces the file

The **schema is public and the file is private**. This repo defines the shape, documents it, validates it and ships a reference validator; it ships no manifest and no launcher names. Populating it — which launchers exist on a host, which models they reach, which API key is behind them — is deployment, and this repo already keeps host-specific paths, credentials, handles and launcher names out (see README's *What this repo is not*).

That split is what keeps `ccd launch` backend-orthogonal: it never learns what a backend *is*, it execs a bare name the manifest supplies and resolves on `$PATH`. Shipping a starter manifest was rejected for the same reason a pricing table was rejected in ADR-0008 — a plausible-looking default that is wrong for the reader's machine is worse than an absent one, and here it would additionally be the first host-specific fact in a repo that has none.

The file being machine-rendered rather than hand-edited is also why it is JSON. `ccd` already shells to `python3` for every RPC, so JSON parses with no new dependency, and a producing layer emits it without a templating hazard. A more human-friendly format would be buying editability for a file nobody is supposed to edit by hand.

## Where validation lives

In the **reader**, never in the broker. `ccd` validates the closed slot set, the closed effort set, unique ids and a bare-name launcher when it loads the manifest. The broker learns none of this vocabulary: it treats roster text as opaque, which is what lets it stay agnostic about backends that do not exist yet, and ADR-0004's line about keeping configuration out of the broker applies unchanged.

Shape and availability are validated at different moments. A manifest is well-formed or not on any machine, so shape is checked at load; whether a named launcher exists on `$PATH` depends on the host, so it is resolved at launch. A controller that renders a manifest for a host it is not itself can therefore still validate what it produced.

## Consequences

A first-party entry pins a concrete model id (`claude-sonnet-5` for the `sonnet` slot) so that the drift comparison has something to compare against. Model ids move, so that value will eventually go stale — deliberately, because a stale pin shows up as visible drift on the roster rather than as silence. The alternative, leaving `model` empty for first-party entries, would make exactly the entries a reader most expects to be checked the ones that never are.

Every launcher named by a manifest must accept Claude Code's own `--model`/`--effort`/`--name` flags, since that is how the picked entry reaches the session. That is not a new constraint — a remapped launcher is already a wrapper that sets its environment and execs `claude` — but it is now load-bearing, and a launcher that swallowed those flags would produce precisely the mismatch this record exists to prevent.
