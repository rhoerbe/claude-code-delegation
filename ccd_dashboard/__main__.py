"""`python3 -m ccd_dashboard` — render a roster reply as the fleet dashboard.

Reads the JSON reply to a `roster` RPC on stdin (that is what `ccd dashboard`
pipes in) and writes markdown, or the JSON model with `--json`. Keeping the
socket on the bash side means this package needs no transport and can be
driven from a file in tests.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import dashboard


def main(argv: list | None = None) -> int:
    # `None` for the `ccd-dashboard` console script, which passes nothing;
    # an explicit list from `ccd dashboard` and from the suites.
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="ccd-dashboard",
        description="Render the ccd fleet dashboard from a roster reply on stdin.")
    parser.add_argument("--roster", default="-",
                        help="file holding the roster reply JSON (default: stdin)")
    parser.add_argument("--scope", metavar="HANDLE",
                        help="the one handle whose content line is shown")
    parser.add_argument("--json", action="store_true",
                        help="print the JSON model instead of markdown")
    parser.add_argument("--write", metavar="PATH",
                        help="also write the JSON model here (refused inside a "
                             "git working tree)")
    parser.add_argument("--rates", metavar="FILE",
                        help="USD-per-million-token table, {model: {input, output, "
                             "cache_read, cache_creation}}; without it cost is "
                             "reported in tokens only")
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.roster == "-" else open(args.roster).read()
    try:
        roster = dashboard.roster_from_reply(json.loads(raw))
    except ValueError as exc:
        print(f"ccd dashboard: {exc}", file=sys.stderr)
        return 1

    try:
        rates = dashboard.load_rates(args.rates)
    except (OSError, ValueError) as exc:
        print(f"ccd dashboard: --rates: {exc}", file=sys.stderr)
        return 1

    model = dashboard.build(roster, args.scope, rates=rates)

    if args.write:
        try:
            written = dashboard.write_json(model, args.write)
        except dashboard.WriteRefused as exc:
            print(f"ccd dashboard: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"ccd dashboard: --write: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {written}", file=sys.stderr)

    if args.json:
        print(json.dumps(model, indent=2))
    else:
        print(dashboard.render_markdown(model))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
