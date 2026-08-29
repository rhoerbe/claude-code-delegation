"""Entry point: `ccd-broker` (a.k.a. `python -m ccd_broker`).

Runs one broker on the Unix-socket transport, forever. No flags and no backend
selection: v1 hardcodes `UnixSocketTransport` (PLAN §5.1). Configuration is the
`$CCD_SOCKET` environment variable and nothing else.
"""

from __future__ import annotations

import signal
import sys

from .broker import serve
from .transport_uds import UnixSocketTransport, default_socket_path


def _die(signum, _frame):
    # Interrupts accept(); serve()'s finally closes and unlinks the socket.
    sys.exit(0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        if argv[0] in ("-h", "--help"):
            print(f"usage: ccd-broker    (socket: {default_socket_path()})")
            return 0
        print(f"ccd-broker: unexpected argument: {argv[0]}", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, _die)
    try:
        serve(UnixSocketTransport)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
