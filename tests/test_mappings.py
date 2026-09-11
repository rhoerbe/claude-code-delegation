#!/usr/bin/env python3
"""The mapping manifest contract (ADR-0009, issue #13).

Everything here runs against manifests written into a temporary directory, so
the suite never reads the machine's real `~/.config/ccd/mappings.json` and
never needs a broker socket.

Run: tests/test_mappings.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccd_mappings import manifest as m  # noqa: E402

FAILURES: list[str] = []

# The worked example from README.md. `run()` re-reads it out of the README and
# compares, so the two cannot drift apart without this suite saying so. It
# carries all four shapes the contract has to handle: first-party with an
# effort, first-party with none, a remapped model with the `[1m]` suffix, and
# a remapped model with no effort.
EXAMPLE = {
    "schema": 1,
    "mappings": [
        {
            "launcher": "claude",
            "model": "claude-sonnet-5",
            "effort": "medium",
        },
        {
            "launcher": "claude",
            "model": "claude-opus-5",
            "effort": "high",
        },
        {
            "launcher": "claude",
            "model": "claude-haiku-4-5",
            "notes": "no effort: this model never receives one, so claiming a value would be fiction",
        },
        {
            "launcher": "claude-openrouter",
            "model": "moonshotai/kimi-k3[1m]",
            "effort": "max",
            "notes": "effort set by hand from benchmark reading; [1m] lifts the assumed 200k window",
        },
        {
            "launcher": "claude-openrouter",
            "model": "deepseek/deepseek-v4.1-flash",
        },
    ],
}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        FAILURES.append(name)


def entry(**over) -> dict:
    base = {"launcher": "claude", "model": "claude-sonnet-5", "effort": "medium"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def doc(*items) -> dict:
    return {"schema": 1, "mappings": list(items) or [entry()]}


def only(problems: list, needle: str) -> bool:
    return len(problems) == 1 and needle in problems[0]


def write(tmp: Path, name: str, text) -> Path:
    path = tmp / name
    path.write_text(
        text if isinstance(text, str) else json.dumps(text, indent=2),
        encoding="utf-8",
    )
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ccd-map-"))
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: Path) -> int:
    readme = Path(__file__).resolve().parent.parent / "README.md"
    readme_text = readme.read_text(encoding="utf-8")

    print("the worked example in README.md is valid:")
    check("example validates clean", m.validate(EXAMPLE) == [],
          repr(m.validate(EXAMPLE)))
    blocks = re.findall(r"```json\n(.*?)```", readme_text, re.S)
    check("README.md carries exactly one json block", len(blocks) == 1,
          f"found {len(blocks)}")
    if len(blocks) == 1:
        published = json.loads(blocks[0])
        check("README's example is the one tested here", published == EXAMPLE,
              "README.md and EXAMPLE have drifted apart")
        check("and it validates as published", m.validate(published) == [],
              repr(m.validate(published)))
    check("it carries a first-party entry (bare `claude`)",
          any(e["launcher"] == "claude" for e in EXAMPLE["mappings"]))
    check("it carries a remapped entry (another launcher)",
          any(e["launcher"] != "claude" for e in EXAMPLE["mappings"]))
    check("it carries an entry with no effort",
          any("effort" not in e for e in EXAMPLE["mappings"]))
    check("no entry stores an id, a label or a slot", not any(
        k in e for e in EXAMPLE["mappings"] for k in ("id", "display", "slot")))

    # README prints what a picker makes of that file. Read both back rather
    # than restating them, so neither can drift from the derivation.
    listing = re.search(r"A picker\nrenders it as:\n\n```\n(.*?)```", readme_text, re.S)
    check("README shows the rendered picker listing", listing is not None)
    if listing:
        shown = [line.split(". ", 1)[1]
                 for line in listing.group(1).strip().splitlines()]
        derived = m.labels(EXAMPLE)
        check("the example renders as the listing README publishes",
              shown == derived, f"README {shown!r} != derived {derived!r}")
    for ident in m.ids(EXAMPLE):
        check(f"README names the derived id {ident!r}", f"`{ident}`" in readme_text)

    print("\nthe model is named directly; there is no slot:")
    # ADR-0009's revision: a full provider slug is a valid --model argument, so
    # the slot was only ever one route to a model. The field is gone, and a
    # file still carrying it predates this contract.
    check("'slot' is not a stored key", "slot" not in m.REQUIRED + m.OPTIONAL)
    check("a leftover 'slot' is refused, not ignored",
          only(m.validate(doc(entry(slot="opus"))), "'slot' is no longer a field"))
    check("and the message says to regenerate",
          "regenerate" in m.validate(doc(entry(slot="opus")))[0])
    check("a leftover 'id' is refused",
          only(m.validate(doc(entry(id="sonnet-5-medium"))), "'id' is no longer"))
    check("a leftover 'display' is refused",
          only(m.validate(doc(entry(display="Opus/High"))), "'display' is no longer"))
    check("a provider slug is an ordinary model value",
          m.validate(doc(entry(model="deepseek/deepseek-v4.1-flash"))) == [])

    print("\neffort is optional, because for some models it is never sent:")
    check("an entry with no effort is valid",
          m.validate(doc(entry(effort=None))) == [])
    for effort in m.EFFORTS:
        check(f"effort {effort!r} accepted",
              m.validate(doc(entry(effort=effort))) == [])
    check("effort 'extreme' refused",
          only(m.validate(doc(entry(effort="extreme"))), "'effort'"))
    check("effort 'Max' refused (case matters)",
          only(m.validate(doc(entry(effort="Max"))), "'effort'"))
    # Absent means "none is sent". An empty string is a value that says
    # nothing, which is the fiction the optional field exists to avoid.
    check("an empty effort is refused rather than treated as absent",
          only(m.validate(doc(entry(effort=""))), "omitted entirely"))
    # `null` is how a transcript records "no effort was sent", so it says the
    # same thing as an absent key and is accepted as such.
    check("a null effort means absent, like an omitted key",
          m.validate(doc({"launcher": "claude", "model": "x",
                          "effort": None})) == [])
    check("and renders as an entry with no effort",
          m.display({"model": "x", "effort": None}) == "x")

    print("\nthe id is derived from model and effort:")
    check("model plus effort", m.entry_id(entry()) == "claude-sonnet-5-medium")
    check("model alone when there is no effort",
          m.entry_id(entry(effort=None)) == "claude-sonnet-5")
    check("the provider prefix is dropped",
          m.entry_id(entry(model="moonshotai/kimi-k3", effort="max"))
          == "kimi-k3-max")
    check("uppercase is folded down",
          m.entry_id(entry(model="deepseek/DeepSeek-V4", effort="low"))
          == "deepseek-v4-low")
    check("the [1m] suffix survives into the id",
          m.entry_id(entry(model="moonshotai/kimi-k3[1m]", effort="max"))
          == "kimi-k3-1m-max")
    # An entry with the suffix and one without are different mappings; if the
    # suffix were stripped they would collide and be reported as duplicates.
    suffixed = doc(entry(model="moonshotai/kimi-k3", effort="max"),
                   entry(model="moonshotai/kimi-k3[1m]", effort="max"))
    check("so [1m] and plain are distinct mappings, not a collision",
          m.validate(suffixed) == [] and len(set(m.ids(suffixed))) == 2,
          repr(m.ids(suffixed)))
    check("every derived id matches the id pattern",
          all(m.ID_RE.match(i) for i in m.ids(EXAMPLE)), repr(m.ids(EXAMPLE)))
    check("a model yielding no usable id is refused",
          only(m.validate(doc(entry(model="!!!"))), "usable id"))

    print("\ncolliding ids take the launcher — all of them, not just the second:")
    clash = doc(entry(launcher="claude-openrouter", model="moonshotai/kimi-k3",
                      effort="max"),
                entry(launcher="claude-alt", model="moonshotai/kimi-k3",
                      effort="max"))
    check("both entries carry their launcher",
          m.ids(clash) == ["kimi-k3-max-claude-openrouter",
                           "kimi-k3-max-claude-alt"], repr(m.ids(clash)))
    check("and the file still validates", m.validate(clash) == [])
    # Order-independence is the point: if only the later entry were renamed,
    # reordering the file would rename an entry that anything may have recorded.
    reversed_clash = {"schema": 1, "mappings": list(reversed(clash["mappings"]))}
    check("reordering the file does not rename anything",
          sorted(m.ids(clash)) == sorted(m.ids(reversed_clash)),
          repr(m.ids(reversed_clash)))
    check("an entry that does not collide keeps the short id",
          m.ids(doc(entry(launcher="claude-openrouter",
                          model="moonshotai/kimi-k3", effort="max")))
          == ["kimi-k3-max"])
    dupe = doc(entry(), entry(notes="same launcher, model and effort"))
    check("a true duplicate is refused",
          any("both derive the id" in p for p in m.validate(dupe)),
          repr(m.validate(dupe)))
    check("and it names the entry it duplicates",
          any("mappings[0]" in p for p in m.validate(dupe)))

    print("\nthe label is derived, so it cannot contradict the fields:")
    check("model and effort",
          m.display(entry(model="moonshotai/kimi-k3", effort="max"))
          == "kimi-k3/max")
    check("model alone when no effort is sent",
          m.display(entry(model="deepseek/deepseek-v4.1-flash", effort=None))
          == "deepseek-v4.1-flash")
    check("first-party reads the same way",
          m.display(entry()) == "claude-sonnet-5/medium")
    check("the [1m] suffix is shown, since it is part of the model asked for",
          m.display(entry(model="moonshotai/kimi-k3[1m]", effort="max"))
          == "kimi-k3[1m]/max")
    check("a stored 'display' is ignored, not honoured",
          m.display(entry(display="haiku/low")) == "claude-sonnet-5/medium")
    # The label has to stay greppable: this same string is the manifest's
    # `model`, `ccd ls`'s resolved-model column, and the transcript. Restyling
    # it would need no table but would invent a name.
    check("the model id is rendered exactly as its provider writes it", all(
        m.display({"model": mid, "effort": "max"}).split("/max")[0]
        == mid.rsplit("/", 1)[-1]
        for mid in ("moonshotai/kimi-k3", "z-ai/glm-5.3-flash", "GLM-4.6",
                    "deepseek/DeepSeek-V4", "mistral-large-2512")))

    print("\ncolliding labels take the launcher, exactly as ids do:")
    # A label exists so a human can choose from it; two identical rows mean the
    # choice cannot be made from the label at all.
    alone = doc(entry(launcher="claude-openrouter", model="moonshotai/kimi-k3",
                      effort="max"),
                entry())
    check("outside a collision the label stays short",
          m.labels(alone) == ["kimi-k3/max", "claude-sonnet-5/medium"],
          repr(m.labels(alone)))
    both = doc(entry(launcher="claude-openrouter", model="moonshotai/kimi-k3",
                     effort="max"),
               entry(launcher="claude-alt", model="moonshotai/kimi-k3",
                     effort="max"),
               entry())
    check("in a collision every member carries its launcher",
          m.labels(both) == ["kimi-k3/max (claude-openrouter)",
                             "kimi-k3/max (claude-alt)",
                             "claude-sonnet-5/medium"], repr(m.labels(both)))
    check("the uninvolved entry is untouched",
          m.labels(both)[2] == "claude-sonnet-5/medium")
    check("no two labels in a file are equal",
          len(set(m.labels(both))) == len(m.labels(both)))
    # Same property the ids have, and for the same reason: anything that
    # quoted a label must not be invalidated by a later reordering.
    flipped = {"schema": 1, "mappings": list(reversed(both["mappings"]))}
    check("reordering the file does not relabel anything",
          sorted(m.labels(both)) == sorted(m.labels(flipped)),
          repr(m.labels(flipped)))
    check("labels and ids collide on the same entries, so they stay in lockstep",
          [l.endswith(")") for l in m.labels(both)]
          == [i.endswith(("-claude-openrouter", "-claude-alt"))
              for i in m.ids(both)],
          f"{m.labels(both)!r} vs {m.ids(both)!r}")

    print("\nlauncher is a bare name, resolved on $PATH at launch:")
    check("a path is refused",
          only(m.validate(doc(entry(launcher="/usr/bin/claude"))), "bare name"))
    check("a relative path is refused",
          only(m.validate(doc(entry(launcher="./claude"))), "bare name"))
    check("a leading dash is refused",
          only(m.validate(doc(entry(launcher="--model"))), "option"))
    check("a bare name is accepted", m.validate(doc(entry(launcher="claude-x"))) == [])
    # Shape is host-independent; availability is not. A manifest naming a
    # launcher this machine lacks is still a valid manifest.
    check("validate does not require the launcher to exist",
          m.validate(doc(entry(launcher="definitely-not-installed"))) == [])
    try:
        m.resolve_launcher(entry(launcher="definitely-not-installed"))
        check("resolve_launcher rejects a missing launcher", False, "no raise")
    except m.ManifestError as exc:
        check("resolve_launcher rejects a missing launcher", "$PATH" in str(exc))
        check("and names the mapping by its derived id",
              "claude-sonnet-5-medium" in str(exc), str(exc))
    check("resolve_launcher finds a real one",
          m.resolve_launcher(entry(launcher="sh")).endswith("sh"))

    print("\nrequired fields, and unknown ones:")
    for field in m.REQUIRED:
        missing = {k: v for k, v in entry().items() if k != field}
        check(f"missing {field!r} refused",
              any(f"'{field}'" in p for p in m.validate(doc(missing))))
        check(f"blank {field!r} refused",
              any(f"'{field}'" in p for p in m.validate(doc(entry(**{field: "  "})))))
    check("'notes' is optional", m.validate(doc(entry())) == [])
    check("'notes' must be a string when present",
          only(m.validate(doc(entry(notes=7))), "'notes'"))
    # The file is machine-rendered: a producing layer that learns a new field
    # must not break every older reader. Retired keys are the exception.
    check("an unknown entry key is ignored, not refused",
          m.validate(doc(entry(colour="blue"))) == [])
    check("an unknown top-level key is ignored, not refused",
          m.validate({"schema": 1, "mappings": [entry()], "generated_by": "x"}) == [])

    print("\nthe envelope:")
    check("a bare array is refused (no schema to version it)",
          only(m.validate([entry()]), "top level"))
    check("missing 'schema' refused",
          any("'schema'" in p for p in m.validate({"mappings": [entry()]})))
    check("a future schema refused with an upgrade hint",
          any("upgrade ccd" in p for p in m.validate(
              {"schema": m.SCHEMA + 1, "mappings": [entry()]})))
    check("'mappings' must be an array",
          only(m.validate({"schema": 1, "mappings": {}}), "'mappings'"))
    check("an empty 'mappings' is refused",
          only(m.validate({"schema": 1, "mappings": []}), "nothing to pick"))

    print("\nevery problem is reported, not just the first:")
    bad = m.validate(doc(entry(slot="opus", effort="nope", notes=7)))
    check("three faults in one entry give three problems", len(bad) == 3, repr(bad))

    print("\nwhere the manifest lives:")
    env = {"CCD_MAPPINGS": str(tmp / "explicit.json")}
    check("$CCD_MAPPINGS wins", m.manifest_path(env) == tmp / "explicit.json")
    env = {"XDG_CONFIG_HOME": str(tmp / "cfg")}
    check("else $XDG_CONFIG_HOME/ccd/mappings.json",
          m.manifest_path(env) == tmp / "cfg" / "ccd" / "mappings.json")
    check("else ~/.config/ccd/mappings.json",
          m.manifest_path({}) == Path("~/.config/ccd/mappings.json").expanduser())
    check("an empty $CCD_MAPPINGS does not win",
          m.manifest_path({"CCD_MAPPINGS": "", "XDG_CONFIG_HOME": str(tmp)})
          == tmp / "ccd" / "mappings.json")

    print("\nloading:")
    good = write(tmp, "good.json", EXAMPLE)
    loaded = m.load(good)
    check("a good manifest loads", len(m.entries(loaded)) == 5)
    check("file order is presentation order",
          [e["model"] for e in m.entries(loaded)]
          == [e["model"] for e in EXAMPLE["mappings"]])
    check("find() resolves a derived id",
          m.find(loaded, "kimi-k3-1m-max")["model"] == "moonshotai/kimi-k3[1m]")
    check("find() resolves an id with no effort in it",
          m.find(loaded, "claude-haiku-4-5")["model"] == "claude-haiku-4-5")
    check("find() returns None for a stranger", m.find(loaded, "nope") is None)

    # "You have not set this up" and "what you set up is wrong" are different
    # messages to a human, so they are different exceptions.
    try:
        m.load(tmp / "absent.json")
        check("a missing manifest raises FileNotFoundError", False, "no raise")
    except FileNotFoundError as exc:
        check("a missing manifest raises FileNotFoundError", "absent.json" in str(exc))
    except m.ManifestError:
        check("a missing manifest raises FileNotFoundError", False, "got ManifestError")

    broken = write(tmp, "broken.json", "{not json")
    try:
        m.load(broken)
        check("malformed JSON raises ManifestError", False, "no raise")
    except m.ManifestError as exc:
        check("malformed JSON raises ManifestError", "not valid JSON" in str(exc))

    invalid = write(tmp, "invalid.json", doc(entry(effort="nope", notes=7)))
    try:
        m.load(invalid)
        check("an invalid manifest raises ManifestError", False, "no raise")
    except m.ManifestError as exc:
        check("an invalid manifest raises ManifestError", len(exc.problems) == 2,
              repr(exc.problems))
        check("and the message names the file", str(invalid) in str(exc))
        check("and lists every problem, not just the first",
              str(exc).count("\n  - ") == 2, str(exc))

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("PASS — the mapping manifest contract holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
