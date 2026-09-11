"""`ccd` — the command-line front end to ccd-broker.

One implementation, two ways in: the `ccd` console script an install puts on
`$PATH`, and the `./ccd` stub at the repo root that a checkout runs directly.
Both call `ccd_cli.cli.main`.

The surface is frozen — flags, positional-overrides-environment defaults, exit
codes and output text — and `tests/characterize_cli.sh` is the instrument that
says so, comparing every subcommand against `tests/golden/`.
"""

from .cli import main

__all__ = ["main"]
