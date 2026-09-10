#!/usr/bin/env python3
"""Delivery-acknowledgement tests for `_ClientContext.deliver()` (issue #3).

`deliver()` decides whether a reserved message was actually taken by the
client. Getting it wrong in either direction is a data bug:

  * false negative -> the message is re-queued after the client already read
    it, and the next `recv` hands the SAME task out a second time (issue #3);
  * false positive -> a client that died before reading loses the task
    silently.

`ccd_smoke.sh` covers only the second direction (SIGKILL *before* reading).
These cover both, with the emphasis on the first, which is the one that
produced a duplicate in real use.

Runs against a private, in-process broker on its own socket. Stdlib only.
Run: tests/test_deliver_ack.py
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccd_broker.broker import Broker  # noqa: E402
from ccd_broker.transport_uds import UnixSocketTransport  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        FAILURES.append(name)


def start_broker() -> tuple[Broker, str]:
    sock = f"/tmp/ccd-test-{os.getpid()}-{time.time_ns()}.sock"
    broker = Broker()
    transport = UnixSocketTransport(broker, sock)
    threading.Thread(target=transport.serve, daemon=True).start()
    for _ in range(500):
        if os.path.exists(sock):
            return broker, sock
        time.sleep(0.01)
    raise RuntimeError("broker did not come up")


def rpc(sock: str, method: str, args: dict) -> dict:
    """A client shaped exactly like the `ccd` CLI: read the reply, close."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock)
    try:
        s.sendall((json.dumps({"method": method, "args": args}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    line = buf.split(b"\n", 1)[0]
    return json.loads(line) if line else {}


def still_queued(sock: str, handle: str, msg: str) -> bool:
    """True if `msg` is still deliverable to `handle` (i.e. it was re-queued)."""
    reply = rpc(sock, "recv", {"handle": handle, "timeout": "0.4"})
    return bool(reply.get("ok")) and reply.get("msg") == msg


# --- the child used by the abrupt-death cases -------------------------------
# Reads (or deliberately does not read) the reply, then dies by SIGKILL
# without closing the socket — what an Esc-interrupt does to `ccd recv`.
CHILD = r"""
import json, os, signal, socket, sys, time
sock, handle, do_read = sys.argv[1], sys.argv[2], sys.argv[3] == "read"
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock)
s.sendall((json.dumps({"method": "recv",
                       "args": {"handle": handle, "timeout": "10"}}) + "\n").encode())
if do_read:
    buf = b""
    while b"\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    sys.stdout.write(buf.split(b"\n", 1)[0].decode() + "\n")
    sys.stdout.flush()
else:
    time.sleep(0.6)          # let the broker send; never read it
os.kill(os.getpid(), signal.SIGKILL)
"""


def run_child(sock: str, handle: str, mode: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, sock, handle, mode],
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout.strip()


def main() -> int:
    broker, sock = start_broker()
    print(f"broker on {sock}")

    # 1. Message already queued, client reads then closes cleanly.
    print("\nqueued message, clean close after read:")
    rpc(sock, "send", {"to": "w1", "msg": "t1", "from": "disp"})
    got = rpc(sock, "recv", {"handle": "w1", "timeout": "5"})
    check("delivered once", got.get("msg") == "t1", repr(got))
    check("not re-queued after a clean close", not still_queued(sock, "w1", "t1"))

    # 2. Client parked in recv *before* the message exists (the real shape:
    #    a worker waiting, then a dispatcher sending).
    print("\nparked recv, message arrives while waiting:")
    out: dict = {}

    def park() -> None:
        out["r"] = rpc(sock, "recv", {"handle": "w2", "timeout": "10"})

    t = threading.Thread(target=park)
    t.start()
    time.sleep(0.3)                      # ensure genuinely parked
    rpc(sock, "send", {"to": "w2", "msg": "t2", "from": "disp"})
    t.join(timeout=15)
    check("delivered to the parked client", out.get("r", {}).get("msg") == "t2")
    check("not re-queued after a parked delivery", not still_queued(sock, "w2", "t2"))

    # 3. Client reads the reply, then dies abruptly without closing.
    #    This is the issue #3 shape: the task WAS consumed, so re-queueing it
    #    would hand the same task to a worker twice.
    print("\nabrupt death (SIGKILL) AFTER reading the reply:")
    rpc(sock, "send", {"to": "w3", "msg": "t3", "from": "disp"})
    child_out = run_child(sock, "w3", "read")
    check("client really received it", "t3" in child_out, repr(child_out))
    check("not re-queued after death-post-read", not still_queued(sock, "w3", "t3"))

    # 4. The opposite direction, which must keep working: a client that dies
    #    WITHOUT reading must have its message re-queued, not dropped.
    print("\nabrupt death (SIGKILL) BEFORE reading the reply:")
    rpc(sock, "send", {"to": "w4", "msg": "t4", "from": "disp"})
    run_child(sock, "w4", "noread")
    check("re-queued after death-pre-read", still_queued(sock, "w4", "t4"))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("PASS — delivery acknowledgement correct in both directions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
