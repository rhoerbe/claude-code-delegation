"""`UnixSocketTransport` — the v1 (and only) transport.

AF_UNIX SOCK_STREAM, line-delimited JSON, one logical request per connection,
a thread per connection. The socket lives at `$CCD_SOCKET` (default
`${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.sock`), is created mode 0600, and every
connection's peer uid is checked with SO_PEERCRED — there is no auth token
(PLAN §5.1: peer-uid + filesystem permissions are the v1 security model).

Client contract (matters for the `ccd` CLI):

  1. connect, write one JSON object followed by "\\n";
  2. keep the connection open and read the reply line;
  3. close.

Step 3 is the acknowledgement. Do **not** `shutdown(SHUT_WR)` before reading —
a half-close is tolerated (it is distinguishable from a real close, see
`_ClientContext`), but a full close before the reply is read is read as "the
client died", which re-queues a reserved message.

How disconnects are detected (Linux AF_UNIX semantics, and why this is exact):

  * peer fully closed  -> our end gets `sk_shutdown == SHUTDOWN_MASK` -> POLLHUP
  * peer closed while our reply was still unread in its receive queue
                       -> our end additionally gets `sk_err = ECONNRESET` -> POLLERR
  * peer only did `shutdown(SHUT_WR)` -> POLLIN/POLLRDHUP but *no* POLLHUP

So POLLERR after a write means "the client died without taking the reply"
(re-queue), POLLHUP without POLLERR means "the client read it and exited"
(commit), and POLLRDHUP alone means the client is still there reading.
"""

from __future__ import annotations

import errno
import json
import os
import pwd
import select
import socket
import struct
import threading
import time
from typing import Optional

from .broker import Broker

#: Longest we wait for a client to read a `recv` reply and close. A well-behaved
#: client closes within microseconds; if one lingers past this we assume the
#: reply landed and consume the message.
ACK_TIMEOUT = 5.0

#: How long a connection may take to send its request line before we hang up.
REQUEST_TIMEOUT = 30.0

#: Largest request line we will read (a task description, not a payload store).
MAX_REQUEST_BYTES = 4 * 1024 * 1024

_POLLRDHUP = getattr(select, "POLLRDHUP", 0)
_DEAD = select.POLLERR | select.POLLNVAL
_READABLE = select.POLLIN | _POLLRDHUP


def default_socket_path() -> str:
    """`$CCD_SOCKET`, else `${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.sock`."""
    explicit = os.environ.get("CCD_SOCKET")
    if explicit:
        return explicit
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    return os.path.join(base, f"ccd-{user}.sock")


class _ClientContext:
    """Liveness + acknowledged delivery for one connection (see module docstring)."""

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._poll = select.poll()
        self._events = _DEAD | select.POLLHUP | _READABLE
        self._poll.register(conn.fileno(), self._events)

    def _drop_readable(self) -> None:
        """Stop watching POLLIN once the peer half-closed, or it spins."""
        self._events &= ~_READABLE
        self._poll.register(self._conn.fileno(), self._events)

    def alive(self) -> bool:
        """False once the peer is gone. Never blocks."""
        while True:
            events = self._poll.poll(0)
            if not events:
                return True
            flags = events[0][1]
            if flags & _DEAD:
                return False
            if flags & select.POLLHUP:
                return False
            if flags & select.POLLIN:
                try:
                    data = self._conn.recv(4096)
                except OSError:
                    return False
                if data == b"":
                    self._drop_readable()  # half-close only; peer still reads
                    return True
                continue  # unexpected trailing bytes: ignore, re-poll
            if flags & _POLLRDHUP:
                self._drop_readable()
                return True
            return True

    def deliver(self, resp: dict) -> bool:
        """Write the reply, then wait for the client to acknowledge by closing.

        True  -> the client took it (consume the message).
        False -> the client died with the reply unread (re-queue it).
        """
        try:
            self._conn.sendall(_encode(resp))
        except OSError:
            return False  # peer was already gone

        deadline = time.monotonic() + ACK_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True  # client is lingering; treat the write as good
            events = self._poll.poll(remaining * 1000.0)
            if not events:
                return True
            flags = events[0][1]
            if flags & _DEAD:
                return False  # ECONNRESET: closed with our reply unread
            if flags & select.POLLIN:
                try:
                    data = self._conn.recv(4096)
                except OSError:
                    return False
                if data == b"":
                    self._drop_readable()
                    if flags & select.POLLHUP:
                        return True
                    continue
                continue
            if flags & select.POLLHUP:
                return True  # clean close after reading the reply
            if flags & _POLLRDHUP:
                self._drop_readable()
                continue
            return True


class UnixSocketTransport:
    """Sole concrete `Transport` (PLAN §5.1)."""

    name = "uds"

    def __init__(self, broker: Broker, path: Optional[str] = None) -> None:
        self._broker = broker
        self.path = path or default_socket_path()
        self._listener: Optional[socket.socket] = None
        self._local = threading.local()
        self._stopping = threading.Event()

    # -- Transport protocol ------------------------------------------------

    def serve(self) -> None:
        self._bind()
        try:
            while not self._stopping.is_set():
                try:
                    conn, _ = self._listener.accept()
                except OSError as exc:
                    if self._stopping.is_set():
                        break
                    if exc.errno == errno.EINTR:
                        continue
                    raise
                threading.Thread(
                    target=self._serve_conn, args=(conn,), daemon=True
                ).start()
        finally:
            self.close()

    def handle(self, req: dict) -> Optional[dict]:
        """Hand one request to the broker on behalf of the current connection."""
        conn = getattr(self._local, "conn", None)
        ctx = _ClientContext(conn) if conn is not None else None
        return self._broker.dispatch(req, ctx)

    # -- lifecycle ---------------------------------------------------------

    def _bind(self) -> None:
        if os.path.exists(self.path):
            if _socket_is_live(self.path):
                raise SystemExit(f"ccd-broker: already running on {self.path}")
            os.unlink(self.path)  # stale socket from a dead broker
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # 0600 from the moment it exists
        try:
            listener.bind(self.path)
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        listener.listen(128)
        self._listener = listener

    def close(self) -> None:
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            finally:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass

    # -- one connection ----------------------------------------------------

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            uid = _peer_uid(conn)
            if uid is not None and uid != os.getuid():
                _write(conn, {"ok": False, "reason": "permission denied"})
                return

            conn.settimeout(REQUEST_TIMEOUT)
            try:
                line = _read_line(conn)
            except (OSError, ValueError) as exc:
                _write(conn, {"ok": False, "reason": f"bad request: {exc}"})
                return
            if line is None:
                return  # client hung up without asking anything
            conn.settimeout(None)

            try:
                req = json.loads(line)
            except ValueError as exc:
                _write(conn, {"ok": False, "reason": f"invalid JSON: {exc}"})
                return

            self._local.conn = conn
            try:
                resp = self.handle(req)
            finally:
                self._local.conn = None
            if resp is not None:
                _write(conn, resp)
        except Exception as exc:  # never let one client kill the broker
            _write(conn, {"ok": False, "reason": f"internal error: {exc}"})
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _peer_uid(conn: socket.socket) -> Optional[int]:
    """Peer uid via SO_PEERCRED, or None where the platform lacks it."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _socket_is_live(path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _read_line(conn: socket.socket) -> Optional[bytes]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return b"".join(chunks) or None
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise ValueError("request too large")


def _encode(resp: dict) -> bytes:
    return json.dumps(resp, separators=(",", ":")).encode("utf-8") + b"\n"


def _write(conn: socket.socket, resp: dict) -> bool:
    try:
        conn.sendall(_encode(resp))
        return True
    except OSError:
        return False
