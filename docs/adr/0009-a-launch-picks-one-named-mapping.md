---
status: accepted
---

# A launch picks one named mapping, not a slot and an effort

Starting a participant needs several facts to agree: which launcher runs, which model slot it asks Claude Code for, what effort it requests, and what the roster should then advertise. Asking the human for those facts separately is what issue #10 was filed about — four values retyped per window, and a roster advertising a model slot the session was not running at. The fix is not better advice about keeping them in sync. It is to **remove the second place they can be stated**: the human picks one entry from a list, and every derived value comes from that entry.

The entries live in a JSON manifest at `${XDG_CONFIG_HOME:-~/.config}/ccd/mappings.json`, overridable with `$CCD_MAPPINGS`. Each stores a `launcher` and a `model`, an `effort` where one applies, and optional `notes`. The id and the label are derived from those. A launch therefore reduces to one choice, and the launched session carries one environment variable naming it — with one variable there is structurally nothing to fall out of sync.

Stating slot and effort separately at the prompt was the obvious alternative and is rejected on the same grounds the issue reports: any interface that accepts two independent values accepts two that disagree, and no amount of validation downstream can tell an intended unusual pairing from a typo. A picked name cannot disagree with itself.

## Superseded: why the slot carried no effort

**This section is kept for the record. The slot was removed entirely by the revision below; what follows was the reasoning while the field existed.**

A **model slot** is exactly one of `fable`, `opus`, `sonnet`, `haiku` — the four names Claude Code's `--model` accepts — and it carries no effort. Effort is its own field with exactly one value per entry. Writing a slot as `opus/high` was considered and rejected outright: it puts an effort inside a name, so the name and the `effort` field can then contradict each other. That is issue #10's defect one level up, and moving it from the environment into a config file would make it permanent rather than fixing it.

This is a vocabulary decision as much as a schema one. "Model slot" replaces an older term that meant the slot, the effort, or the pair of them depending on the sentence — which is why that term was retired from this repo rather than redefined (issue #10).

## Revision: the slot is gone; models are named directly

A probe settled this after the section above was written. Passing a full provider slug straight to `--model` **works**: a remapped launcher invoked as `--model deepseek/deepseek-v4.1-flash` answered, and the transcript recorded `model: deepseek/deepseek-v4.1-flash`. The control arm matters more than the result: the *same* launcher reached through the slot mechanism, `--model sonnet`, produced the **identical** out-of-catalog warning and the same assumed 200k context window. So the slot bought nothing — Claude Code's warning is about the resolved model id, which is outside its catalog either way.

The slot was therefore only ever one route to a model, and naming the model directly reaches it too. It also reaches *any* model the provider offers, rather than the handful a launcher happens to have been rendered with, which is a real gain rather than a tidy-up. The field is removed, not merely discouraged, so nothing can state a slot that contradicts the model beside it.

What survives from the superseded reasoning is its principle: a name must not carry a setting. That principle now applies one level up — the id and the label are derived from the fields rather than stored next to them.

## Effort is optional, because sometimes it is never sent

Effort is **stored only where it applies**, and omitted otherwise. Evidence, not caution: across every transcript on the machine this was checked on, all twelve `claude-haiku-4-5` records carry `effort: null` — including two sessions launched with an explicit `--effort low` and `--effort xhigh`. The client documents the mechanism it is using there (`effort_unsupported`): it drops effort when the model rejects it and stops sending it from the next turn onward.

So for those models effort is not merely ignored downstream, it never leaves the client. An entry claiming one would be fiction, and a manifest that records fiction is worse than one that records nothing — a reader has no way to tell the two apart. Making the field optional is what lets the file say "no effort here" rather than picking a plausible value.

## The `[1m]` context suffix lives in the model string

Any model outside Claude Code's catalog — every third-party slug here — makes the session assume a 200k context window for auto-compaction, however it was reached. Appending `[1m]` to the model name asks for 1M instead, and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` sets an exact value.

Because `model` is passed verbatim to `--model`, the suffix belongs in that string rather than in a field of its own. A separate `context` field was rejected: it would be a second way to say something the model string already says, which is the shape of defect this whole record exists to remove. The cost is that omitting the suffix is silent — nothing fails, long sessions simply compact early against a window smaller than the model has — so the documentation states it rather than leaving it to be discovered.

## Effort is requested, never guaranteed

The manifest records what will be **asked for**, and the documentation has to say so plainly rather than implying the value is what runs. Claude Code emits effort as a thinking budget, that budget can be silently downgraded server-side for some models, and a non-Anthropic backend reinterprets it against its own scale. Every model the deployment layer currently maps advertises `reasoning`/`reasoning_effort` on its provider, and that provider cross-maps a token budget to an effort level — but whether the Anthropic-compatible `/v1/messages` hop performs that translation is undocumented, and was deliberately not tested rather than guessed at.

So no component infers an effort. The operator sets each entry's effort by hand from their own benchmark reading, `ccd` reports what was requested, and the observed value (`$CLAUDE_EFFORT`, which Claude Code sets per turn *after* any downgrade) is compared against it and shown as drift. Requested and observed are two different facts and the tooling never collapses them into one.

## Why the id is computed, not written

An entry's `id` is **derived** from its model and effort — `kimi-k3-max`, `deepseek-v4.1-flash` where there is no effort — and lowercased into one shell-safe word, since it becomes `$CCD_MAPPING` in the launched session.

Storing it was the original design and is rejected for the same reason the stored label is: a hand-written id can say `kimi-k3-max` on an entry that runs `high`. That is issue #10's defect in miniature, and a file whose purpose is to stop a name contradicting a setting should not contain a hand-written name.

Deriving from the model rather than the launcher is what keeps the id meaningful. A launcher is a route, and the same model is often reachable through more than one; an id built from the route would name the disguise rather than the thing. Where two entries genuinely do reach the same model at the same effort through different launchers, **both** ids carry their launcher rather than only the second — so reordering the file cannot rename an entry, and anything that recorded an id stays true. Two entries agreeing on launcher, model *and* effort are a duplicate, and that is an error rather than a naming problem.

The `[1m]` suffix survives into the id (`kimi-k3-1m-max`), because an entry with it and one without are different mappings and must not collide.

## Why the label is derived, not stored

An entry's human-readable label — `kimi-k3/max`, `deepseek-v4.1-flash` — is **computed from `model` and `effort`**, by one function every renderer calls. There is no `display` field and no override.

Storing the label was the first design and is rejected: it is free text sitting beside the fields it claims to describe, so an entry can say `effort: "max"` and show "High". That is precisely this record's own defect one level down, and an epic whose purpose is to make a class of mistake impossible should not reintroduce it in the file that fixes it. Deriving makes the disagreement unrepresentable rather than merely discouraged, and one shared function keeps a picker and any later renderer from diverging.

The label is the model plus the effort when there is one — `kimi-k3/max`, `claude-sonnet-5/medium` — and the model alone when there is not: `deepseek-v4.1-flash`. Showing nothing where no effort is sent is the honest rendering; there is no value to display.

Model ids are rendered **as their provider writes them**, shortened to the last path segment and otherwise untouched. Title-casing them was considered and rejected: it needs no table, but it invents a name — `glm-5.3-flash` is not `Glm-5.3-Flash` to anyone — and it breaks the property that matters more, which is that the label matches the string the same reader meets in the manifest's `model` field, in `ccd ls`, and in the transcript. A label nobody can grep for is a small recurring cost paid for a cosmetic gain. Dropping the provider prefix is shortening rather than renaming, and the full id sits one column away.

Labels disambiguate exactly as ids do, and are derived over the whole file for the same reason one entry cannot see a collision. A label exists so a human can choose from it, so two identical rows in a picker mean the choice cannot be made from the label at all — a display that fails to identify the thing is this record's own defect wearing different clothes. Where entries share a label, every member of that group carries its launcher, which also keeps label and id in lockstep so anything quoting one can be matched to the other. Appending the launcher unconditionally was rejected: it makes every label noisier to fix a case that usually does not arise.

An entry that genuinely needs a human aside carries `notes`. The difference that matters is that `notes` is visibly not the label, so nobody reads it as authoritative.

## Why the deployment layer produces the file

The **schema is public and the file is private**. This repo defines the shape, documents it, validates it and ships a reference validator; it ships no manifest and no launcher names. Populating it — which launchers exist on a host, which models they reach, which API key is behind them — is deployment, and this repo already keeps host-specific paths, credentials, handles and launcher names out (see README's *What this repo is not*).

That split is what keeps `ccd launch` backend-orthogonal: it never learns what a backend *is*, it execs a bare name the manifest supplies and resolves on `$PATH`. Shipping a starter manifest was rejected for the same reason a pricing table was rejected in ADR-0008 — a plausible-looking default that is wrong for the reader's machine is worse than an absent one, and here it would additionally be the first host-specific fact in a repo that has none.

The file being machine-rendered rather than hand-edited is also why it is JSON. `ccd` already shells to `python3` for every RPC, so JSON parses with no new dependency, and a producing layer emits it without a templating hazard. A more human-friendly format would be buying editability for a file nobody is supposed to edit by hand.

## Where validation lives

In the **reader**, never in the broker. `ccd` validates the closed effort set, a bare-name launcher, retired keys and id collisions when it loads the manifest. The broker learns none of this vocabulary: it treats roster text as opaque, which is what lets it stay agnostic about backends that do not exist yet, and ADR-0004's line about keeping configuration out of the broker applies unchanged.

Shape and availability are validated at different moments. A manifest is well-formed or not on any machine, so shape is checked at load; whether a named launcher exists on `$PATH` depends on the host, so it is resolved at launch. A controller that renders a manifest for a host it is not itself can therefore still validate what it produced.

Unknown keys are ignored and retired keys are refused, which are two halves of one rule rather than a contradiction. Ignoring an unknown key buys **forward** compatibility: an older reader survives a newer producer that has learned a field. `slot`, `id` and `display` are the **backward** case — known-dead keys from an older producer — and there silence is the expensive answer, because a stale manifest would validate clean while the intent encoded in it is dropped on the floor. Refusing names the key and asks for the file to be regenerated, which is the only action that actually fixes it.

## Consequences

Every entry pins a concrete model id, first-party ones included, so the drift comparison has something to compare against. Model ids move, so those values will eventually go stale — deliberately, because a stale pin shows up as visible drift on the roster rather than as silence.

Because the manifest now names models rather than slots, it also has to be regenerated when a provider renames one. That is the price of naming the thing instead of a route to it, and it is paid by the layer that renders the file rather than by a human editing it.

Every launcher named by a manifest must accept Claude Code's own `--model`/`--effort`/`--name` flags, since that is how the picked entry reaches the session. That is not a new constraint — a remapped launcher is already a wrapper that sets its environment and execs `claude` — but it is now load-bearing, and a launcher that swallowed those flags would produce precisely the mismatch this record exists to prevent.
