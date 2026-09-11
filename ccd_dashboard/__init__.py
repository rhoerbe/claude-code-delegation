"""Fleet dashboard for ccd (ADR-0008).

Read-only, and split the way ADR-0008 splits it: metadata for every announced
handle, content for one scope at a time. Nothing here talks to the broker —
`ccd dashboard` hands it a `roster` reply on stdin and this package does the
rest from the participants' own transcripts.
"""

from .dashboard import (  # noqa: F401
    SCHEMA,
    WriteRefused,
    build,
    load_rates,
    render_markdown,
    roster_from_reply,
    write_json,
)
