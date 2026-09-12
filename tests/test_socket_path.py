"""The client and the broker must compute the SAME default socket address.

This exists because they did not, and the symptom was misleading rather than
loud: the broker ran as a `systemd --user` service (which always has
XDG_RUNTIME_DIR) and listened on /run/user/<uid>/ccd-<user>.sock, while a client
whose environment had lost XDG_RUNTIME_DIR — Claude Code's Bash tool strips it —
fell through to /tmp and reported "broker unreachable". A wrong address reads as
an absent broker.

Two implementations of one rule is the underlying defect; these tests pin them
together so the next edit to either has to keep them agreeing.
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ccd_broker.transport_uds import default_socket_path
from ccd_cli.rpc import pidfile_path, socket_path


ACCOUNT = pwd.getpwuid(os.getuid()).pw_name


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("CCD_SOCKET", "CCD_PIDFILE", "XDG_RUNTIME_DIR", "USER"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_client_and_broker_agree_with_xdg_set(clean_env, tmp_path):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socket_path() == default_socket_path()
    assert socket_path() == str(tmp_path / f"ccd-{ACCOUNT}.sock")


def test_client_and_broker_agree_with_xdg_missing(clean_env):
    """The case that broke: no XDG_RUNTIME_DIR, a real /run/user/<uid>."""
    assert socket_path() == default_socket_path()
    if os.path.isdir(f"/run/user/{os.getuid()}"):
        assert socket_path() == f"/run/user/{os.getuid()}/ccd-{ACCOUNT}.sock"
        assert "/tmp/" not in socket_path()


def test_falls_back_to_tmp_only_when_there_is_no_runtime_dir(clean_env, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    assert socket_path() == f"/tmp/ccd-{ACCOUNT}.sock"
    assert default_socket_path() == f"/tmp/ccd-{ACCOUNT}.sock"


def test_a_stale_USER_cannot_rename_the_socket(clean_env, tmp_path):
    """`sudo -u` that does not reset USER must not address another account."""
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    clean_env.setenv("USER", "someone-else")
    assert socket_path() == str(tmp_path / f"ccd-{ACCOUNT}.sock")
    assert default_socket_path() == socket_path()
    assert "someone-else" not in socket_path()


def test_explicit_env_still_wins(clean_env):
    clean_env.setenv("CCD_SOCKET", "/explicit/path.sock")
    clean_env.setenv("CCD_PIDFILE", "/explicit/path.pid")
    assert socket_path() == "/explicit/path.sock"
    assert default_socket_path() == "/explicit/path.sock"
    assert pidfile_path() == "/explicit/path.pid"


def test_pidfile_sits_beside_the_socket(clean_env, tmp_path):
    clean_env.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert pidfile_path() == socket_path().replace(".sock", ".pid")
