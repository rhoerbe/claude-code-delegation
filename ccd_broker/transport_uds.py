"""`UnixSocketTransport` — the v1 (and only) transport.

AF_UNIX SOCK_STREAM, line-delimited JSON, one logical request per connection,
a thread per connection. The socket lives at `$CCD_SOCKET` (default
`${XDG_RUNTIME_DIR:-/tmp}/ccd-$USER.sock`), is created mode 0600, and every
connection's peer uid is checked with SO_PEERCRED — there is no auth token
(PLAN §5.1: peer-uid + filesystem permissions are the v1 security model).

Client contract (matters for the `ccd` CLI):

  1. connect, write one JSON object followed by "\\n";
  2. keep the connection open and read the reply line;
  3. close (or just exit).

A client that stays alive without reading its reply is the only thing that
looks like a dead client, and only for `ACK_TIMEOUT`. `shutdown(SHUT_WR)`
after the request is tolerated — a half-close is distinguishable from a real
close (see below) — but a client that *exits* on stdin EOF is not: `socat`
needs `-t <big>` or it tears the connection down 0.5s into a parked `recv`.

How the broker tells "the client took the reply" from "the client died"
(Linux AF_UNIX semantics, and why this is exact):

  * `SIOCOUTQ` on our end reports the bytes we sent that the peer has not yet
    consumed; it drops to 0 the moment the peer reads them. That is a genuine
    read-receipt, and it does not require the client to close first.
  * peer fully closed -> `sk_shutdown == SHUTDOWN_MASK` -> POLLHUP
  * peer closed while our reply was still unread in its receive queue
    -> `sk_err = ECONNRESET` -> POLLERR. The kernel sets `sk_err` *before* it
    purges that unread queue, so a POLLERR re-check after seeing an empty
    SIOCOUTQ cannot be fooled by a close that discarded the reply.
  * peer only did `shutdown(SHUT_WR)` -> POLLIN/POLLRDHUP but *no* POLLHUP

So: SIOCOUTQ 0 (and no POLLERR) means "read it" -> consume; POLLERR means
"died with it unread" -> re-queue; POLLHUP without POLLERR means "read it and
exited" -> consume; POLLRDHUP alone means the client is still there reading.
"""

from __future__ import annotations

import array
import errno
import fcntl
import json
import os
import pwd
import select
import socket
import struct
import sys
import termios
import threading
import time
from typing import Optional

from .broker import Broker

#: Longest we wait for a client to take its `recv` reply. A client that is
#: reading acknowledges in microseconds; one that lingers past this without
#: reading gets the message counted as delivered anyway (better than handing
#: the same task to a second worker).
ACK_TIMEOUT = 5.0

#: How long a connection may take to send its request line before we hang up.
REQUEST_TIMEOUT = 30.0

#: Largest request line we will read (a task description, not a payload store).
MAX_REQUEST_BYTES = 4 * 1024 * 1024

_POLLRDHUP = getattr(select, "POLLRDHUP", 0)
_DEAD = select.POLLERR | select.POLLNVAL
_READABLE = select.POLLIN | _POLLRDHUP
_SIOCOUTQ = getattr(termios, "TIOCOUTQ", 0x5411)


def _warn(msg: str) -> None:
    """Diagnostic to the broker's stderr, which the broker log captures.

    Re-queueing a reserved message is the one place this transport can turn a
    correct single delivery into a duplicate one (the client took the reply,
    we concluded it had not), and it is invisible from either side when it
    happens. Issue #3 reported exactly that and could not be diagnosed after
    the fact, because nothing recorded *which* of the failure paths fired.
    So every re-queue says so, with its reason.
    """
    print(
        f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] ccd-broker: {msg}",
        file=sys.stderr,
        flush=True,
    )


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

    def _flags(self, timeout_ms: float) -> int:
        events = self._poll.poll(timeout_ms)
        return events[0][1] if events else 0

    def _unread_bytes(self) -> Optional[int]:
        """Bytes we sent that the peer has not consumed, or None if unavailable."""
        try:
            buf = array.array("i", [0])
            fcntl.ioctl(self._conn.fileno(), _SIOCOUTQ, buf, True)
            return buf[0]
        except OSError:
            return None

    def alive(self) -> bool:
        """False once the peer is gone. Never blocks."""
        while True:
            flags = self._flags(0)
            if flags & (_DEAD | select.POLLHUP):
                return False
            if flags & select.POLLIN:
                try:
                    data = self._conn.recv(4096)
                except OSError as exc:
                    _warn(f"deliver: recv() failed while awaiting ack ({exc}); "
                          "re-queueing")
                    return False
                if data == b"":
                    self._drop_readable()  # half-close only; peer still reads
                    return True
                continue  # unexpected trailing bytes: ignore and re-poll
            if flags & _POLLRDHUP:
                self._drop_readable()
            return True

    def deliver(self, resp: dict) -> bool:
        """Write the reply and wait for the client to actually take it.

        True  -> the client read it (consume the message).
        False -> the client died with the reply unread (re-queue it).
        """
        try:
            self._conn.sendall(_encode(resp))
        except OSError as exc:
            _warn(f"deliver: peer gone before the reply was sent ({exc}); re-queueing")
            return False  # peer was already gone

        deadline = time.monotonic() + ACK_TIMEOUT
        wait_ms = 2.0
        while True:
            flags = self._flags(0)
            if flags & _DEAD:
                _warn("deliver: POLLERR/POLLNVAL before ack — peer died with the "
                      "reply unread; re-queueing")
                return False  # ECONNRESET: died with the reply unread
            unread = self._unread_bytes()
            if unread == 0:
                # Re-check: a close that *discarded* the reply also empties the
                # queue, but sets sk_err first, so a POLLERR here disambiguates.
                if self._flags(0) & _DEAD:
                    _warn("deliver: send queue emptied by a close that discarded "
                          "the reply (POLLERR on re-check); re-queueing")
                    return False
                return True
            if flags & select.POLLIN:
                try:
                    data = self._conn.recv(4096)
                except OSError as exc:
                    _warn(f"deliver: recv() failed while awaiting ack ({exc}); "
                          "re-queueing")
                    return False
                if data == b"":
                    self._drop_readable()
                continue
            if flags & _POLLRDHUP:
                self._drop_readable()
                continue
            if flags & select.POLLHUP:
                return True  # closed cleanly, reply consumed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True  # lingering without reading; count it as delivered
            if unread is None:
                # No SIOCOUTQ (non-Linux): fall back to close-as-acknowledgement
                # and just wait for the peer to hang up.
                self._poll.poll(remaining * 1000.0)
                continue
            self._poll.poll(min(wait_ms, remaining * 1000.0))
            wait_ms = min(wait_ms * 2, 50.0)


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
