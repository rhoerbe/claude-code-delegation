"""Shared fixtures for the ccd test suite (issue #13 phase 3).

conftest.py runs before collection, so it is the one place allowed to put the
repo root on sys.path — every test_*.py below imports ccd_broker, ccd_dashboard
and ccd_mappings as ordinary packages, the same way the pre-pytest scripts did
with their own `sys.path.insert(0, ...)` line.

Fixtures here cover only the scaffolding that is genuinely identical across
suites: a fresh in-process broker, the `call()` RPC-dispatch helper, and a
broker served over a real temp-socket transport for the tests that need an
actual client/server round trip. Anything shaped differently per suite
(synthetic transcripts, mapping-manifest fixtures, the SIGKILL child script)
stays local to the file that uses it.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ccd_broker.broker import Broker  # noqa: E402
from ccd_broker.transport_uds import UnixSocketTransport  # noqa: E402


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def broker() -> Broker:
    """A fresh in-process broker with no transport, for tests that call
    dispatch() directly and never need a socket (affiliation, dashboard's
    broker section, liveness)."""
    return Broker()


@pytest.fixture
def call():
    """RPC helper bound to whichever broker a test passes it:
    call(broker, "announce", handle="w1", effort="medium")."""

    def _call(b: Broker, method: str, **args) -> dict:
        return b.dispatch({"method": method, "args": args}) or {}

    return _call


def _rpc(sock: str, method: str, args: dict) -> dict:
    """A client shaped exactly like the `ccd` CLI: connect, send one request,
    read the reply, close."""
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


@pytest.fixture
def rpc():
    """A client shaped exactly like the `ccd` CLI, for tests driving a
    live_broker: rpc(sock, "send", {...})."""
    return _rpc


@pytest.fixture
def live_broker(tmp_path):
    """A Broker served over a real UDS socket in a background thread, for
    tests that need actual client/server round trips (delivery
    acknowledgement) rather than direct dispatch() calls. Yields
    (broker, socket_path); the temp socket dies with tmp_path."""
    sock = str(tmp_path / f"ccd-test-{os.getpid()}-{time.time_ns()}.sock")
    b = Broker()
    transport = UnixSocketTransport(b, sock)
    thread = threading.Thread(target=transport.serve, daemon=True)
    thread.start()
    for _ in range(500):
        if os.path.exists(sock):
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("broker did not come up")
    yield b, sock
