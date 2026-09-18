"""`ccd ls --json`: the mapping id, machine-readable (issue #27).

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

# `ccd ls`'s text form is tab-separated with no header, and its consumers count
# fields — which is why the mapping went in a JSON flag instead of an 8th
# column. These name the existing seven, to assert they did not move.
HANDLE, EFFORT, OWNER, MODEL, PID, STATUS, DRIFT = range(7)


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
    """A handle's row from the TEXT output."""
    for line in run("ls").stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0] == handle:
            return parts
    raise AssertionError(f"{handle} not on the roster:\n{run('ls').stdout}")


def entry(run, handle: str) -> dict:
    """A handle's entry from `ccd ls --json`."""
    out = run("ls", "--json")
    assert out.returncode == 0, out.stderr
    for worker in json.loads(out.stdout)["workers"]:
        if worker.get("handle") == handle:
            return worker
    raise AssertionError(f"{handle} not in --json:\n{out.stdout}")


# ----------------------------------------------------------------------
# the id is there to be read
# ----------------------------------------------------------------------

def test_a_launched_session_shows_its_mapping(cli):
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    assert entry(cli, "143-2-kimi-k3-max-api")["mapping"] == "kimi-k3-max"


def test_a_session_with_no_mapping_says_null_rather_than_omitting_the_key(cli):
    """`null` and a missing key are different to a consumer: one means "this
    session has no mapping", the other "this ccd does not know about
    mappings"."""
    cli("announce", "a-model", "low", CCD_HANDLE="hand-1")
    hand = entry(cli, "hand-1")
    assert "mapping" in hand
    assert hand["mapping"] is None


def test_liveness_is_the_boolean_not_the_rendered_text(cli):
    """A consumer wants true/false; `up`/`dead` is the text output's
    presentation of the same fact, and two spellings of one fact is what this
    whole epic keeps removing."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2",
        CLAUDE_PID=str(os.getpid()))
    assert entry(cli, "143-2-kimi-k3-max-api")["alive"] is True


def test_liveness_is_null_when_there_is_no_pid_to_check(cli):
    """Three states, not two: a participant with no pid is not dead, there is
    simply nothing to ask about it. Squashing that to false would report every
    plain-shell participant as gone."""
    cli("announce", "a-model", "low", CCD_HANDLE="hand-1")
    hand = entry(cli, "hand-1")
    assert hand["pid"] is None
    assert hand["alive"] is None
    assert row(cli, "hand-1")[STATUS] == "-"


def test_the_drift_marker_is_included(cli):
    """It is the one thing in `ccd ls` the broker does not supply, so a
    consumer would otherwise have to recompute it from transcripts."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    assert entry(cli, "143-2-kimi-k3-max-api")["drift"] == ""


def test_both_forms_agree_about_drift(cli):
    """They share one function, so a divergence would mean someone forked it."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    handle = "143-2-kimi-k3-max-api"
    assert row(cli, handle)[DRIFT] == entry(cli, handle)["drift"]


def test_an_empty_roster_is_an_empty_list(cli):
    """Not the text sentinel. A consumer should not have to string-match
    "(no workers announced)".

    Compared whole rather than key by key, so a key appearing or disappearing
    fails here and has to be looked at -- which is how `orphans` (#39) was
    caught arriving. An empty roster is now two empty containers: no sessions,
    and no queues waiting for handles that are not sessions.
    """
    out = cli("ls", "--json")
    assert out.returncode == 0
    assert json.loads(out.stdout) == {"workers": [], "orphans": {}}


def test_an_unknown_flag_is_a_usage_error(cli):
    out = cli("ls", "--wat")
    assert out.returncode == 2
    assert "usage: ccd ls [--json]" in out.stderr


# ----------------------------------------------------------------------
# the text output did not move — the whole reason for a flag
# ----------------------------------------------------------------------

def test_the_text_output_still_has_seven_fields(cli):
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    assert len(row(cli, "143-2-kimi-k3-max-api")) == 7


def test_the_text_columns_are_where_they_were(cli):
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    parts = row(cli, "143-2-kimi-k3-max-api")
    assert parts[HANDLE] == "143-2-kimi-k3-max-api"
    assert parts[EFFORT] == "max"
    assert parts[OWNER] == "-"
    assert parts[MODEL] == "moonshotai/kimi-k3"
    assert parts[STATUS] in ("up", "dead", "-")


def test_the_text_output_never_mentions_the_mapping(cli):
    """It is deliberately not an 8th column: consumers count fields, and two
    ccd-swap bugs today were positional indices going stale."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    assert "kimi-k3-max" not in row(cli, "143-2-kimi-k3-max-api")[1:]


def test_the_existing_field_count_consumer_still_works(cli):
    """hosting's ccd_overview.py branches on `len(parts) >= 7` and reads
    handle/effort/owner from 0/1/2. Its exact logic."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    parts = row(cli, "143-2-kimi-k3-max-api")
    assert len(parts) >= 7
    assert (parts[1], parts[2]) == ("max", "-")


def test_there_is_still_no_header_row(cli):
    """A header parses as a worker literally named HANDLE."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    assert not cli("ls").stdout.splitlines()[0].lower().startswith("handle\t")


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
    assert entry(cli, handle)["mapping"] == "kimi-k3-max"


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
    assert entry(cli, handle)["mapping"] == "kimi-k3-max"


def test_a_hand_reannounce_without_the_variable_does_not_erase_it(cli):
    """Someone repairing a handle from a shell has no $CCD_MAPPING. That must
    leave the id alone rather than blanking it — the broker's carry-forward,
    which is still doing its job."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    handle = "143-2-kimi-k3-max-api"
    cli("announce", "moonshotai/kimi-k3", "max", CCD_HANDLE=handle,
        CCD_MAPPING=None)
    assert entry(cli, handle)["mapping"] == "kimi-k3-max"


def test_the_id_is_one_launch_accepts(cli):
    """The whole point is relaunching with it, so a printed id the launcher
    rejects would be worse than printing nothing."""
    cli("launch", "kimi-k3-max", "--issue", "143", "--phase", "2")
    ident = entry(cli, "143-2-kimi-k3-max-api")["mapping"]
    out = cli("launch", ident, "--handle", "relaunched")
    assert "no mapping" not in out.stderr, out.stderr
