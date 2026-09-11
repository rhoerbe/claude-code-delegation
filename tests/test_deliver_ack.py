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

Runs against a private, in-process broker on its own socket (the
`live_broker` fixture in conftest.py). Stdlib only, aside from pytest.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time


def still_queued(rpc, sock: str, handle: str, msg: str) -> bool:
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


def test_queued_message_clean_close_after_read(live_broker, rpc):
    _, sock = live_broker
    rpc(sock, "send", {"to": "w1", "msg": "t1", "from": "disp"})
    got = rpc(sock, "recv", {"handle": "w1", "timeout": "5"})
    assert got.get("msg") == "t1", repr(got)
    assert not still_queued(rpc, sock, "w1", "t1")


def test_parked_recv_message_arrives_while_waiting(live_broker, rpc):
    # The real shape: a worker waiting, then a dispatcher sending.
    _, sock = live_broker
    out: dict = {}

    def park() -> None:
        out["r"] = rpc(sock, "recv", {"handle": "w2", "timeout": "10"})

    t = threading.Thread(target=park)
    t.start()
    time.sleep(0.3)                      # ensure genuinely parked
    rpc(sock, "send", {"to": "w2", "msg": "t2", "from": "disp"})
    t.join(timeout=15)
    assert out.get("r", {}).get("msg") == "t2"
    assert not still_queued(rpc, sock, "w2", "t2")


def test_abrupt_death_after_reading_the_reply(live_broker, rpc):
    # The issue #3 shape: the task WAS consumed, so re-queueing it would hand
    # the same task to a worker twice.
    _, sock = live_broker
    rpc(sock, "send", {"to": "w3", "msg": "t3", "from": "disp"})
    child_out = run_child(sock, "w3", "read")
    assert "t3" in child_out, repr(child_out)
    assert not still_queued(rpc, sock, "w3", "t3")


def test_abrupt_death_before_reading_the_reply(live_broker, rpc):
    # The opposite direction, which must keep working: a client that dies
    # WITHOUT reading must have its message re-queued, not dropped.
    _, sock = live_broker
    rpc(sock, "send", {"to": "w4", "msg": "t4", "from": "disp"})
    run_child(sock, "w4", "noread")
    assert still_queued(rpc, sock, "w4", "t4")
