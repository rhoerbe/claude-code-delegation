"""The mapping manifest: one picked entry supplies a launch's every fact.

`ccd` reads the manifest through this package (ADR-0009). It defines the shape
and validates it; it ships no manifest of its own, and no launcher names —
populating the file is the deployment layer's job, which is what keeps this
repo free of host-specific facts.

The broker never sees any of this. It treats roster text as opaque and must
learn no Claude Code vocabulary (ADR-0004).

Public surface:

    from ccd_mappings import load, manifest_path, validate

    doc = load()                     # raises ManifestError listing every problem
    for entry in entries(doc):       # file order is presentation order
        ...
"""

from .manifest import (  # noqa: F401
    EFFORTS,
    ID_RE,
    SCHEMA,
    SLOTS,
    ManifestError,
    display,
    entries,
    find,
    load,
    manifest_path,
    resolve_launcher,
    validate,
)

__all__ = [
    "EFFORTS",
    "ID_RE",
    "SCHEMA",
    "SLOTS",
    "ManifestError",
    "display",
    "entries",
    "find",
    "load",
    "manifest_path",
    "resolve_launcher",
    "validate",
]
