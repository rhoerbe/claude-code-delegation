"""`ccd announce` resolving model and effort from $CCD_MAPPING (issue #13).

Driven as a subprocess against a throwaway broker on a temp socket, the same
way test_dashboard.py drives `ccd dashboard`: the point is what the roster ends
up holding, which no in-process check of the parse tree can show.

The bug behind these: `ccd launch` removes CCD_MODEL/CCD_EFFORT from the
environment it hands on, while the worker skill still announced with them — so
a launched session announced two empty strings and landed on the roster with no
effort, silently.
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
        {"launcher": "claude", "model": "claude-sonnet-5", "effort": "medium"},
        {"launcher": "claude", "model": "claude-haiku-4-5"},
        {"launcher": "claude-openrouter", "model": "moonshotai/kimi-k3",
         "effort": "max", "billing": "api"},
    ],
}


@pytest.fixture
def cli(tmp_path, repo_root):
    """A running broker on a temp socket, plus a `ccd ...` runner."""
    manifest = tmp_path / "mappings.json"
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "CCD_SOCKET": str(tmp_path / "s.sock"),
        "CCD_PIDFILE": str(tmp_path / "s.pid"),
        "CCD_MAPPINGS": str(manifest),
    })
    for stale in ("CCD_HANDLE", "CCD_MAPPING", "CLAUDE_PID",
                  "CLAUDE_CODE_SESSION_ID"):
        env.pop(stale, None)

    def run(*args, **overrides):
        e = dict(env)
        e.update({k: v for k, v in overrides.items() if v is not None})
        for k, v in overrides.items():
            if v is None:
                e.pop(k, None)
        return subprocess.run([str(repo_root / "ccd"), *args],
                              capture_output=True, text=True, env=e)

    run("broker", "start")
    for _ in range(50):
        if run("ping").returncode == 0:
            break
        time.sleep(0.1)
    yield run
    run("broker", "stop")


def roster(run) -> dict:
    """handle -> its `ccd ls` columns."""
    out = run("ls").stdout
    rows = {}
    for line in out.splitlines():
        if not line.strip() or line.startswith("("):
            continue
        cols = line.split("\t")
        rows[cols[0]] = cols
    return rows


# ----------------------------------------------------------------------
# the bug itself
# ----------------------------------------------------------------------

def test_a_mapping_puts_the_effort_on_the_roster(cli):
    out = cli("announce", CCD_HANDLE="w1", CCD_MAPPING="kimi-k3-max")
    assert out.returncode == 0, out.stderr
    assert roster(cli)["w1"][1] == "max"


def test_a_mapping_puts_the_resolved_model_on_the_roster(cli):
    cli("announce", CCD_HANDLE="w1", CCD_MAPPING="kimi-k3-max")
    assert roster(cli)["w1"][3] == "moonshotai/kimi-k3"


def test_a_typed_positional_never_reaches_the_roster_as_a_model(cli):
    """It is free text a human typed — historically a slot name like "sonnet",
    which is not a resolved model id. Only a mapping-resolved value is."""
    cli("announce", "typed-model", "low", CCD_HANDLE="w2")
    assert roster(cli)["w2"][3] == "-"
    assert roster(cli)["w2"][1] == "low"


# ----------------------------------------------------------------------
# precedence
# ----------------------------------------------------------------------

def test_explicit_positionals_win_over_the_mapping(cli):
    """An operator typing values means them."""
    out = cli("announce", "typed-model", "low",
              CCD_HANDLE="w3", CCD_MAPPING="kimi-k3-max")
    assert "(typed-model/low)" in out.stdout
    assert roster(cli)["w3"][1] == "low"


def test_a_handle_positional_still_takes_the_rest_from_the_mapping(cli):
    out = cli("announce", "w4", CCD_MAPPING="claude-sonnet-5-medium")
    assert out.returncode == 0, out.stderr
    assert roster(cli)["w4"][1] == "medium"


def test_the_three_positional_form_is_untouched(cli):
    out = cli("announce", "w5", "a-model", "high")
    assert out.returncode == 0, out.stderr
    assert roster(cli)["w5"][1] == "high"


# ----------------------------------------------------------------------
# an entry with no effort
# ----------------------------------------------------------------------

def test_an_entry_with_no_effort_announces_without_one(cli):
    """claude-haiku-4-5 is the real case: the client never sends effort for it.

    On the wire a missing key and an empty string are already the same value
    for `effort`, so there is nothing to distinguish and nothing lost.
    """
    out = cli("announce", CCD_HANDLE="w6", CCD_MAPPING="claude-haiku-4-5")
    assert out.returncode == 0, out.stderr
    assert roster(cli)["w6"][1] == "-"


def test_the_confirmation_drops_the_empty_effort(cli):
    out = cli("announce", CCD_HANDLE="w6", CCD_MAPPING="claude-haiku-4-5")
    assert "(claude-haiku-4-5)" in out.stdout
    assert "claude-haiku-4-5/" not in out.stdout


# ----------------------------------------------------------------------
# failing loudly beats announcing nothing
# ----------------------------------------------------------------------

def test_an_unknown_mapping_id_fails_loudly(cli):
    out = cli("announce", CCD_HANDLE="e1", CCD_MAPPING="no-such-id")
    assert out.returncode == 1
    assert "no mapping 'no-such-id'" in out.stderr
    assert "e1" not in roster(cli)


def test_a_missing_manifest_fails_loudly(cli, tmp_path):
    out = cli("announce", CCD_HANDLE="e2", CCD_MAPPING="kimi-k3-max",
              CCD_MAPPINGS=str(tmp_path / "absent.json"))
    assert out.returncode == 1
    assert "no mapping manifest at" in out.stderr
    assert "e2" not in roster(cli)


def test_a_malformed_manifest_fails_loudly(cli, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    out = cli("announce", CCD_HANDLE="e3", CCD_MAPPING="kimi-k3-max",
              CCD_MAPPINGS=str(bad))
    assert out.returncode == 1
    assert "not valid JSON" in out.stderr
    assert "e3" not in roster(cli)


# ----------------------------------------------------------------------
# the hand-started path (ADR-0005) is untouched
# ----------------------------------------------------------------------

def test_no_mapping_and_no_positionals_is_still_a_usage_error(cli):
    out = cli("announce", CCD_HANDLE="e4")
    assert out.returncode == 2
    assert "usage: ccd announce" in out.stderr
    assert "e4" not in roster(cli)


def test_a_plain_shell_with_positionals_still_announces(cli):
    out = cli("announce", "a-model", "low", CCD_HANDLE="hand-1")
    assert out.returncode == 0, out.stderr
    assert roster(cli)["hand-1"][1] == "low"
