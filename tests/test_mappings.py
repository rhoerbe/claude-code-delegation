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
# compares, so the two cannot drift apart without this suite saying so. One
# first-party entry and one remapped entry are the minimum that proves the
# shape carries both.
EXAMPLE = {
    "schema": 1,
    "mappings": [
        {
            "id": "sonnet-5-medium",
            "slot": "sonnet",
            "effort": "medium",
            "launcher": "claude",
            "model": "claude-sonnet-5",
            "display": "Sonnet/Medium",
        },
        {
            "id": "opus-5-high",
            "slot": "opus",
            "effort": "high",
            "launcher": "claude",
            "model": "claude-opus-5",
            "display": "Opus/High",
        },
        {
            "id": "kimi-k3-max",
            "slot": "opus",
            "effort": "max",
            "launcher": "claude-openrouter",
            "model": "moonshotai/kimi-k3",
            "display": "Opus/Max → Kimi-K3",
            "notes": "effort set by hand from benchmark reading; requested, not guaranteed",
        },
        {
            "id": "glm-5.3-flash-high",
            "slot": "sonnet",
            "effort": "high",
            "launcher": "claude-openrouter",
            "model": "z-ai/glm-5.3-flash",
            "display": "Sonnet/High → GLM-5.3-Flash",
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
    base = {
        "id": "sonnet-5-medium",
        "slot": "sonnet",
        "effort": "medium",
        "launcher": "claude",
        "model": "claude-sonnet-5",
        "display": "Sonnet/Medium",
    }
    base.update(over)
    return base


def doc(*entries) -> dict:
    return {"schema": 1, "mappings": list(entries) or [entry()]}


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
    print("the worked example in README.md is valid:")
    check("example validates clean", m.validate(EXAMPLE) == [],
          repr(m.validate(EXAMPLE)))

    readme = Path(__file__).resolve().parent.parent / "README.md"
    blocks = re.findall(r"```json\n(.*?)```", readme.read_text(encoding="utf-8"),
                        re.S)
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
    check("every id is unique",
          len({e["id"] for e in EXAMPLE["mappings"]}) == len(EXAMPLE["mappings"]))
    # ADR-0009: the display is presentation, the fields are the truth — but an
    # example that contradicts itself would teach the wrong thing.
    kimi = m.find(EXAMPLE, "kimi-k3-max")
    check("the example's display agrees with its own effort",
          "Max" in kimi["display"] and kimi["effort"] == "max", repr(kimi))

    print("\nthe closed sets are closed:")
    for slot in m.SLOTS:
        check(f"slot {slot!r} accepted", m.validate(doc(entry(slot=slot))) == [])
    check("slot 'gpt' refused", only(m.validate(doc(entry(slot="gpt"))), "'slot'"))
    check("slot 'Opus' refused (case matters)",
          only(m.validate(doc(entry(slot="Opus"))), "'slot'"))
    for effort in m.EFFORTS:
        check(f"effort {effort!r} accepted",
              m.validate(doc(entry(effort=effort))) == [])
    check("effort 'extreme' refused",
          only(m.validate(doc(entry(effort="extreme"))), "'effort'"))

    print("\na slot never carries an effort (ADR-0009, the #10 defect one level up):")
    problems = m.validate(doc(entry(slot="opus/high")))
    check("slot 'opus/high' is refused", len(problems) == 1, repr(problems))
    check("and the message says where the effort belongs",
          problems and "put it in 'effort'" in problems[0], repr(problems))

    print("\nids are unique and shell-safe:")
    dupe = m.validate(doc(entry(), entry(display="second")))
    check("a duplicate id is refused", any("duplicate" in p for p in dupe), repr(dupe))
    check("the duplicate names both positions",
          any("mappings[0]" in p for p in dupe), repr(dupe))
    check("uppercase id refused", only(m.validate(doc(entry(id="Sonnet-5"))), "'id'"))
    check("id with a space refused", only(m.validate(doc(entry(id="sonnet 5"))), "'id'"))
    check("id with a slash refused", only(m.validate(doc(entry(id="a/b"))), "'id'"))
    check("dots allowed (a model version is not a typo)",
          m.validate(doc(entry(id="glm-5.3-flash-high"))) == [])

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
    check("resolve_launcher finds a real one",
          m.resolve_launcher(entry(launcher="sh")).endswith("sh"))

    print("\nrequired fields, and unknown ones:")
    for field in m.REQUIRED:
        missing = {k: v for k, v in entry().items() if k != field}
        check(f"missing {field!r} refused",
              any(f"'{field}'" in p for p in m.validate(doc(missing))))
        blank = entry(**{field: "  "})
        check(f"blank {field!r} refused",
              any(f"'{field}'" in p for p in m.validate(doc(blank))))
    check("'notes' is optional", m.validate(doc(entry())) == [])
    check("'notes' must be a string when present",
          only(m.validate(doc(entry(notes=7))), "'notes'"))
    # The file is machine-rendered: a producing layer that learns a new field
    # must not break every older reader that does not know it yet.
    check("an unknown entry key is ignored, not refused",
          m.validate(doc(entry(colour="blue"))) == [])
    check("an unknown top-level key is ignored, not refused",
          m.validate({"schema": 1, "mappings": [entry()], "generated_by": "x"}) == [])

    print("\nthe envelope:")
    check("a bare array is refused (no schema to version it)",
          only(m.validate([entry()]), "top level"))
    check("missing 'schema' refused", any("'schema'" in p for p in m.validate(
        {"mappings": [entry()]})))
    check("a future schema refused with an upgrade hint",
          any("upgrade ccd" in p for p in m.validate(
              {"schema": m.SCHEMA + 1, "mappings": [entry()]})))
    check("an older schema is accepted",
          m.validate({"schema": 1, "mappings": [entry()]}) == [])
    check("'mappings' must be an array",
          only(m.validate({"schema": 1, "mappings": {}}), "'mappings'"))
    check("an empty 'mappings' is refused",
          only(m.validate({"schema": 1, "mappings": []}), "nothing to pick"))

    print("\nevery problem is reported, not just the first:")
    bad = m.validate(doc(entry(id="Bad", slot="nope", effort="nope")))
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
    check("a good manifest loads", len(m.entries(loaded)) == 4)
    check("file order is presentation order",
          [e["id"] for e in m.entries(loaded)]
          == [e["id"] for e in EXAMPLE["mappings"]])
    check("find() returns the entry", m.find(loaded, "kimi-k3-max")["model"]
          == "moonshotai/kimi-k3")
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

    invalid = write(tmp, "invalid.json", doc(entry(slot="gpt", effort="nope")))
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
