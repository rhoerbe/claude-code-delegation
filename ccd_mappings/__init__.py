"""The mapping manifest: one picked entry supplies a launch's every fact.

`ccd` reads the manifest through this package (ADR-0009). It defines the shape
and validates it; it ships no manifest of its own, and no launcher names —
populating the file is the deployment layer's job, which is what keeps this
repo free of host-specific facts.

The broker never sees any of this. It treats roster text as opaque and must
learn no Claude Code vocabulary (ADR-0004).

Public surface:

    from ccd_mappings import entries, ids, labels, load

    doc = load()                          # raises ManifestError, listing every problem
    for ident, label in zip(ids(doc), labels(doc)):    # file order is picker order
        print(ident, label)               # both derived, never stored
"""

from .manifest import (  # noqa: F401
    EFFORTS,
    ID_RE,
    SCHEMA,
    ManifestError,
    display,
    entries,
    entry_id,
    find,
    ids,
    labels,
    load,
    manifest_path,
    resolve_launcher,
    validate,
)

__all__ = [
    "EFFORTS",
    "ID_RE",
    "SCHEMA",
    "ManifestError",
    "display",
    "entries",
    "entry_id",
    "find",
    "ids",
    "labels",
    "load",
    "manifest_path",
    "resolve_launcher",
    "validate",
]
