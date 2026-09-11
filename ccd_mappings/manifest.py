"""Read and validate the mapping manifest (ADR-0009).

The manifest is the one place a launch's facts are stated. `ccd` reads it and
validates it here; the broker never sees any of this vocabulary and must not
learn it (ADR-0004, ADR-0009).

Shape and availability are checked at different moments on purpose. A manifest
is well-formed or not on any machine, so `validate` runs at load; whether a
named launcher exists depends on the host, so `resolve_launcher` runs at launch.
That lets a controller validate a manifest it renders for a host it is not.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

SCHEMA = 1

# Claude Code's four `--model` names. Closed set: a slot the CLI cannot be
# given is a launch that fails after the window is already open.
SLOTS = ("fable", "opus", "sonnet", "haiku")

# Claude Code's `--effort` values, weakest first. Requested, never guaranteed
# — see ADR-0009. Order is meaningful only for display, never for comparison.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Lowercase so an id is one shell-safe word: it becomes `$CCD_MAPPING` in the
# launched session and is read back from rosters and transcripts.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

REQUIRED = ("id", "slot", "effort", "launcher", "model")
OPTIONAL = ("notes",)


class ManifestError(Exception):
    """A manifest that cannot be trusted. Carries every problem, not the first."""

    def __init__(self, path: Optional[str], problems: list):
        self.path = path
        self.problems = list(problems)
        where = f" in {path}" if path else ""
        super().__init__(
            f"mapping manifest{where} is invalid:\n  - "
            + "\n  - ".join(self.problems)
        )


def manifest_path(env: Optional[dict] = None) -> Path:
    """Where the manifest lives: `$CCD_MAPPINGS`, else the XDG config path."""
    env = os.environ if env is None else env
    override = (env.get("CCD_MAPPINGS") or "").strip()
    if override:
        return Path(override).expanduser()
    config = (env.get("XDG_CONFIG_HOME") or "").strip() or "~/.config"
    return Path(config).expanduser() / "ccd" / "mappings.json"


def validate(doc) -> list:
    """Return every problem with `doc`, or an empty list if it is well-formed.

    Unknown keys are *not* problems. The file is machine-rendered, so a
    producing layer that learns a new field must not break every older reader
    that does not know it yet.
    """
    problems = []
    if not isinstance(doc, dict):
        return [f"top level must be an object, not {type(doc).__name__}"]

    schema = doc.get("schema")
    if schema is None:
        problems.append("missing 'schema'")
    elif not isinstance(schema, int) or isinstance(schema, bool):
        problems.append(f"'schema' must be an integer, not {schema!r}")
    elif schema > SCHEMA:
        problems.append(
            f"'schema' is {schema} but this reader understands {SCHEMA}; "
            "upgrade ccd"
        )

    entries = doc.get("mappings")
    if not isinstance(entries, list):
        problems.append("'mappings' must be an array")
        return problems
    if not entries:
        problems.append("'mappings' is empty; there is nothing to pick")

    seen = {}
    for index, entry in enumerate(entries):
        label = f"mappings[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{label} must be an object")
            continue
        ident = entry.get("id")
        if isinstance(ident, str) and ident:
            label = f"mappings[{index}] ('{ident}')"

        for key in REQUIRED:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{label}: '{key}' must be a non-empty string")
        notes = entry.get("notes")
        if notes is not None and not isinstance(notes, str):
            problems.append(f"{label}: 'notes' must be a string when present")

        if isinstance(ident, str) and ident:
            if not ID_RE.match(ident):
                problems.append(
                    f"{label}: 'id' must match {ID_RE.pattern} — lowercase, "
                    "so it is one shell-safe word"
                )
            if ident in seen:
                problems.append(
                    f"{label}: duplicate 'id', already used by "
                    f"mappings[{seen[ident]}]"
                )
            else:
                seen[ident] = index

        slot = entry.get("slot")
        if isinstance(slot, str) and slot not in SLOTS:
            hint = ""
            if "/" in slot:
                hint = " — a slot carries no effort; put it in 'effort'"
            problems.append(
                f"{label}: 'slot' is {slot!r}, must be one of "
                f"{'/'.join(SLOTS)}{hint}"
            )

        effort = entry.get("effort")
        if isinstance(effort, str) and effort not in EFFORTS:
            problems.append(
                f"{label}: 'effort' is {effort!r}, must be one of "
                f"{'/'.join(EFFORTS)}"
            )

        launcher = entry.get("launcher")
        if isinstance(launcher, str) and launcher.strip():
            if os.sep in launcher or (os.altsep and os.altsep in launcher):
                problems.append(
                    f"{label}: 'launcher' must be a bare name resolved on "
                    f"$PATH, not a path ({launcher!r})"
                )
            elif launcher.startswith("-"):
                problems.append(
                    f"{label}: 'launcher' {launcher!r} would be read as an option"
                )

    return problems


def load(path=None, env: Optional[dict] = None) -> dict:
    """Load and validate the manifest. Raises `ManifestError` on any problem.

    A missing file raises `FileNotFoundError`, which callers distinguish from a
    malformed one: "you have not set this up" and "what you set up is wrong"
    are different messages to a human.
    """
    target = Path(path).expanduser() if path else manifest_path(env)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"no mapping manifest at {target}")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(str(target), [f"not valid JSON: {exc}"])
    problems = validate(doc)
    if problems:
        raise ManifestError(str(target), problems)
    return doc


def entries(doc: dict) -> list:
    """The mappings in file order — which is presentation order (ADR-0009)."""
    return list(doc.get("mappings") or [])


def find(doc: dict, ident: str) -> Optional[dict]:
    """The entry with this id, or None."""
    for entry in entries(doc):
        if entry.get("id") == ident:
            return entry
    return None


def display(entry: dict) -> str:
    """The human-readable label for an entry, derived rather than stored.

    A stored label can contradict the fields it describes, which is the defect
    of issue #10 one level down, so there is no `display` key and no override
    (ADR-0009). An entry needing a human aside carries `notes`, which is
    visibly not authoritative.

    Remapped, where the model is not what the slot names:

        Opus/Max → Kimi-K3

    First-party, where the slot and the model say the same thing, collapsed —
    rendering "Sonnet/Medium → Claude-Sonnet-5" would say it twice:

        Sonnet/Medium
    """
    slot = (entry.get("slot") or "").strip()
    effort = (entry.get("effort") or "").strip()
    model = (entry.get("model") or "").strip()
    asked = f"{slot.title()}/{effort.title()}"
    if not model or _names_the_slot(slot, model):
        return asked
    return f"{asked} → {_pretty_model(model)}"


def _names_the_slot(slot: str, model: str) -> bool:
    """True when the model is the one the slot already names.

    A provider-prefixed id (`moonshotai/kimi-k3`) is never first-party, and a
    first-party id that names a *different* slot than the entry asks for is
    deliberately not collapsed — that pairing is worth showing, not hiding.
    """
    if not slot or "/" in model:
        return False
    return slot.lower() in model.lower().replace("_", "-").split("-")


def _pretty_model(model: str) -> str:
    """`moonshotai/kimi-k3` -> `Kimi-K3`. Mechanical: no model-name table.

    Acronyms therefore come out title-cased like any other word (`glm-5.3` ->
    `Glm-5.3`). Casing them correctly would mean shipping a list of model
    families, which is exactly the host-specific knowledge this repo keeps out.
    """
    tail = model.rsplit("/", 1)[-1]
    return "-".join(part.title() for part in tail.split("-"))


def resolve_launcher(entry: dict) -> str:
    """Absolute path of the entry's launcher, or raise.

    Deliberately not part of `validate`: a manifest is well-formed independent
    of which host reads it, and a controller renders manifests for hosts whose
    launchers it does not have.
    """
    name = entry.get("launcher") or ""
    found = shutil.which(name)
    if not found:
        raise ManifestError(
            None,
            [f"launcher {name!r} for mapping "
             f"{entry.get('id')!r} is not on $PATH"],
        )
    return found
