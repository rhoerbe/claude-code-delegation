"""One line-JSON request/response over `$CCD_SOCKET`.

Each call opens a short-lived connection, writes one request line and blocks
reading one reply line. For `ccd recv` that read *is* the zero-token park
(ADR-0002): the process sleeps in the kernel until the broker writes, and there
is no polling loop anywhere in this package.
"""

from __future__ import annotations

import pwd
import json
import os
import socket
from pathlib import Path


class Unreachable(Exception):
    """The socket could not be reached, or gave no reply line.

    Distinct from a broker-side `{"ok": false}`, which is a perfectly good
    answer and comes back as a reply dict.
    """


def _user() -> str:
    """The account name for this uid, not `$USER`.

    `$USER` survives a `sudo -u` that does not reset it, and a stale value names
    a socket belonging to a different account. That is not hypothetical across
    this fleet: the same uid number denotes different people on different hosts
    (r2h2 is 1000 on one and 1001 on another, where 1000 is someone else), so a
    name carried in from the invoking environment can address the wrong
    account's broker. The uid is the thing the broker authenticates on
    (SO_PEERCRED, ADR-0006), so it is also the right thing to name the socket
    after. `$USER` is consulted only if the uid has no passwd entry at all.
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover - uid with no passwd entry
        return os.environ.get("USER") or str(os.getuid())


def _runtime_dir() -> str:
    """`$XDG_RUNTIME_DIR`, else `/run/user/<uid>` if it exists, else `/tmp`.

    The uid is resolved rather than the variable trusted, because the broker
    runs as a `systemd --user` service — which always has XDG_RUNTIME_DIR —
    while a client may not: Claude Code's Bash tool strips it. Falling straight
    through to /tmp made the CLI address a socket the broker was not listening
    on, and report the broker as down while it was running. Kept byte-identical
    in meaning to `ccd_broker.transport_uds.default_socket_path`, which the
    broker itself uses; `tests/test_socket_path.py` asserts the two agree,
    since a client and a server that compute different addresses is the whole
    failure this prevents.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return base
    candidate = f"/run/user/{os.getuid()}"
    return candidate if os.path.isdir(candidate) else "/tmp"


def socket_path() -> str:
    return os.environ.get("CCD_SOCKET") or f"{_runtime_dir()}/ccd-{_user()}.sock"


def pidfile_path() -> str:
    return os.environ.get("CCD_PIDFILE") or f"{_runtime_dir()}/ccd-{_user()}.pid"


def log_path() -> str:
    """The broker's log sits next to its pidfile, `.pid` swapped for `.log`."""
    pidfile = pidfile_path()
    stem = pidfile[: -len(".pid")] if pidfile.endswith(".pid") else pidfile
    return stem + ".log"


def rpc(method: str, **args) -> dict:
    """Send one request, return the parsed reply. Raises `Unreachable`.

    Every value goes on the wire as a string, including empty ones: the broker
    distinguishes "sent empty" from "not sent" for `session`, `pid` and `cwd`.
    """
    payload = json.dumps({
        "method": method,
        "args": {k: "" if v is None else str(v) for k, v in args.items()},
    }) + "\n"

    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            sock.connect(path)
        except OSError as exc:
            raise Unreachable(f"connect failed: {exc}") from exc
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()

    line = buf.split(b"\n", 1)[0]
    if not line:
        raise Unreachable("broker closed connection without a reply")
    try:
        return json.loads(line)
    except ValueError as exc:
        raise Unreachable(f"broker sent an unparsable reply: {exc}") from exc


def repo_root() -> Path:
    """The directory holding this package, when it is a source checkout.

    `ccd broker start` spawns a child that has to import `ccd_broker`. Running
    from an installed distribution it already can; running from a checkout it
    cannot, so the root is put on the child's `PYTHONPATH`. `pyproject.toml`
    is the discriminator — it is in the checkout and not in the installed tree.
    """
    return Path(__file__).resolve().parent.parent


def source_checkout_root() -> Path | None:
    root = repo_root()
    return root if (root / "pyproject.toml").exists() else None
