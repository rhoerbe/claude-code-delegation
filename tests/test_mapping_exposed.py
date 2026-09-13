"""The mapping id is visible on the roster (issue #27).

`ccd-swap`'s job is "same work, different backend", which means reading the
mapping a session was launched from and relaunching with `ccd launch <new-id>`.
Nothing printed it: the roster carried it, `ccd ls` and the dashboard did not.

The path that matters is not the launch — it is what the roster still holds
turns later, after the worker skill's bare `ccd announce` and after a broker
restart. Those are the two ways the id used to disappear, one silently and one
permanently, so they are what these tests are mostly about.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

MANIFEST = {
    "schema": 1,
    "mappings": [
        {"launcher": "stub-claude", "model": "claude-sonnet-5", "effort": "medium"},
        {"launcher": "stub-claude", "model": "moonshotai/kimi-k3",
         "effort": "max", "billing": "api"},
    ],
}

STUB = "#!/usr/bin/env bash\nexit 0\n"

# `ccd ls` is tab-separated with no header, and its consumers count fields.
HANDLE, EFFORT, OWNER, MODEL, PID, STATUS, DRIFT, MAPPING = range(8)


@pytest.fixture
def cli(tmp_path, repo_root):
    """A broker on a temp socket, stub launchers on PATH, and a `ccd` runner."""
    manifest = tmp_path / "mappings.json"
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("stub-claude", "stub-claude-openrouter"):
        stub = bindir / name
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "CCD_SOCKET": str(tmp_path / "s.sock"),
        "CCD_PIDFILE": str(tmp_path / "s.pid"),
        "CCD_MAPPINGS": str(manifest),
        "PATH": f"{bindir}:{env['PATH']}",
    })
    for stale in ("CCD_HANDLE", "CCD_MAPPING", "CLAUDE_PID",
                  "CLAUDE_CODE_SESSION_ID"):
        env.pop(stale, None)

    def run(*args, **overrides):
        e = dict(env)
        for k, v in overrides.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return subprocess.run([str(repo_root / "ccd"), *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=e)

    run("broker", "start")
    for _ in range(50):
        if run("ping").returncode == 0:
            break
        time.sleep(0.1)
    yield run
    run("broker", "stop")


def row(run, handle: str) -> list:
    for line in run("ls").stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0] == handle:
            return parts
    raise AssertionError(f"{handle} not on the roster:\n{run('ls').stdout}")


# ----------------------------------------------------------------------
# the id is there to be read
# ----------------------------------------------------------------------

def test_a_launched_session_shows_its_mapping(cli):
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    assert row(cli, "143-2-kimi-k3-max-api")[MAPPING] == "kimi-k3-max"


def test_the_mapping_is_the_last_column(cli):
    """Appended, so no existing index moves — see the next test."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    parts = row(cli, "143-2-kimi-k3-max-api")
    assert len(parts) == 8
    assert parts[-1] == "kimi-k3-max"


def test_the_columns_before_it_are_unchanged(cli):
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    parts = row(cli, "143-2-kimi-k3-max-api")
    assert parts[HANDLE] == "143-2-kimi-k3-max-api"
    assert parts[EFFORT] == "max"
    assert parts[OWNER] == "-"
    assert parts[MODEL] == "moonshotai/kimi-k3"
    assert parts[STATUS] in ("up", "dead", "-")


def test_the_existing_field_count_consumer_still_works(cli):
    """hosting's ccd_overview.py branches on `len(parts) >= 7` and reads
    handle/effort/owner from 0/1/2, ignoring everything past the owner. Its
    exact logic, run over the new output."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    parts = row(cli, "143-2-kimi-k3-max-api")
    assert len(parts) >= 7
    effort, owner = parts[1], parts[2]
    assert effort == "max"
    assert owner == "-"


def test_a_session_with_no_mapping_shows_a_dash(cli):
    """A hand-announced participant, or one from before the field existed."""
    cli("announce", "a-model", "low", CCD_HANDLE="hand-1")
    assert row(cli, "hand-1")[MAPPING] == "-"


def test_there_is_still_no_header_row(cli):
    """A header parses as a worker literally named HANDLE."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    first = cli("ls").stdout.splitlines()[0]
    assert not first.lower().startswith("handle\t")


# ----------------------------------------------------------------------
# the two ways it used to disappear
# ----------------------------------------------------------------------

def test_it_survives_the_skill_s_bare_reannounce(cli):
    """The exact path ccd-swap depends on: the worker skill announces with no
    arguments every session, and that must not erase the id."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    handle = "143-2-kimi-k3-max-api"
    out = cli("announce", CCD_HANDLE=handle, CCD_MAPPING="kimi-k3-max")
    assert out.returncode == 0, out.stderr
    assert row(cli, handle)[MAPPING] == "kimi-k3-max"


def test_it_comes_back_after_a_broker_restart(cli):
    """It used to be lost for good here.

    A restart empties the roster, so the carry-forward has nothing to carry;
    every session re-announces and the id existed nowhere afterwards, even
    though $CCD_MAPPING was still set in that session. Announce now sends it.
    """
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    handle = "143-2-kimi-k3-max-api"
    cli("broker", "stop")
    cli("broker", "start")
    assert "(no workers announced)" in cli("ls").stdout

    cli("announce", CCD_HANDLE=handle, CCD_MAPPING="kimi-k3-max")
    assert row(cli, handle)[MAPPING] == "kimi-k3-max"


def test_a_hand_reannounce_without_the_variable_does_not_erase_it(cli):
    """Someone repairing a handle from a shell has no $CCD_MAPPING. That must
    leave the id alone rather than blanking it — the broker's carry-forward,
    which is still doing its job."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    handle = "143-2-kimi-k3-max-api"
    cli("announce", "moonshotai/kimi-k3", "max", CCD_HANDLE=handle,
        CCD_MAPPING=None)
    assert row(cli, handle)[MAPPING] == "kimi-k3-max"


# ----------------------------------------------------------------------
# the machine-readable surface
# ----------------------------------------------------------------------

def test_the_dashboard_json_carries_it(cli):
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    model = json.loads(cli("dashboard", "--json").stdout)
    entry = next(s for s in model["sessions"]
                 if s["handle"] == "143-2-kimi-k3-max-api")
    assert entry["mapping"] == "kimi-k3-max"


def test_the_dashboard_json_says_null_when_there_is_none(cli):
    cli("announce", "a-model", "low", CCD_HANDLE="hand-1")
    model = json.loads(cli("dashboard", "--json").stdout)
    entry = next(s for s in model["sessions"] if s["handle"] == "hand-1")
    assert entry["mapping"] is None


def test_the_id_is_one_launch_accepts(cli):
    """The whole point is relaunching with it, so a printed id the launcher
    rejects would be worse than printing nothing."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    ident = row(cli, "143-2-kimi-k3-max-api")[MAPPING]
    out = cli("launch", ident, "--handle", "relaunched")
    assert "no mapping" not in out.stderr, out.stderr
