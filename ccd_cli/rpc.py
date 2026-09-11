"""One line-JSON request/response over `$CCD_SOCKET`.

Each call opens a short-lived connection, writes one request line and blocks
reading one reply line. For `ccd recv` that read *is* the zero-token park
(ADR-0002): the process sleeps in the kernel until the broker writes, and there
is no polling loop anywhere in this package.
"""

from __future__ import annotations

import getpass
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
    user = os.environ.get("USER")
    if user:
        return user
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - only when the account is nameless
        return str(os.getuid())


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


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
