"""Read and validate the mapping manifest (ADR-0009).

The manifest is the one place a launch's facts are stated. `ccd` reads it and
validates it here; the broker never sees any of this vocabulary and must not
learn it (ADR-0004, ADR-0009).

Two stored fields carry a launch — `launcher` and `model` — with `effort` and
`billing` stored only where they apply. The id and the label are *derived*,
never stored: a hand-written name can contradict the fields beside it, which
is the defect of issue #10 one level down. `billing` is the deliberate
exception to "derive, don't store": unlike a model slot, it names a fact
about the *route* (a subscription seat vs. metered per-token access) that
this repo cannot derive from the model at all — only the deployment layer
that rendered the launcher knows it (ADR-0009's own reason the manifest is
produced there). Storing a fact nobody here can compute is not the mistake
storing a derivable one is.

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

# Claude Code's `--effort` values, weakest first. Requested, never guaranteed,
# and for some models never sent at all — see ADR-0009. Order is meaningful
# only for display, never for comparison.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# How a launcher is paid for: a subscription seat, or metered per-token API
# access. Lowercase, matching hosting's own ccd-launch.j2 (`billing="sub"` /
# `billing="api"`, session labels like `119-3-sonnet-medium-api`) rather than
# ADR-0002's prose casing (`Sub`/`API`) — session labels are lowercase
# throughout, and this repo defers to the deployment layer's own literal
# tokens rather than inventing a second spelling of the same two words.
BILLING = ("sub", "api")

# A derived id must come out as one shell-safe lowercase word: it becomes
# `$CCD_MAPPING` in the launched session and is read back from rosters and
# transcripts.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

REQUIRED = ("launcher", "model")
# `billing` is stored, not derived, unlike everything else optional here —
# see the validate() note below for why that is consistent rather than a
# reintroduction of the retired `slot`.
OPTIONAL = ("effort", "billing", "notes")

# Fields that used to be stored and are now derived or gone. These are not
# "unknown, possibly from a newer producer" — they are known-dead, and their
# presence means the file predates this contract and was not regenerated.
RETIRED = {
    "slot": "models are named directly; there is no slot",
    "id": "the id is derived from model and effort",
    "display": "the label is derived",
}


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


def _short_model(model: str) -> str:
    """`moonshotai/kimi-k3` -> `kimi-k3`. Shortened, never restyled.

    The id is shown as its provider writes it. Restyling it would need no table
    but would *invent* a name — `glm-5.3-flash` is not `Glm-5.3-Flash` to
    anyone — and a label nobody can grep for is a recurring papercut, since this
    same string appears in the manifest's `model`, in `ccd ls`, and in the
    transcript. Dropping the provider prefix is shortening, not renaming, and
    the full id is one column away.
    """
    return model.rsplit("/", 1)[-1]


def _slug(text: str) -> str:
    """Lowercase `text` into one id-safe word.

    `claude-sonnet-5[1m]` -> `claude-sonnet-5-1m`: the context-window suffix is
    part of what distinguishes two otherwise identical entries, so it survives
    into the id rather than being stripped.
    """
    out = re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower())
    out = re.sub(r"-{2,}", "-", out).strip("-.")
    return out


def entry_id(entry: dict, *, with_launcher: bool = False) -> str:
    """The id for one entry, derived from its model and effort.

    `with_launcher` appends the launcher; `ids()` applies it to *every* member
    of a colliding group, so the result never depends on file order.
    """
    parts = [_slug(_short_model(entry.get("model") or ""))]
    effort = (entry.get("effort") or "").strip()
    if effort:
        parts.append(_slug(effort))
    if with_launcher:
        parts.append(_slug(entry.get("launcher") or ""))
    return "-".join(p for p in parts if p)


def ids(doc: dict) -> list:
    """Derived ids, positionally aligned with `entries(doc)`.

    Two entries reaching the same model at the same effort through different
    launchers collide on the base id; both then carry their launcher, so
    reordering the file cannot rename an entry. Anything still colliding is a
    true duplicate, which `validate` reports.
    """
    items = [e if isinstance(e, dict) else {} for e in entries(doc)]
    base = [entry_id(e) for e in items]
    clashing = {i for i in base if base.count(i) > 1}
    return [entry_id(e, with_launcher=True) if b in clashing else b
            for e, b in zip(items, base)]


def display(entry: dict, *, with_launcher: bool = False) -> str:
    """The label for one entry, derived rather than stored.

    The model as its provider writes it, plus the effort when there is one:

        kimi-k3/max
        deepseek-v4.1-flash
        claude-sonnet-5/medium

    A stored label can contradict the fields it describes, so there is no
    `display` key and no override (ADR-0009). An entry needing a human aside
    carries `notes`, which is visibly not authoritative.

    Prefer `labels(doc)`: a label has to identify an entry among the others,
    which this function alone cannot see. `with_launcher` is what `labels()`
    applies to a colliding group.
    """
    model = _short_model((entry.get("model") or "").strip())
    effort = (entry.get("effort") or "").strip()
    label = f"{model}/{effort}" if model and effort else model or effort
    launcher = (entry.get("launcher") or "").strip()
    if with_launcher and launcher:
        return f"{label} ({launcher})" if label else f"({launcher})"
    return label


def labels(doc: dict) -> list:
    """Derived labels, positionally aligned with `entries(doc)`.

    Mirrors `ids()`, and for the same reason. A label exists so a human can
    choose from it, so two identical rows in a picker mean the choice cannot
    be made from the label at all. Where entries share one, every member of
    that group carries its launcher — all of them, so file order cannot change
    what an entry is called.

    Always appending the launcher was rejected: it makes every label noisier,
    `claude-sonnet-5/medium (claude)`, to fix a case that usually does not
    arise.
    """
    items = [e if isinstance(e, dict) else {} for e in entries(doc)]
    base = [display(e) for e in items]
    clashing = {label for label in base if base.count(label) > 1}
    return [display(e, with_launcher=True) if b in clashing else b
            for e, b in zip(items, base)]


def validate(doc) -> list:
    """Return every problem with `doc`, or an empty list if it is well-formed.

    Unknown keys are *not* problems: the file is machine-rendered, so a
    producing layer that learns a field must not break older readers. Keys this
    contract *retired* are a different matter and are reported — see `RETIRED`.
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

    items = doc.get("mappings")
    if not isinstance(items, list):
        problems.append("'mappings' must be an array")
        return problems
    if not items:
        problems.append("'mappings' is empty; there is nothing to pick")

    for index, entry in enumerate(items):
        label = f"mappings[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{label} must be an object")
            continue
        model = entry.get("model")
        if isinstance(model, str) and model.strip():
            label = f"mappings[{index}] ('{model.strip()}')"

        for key in REQUIRED:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{label}: '{key}' must be a non-empty string")

        # `null` says the same thing as an absent key — it is how a transcript
        # records "none was sent" — so it is accepted. An empty string is
        # neither a value nor an honest absence, so it is not.
        effort = entry.get("effort")
        if effort is not None:
            if not isinstance(effort, str) or not effort.strip():
                problems.append(
                    f"{label}: 'effort' must be a non-empty string when "
                    "present, or omitted entirely (null means omitted)"
                )
            elif effort not in EFFORTS:
                problems.append(
                    f"{label}: 'effort' is {effort!r}, must be one of "
                    f"{'/'.join(EFFORTS)}"
                )

        # Same null-means-absent, empty-string-refused precedent as effort
        # above, for the same reason: `null` is how a transcript-shaped
        # producer says "nothing was sent", so it reads the same as an
        # omitted key, while an empty string is neither a value nor an
        # honest absence.
        billing = entry.get("billing")
        if billing is not None:
            if not isinstance(billing, str) or not billing.strip():
                problems.append(
                    f"{label}: 'billing' must be a non-empty string when "
                    "present, or omitted entirely (null means omitted)"
                )
            elif billing not in BILLING:
                problems.append(
                    f"{label}: 'billing' is {billing!r}, must be one of "
                    f"{'/'.join(BILLING)}"
                )

        notes = entry.get("notes")
        if notes is not None and not isinstance(notes, str):
            problems.append(f"{label}: 'notes' must be a string when present")

        for key, why in RETIRED.items():
            if key in entry:
                problems.append(
                    f"{label}: '{key}' is no longer a field — {why}; "
                    "regenerate the manifest"
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

    # The id is derived, so a clash is a duplicated entry rather than a typo.
    derived = ids(doc)
    for index, ident in enumerate(derived):
        entry = items[index] if isinstance(items[index], dict) else {}
        label = f"mappings[{index}]"
        model = entry.get("model")
        # Check the model's own slug, not the assembled id: a model of "!!!"
        # slugs to nothing and would otherwise leave an id made only of the
        # effort — a wrong name rather than a refused one.
        if isinstance(model, str) and model.strip() and not _slug(
                _short_model(model)):
            problems.append(
                f"{label}: 'model' {model!r} does not yield a usable id"
            )
        elif not ident or not ID_RE.match(ident):
            if isinstance(model, str) and model.strip():
                problems.append(
                    f"{label}: 'model' {model!r} does not yield a usable id "
                    f"(derived {ident!r})"
                )
        elif derived.count(ident) > 1 and index != derived.index(ident):
            problems.append(
                f"{label}: same launcher, model and effort as "
                f"mappings[{derived.index(ident)}] — both derive the id "
                f"'{ident}'"
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
    """The entry whose derived id is `ident`, or None."""
    for entry, derived in zip(entries(doc), ids(doc)):
        if derived == ident:
            return entry
    return None


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
             f"{entry_id(entry)!r} is not on $PATH"],
        )
    return found
