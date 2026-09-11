"""`ccd pick` and `ccd launch` (hosting#131 phase 4, ADR-0009).

`pick` and `launch` both run as subprocesses against the real `ccd` entry
point: `launch` execs a launcher via `os.execve`, which replaces the calling
process image — calling `cmd_launch` in-process would replace the test
runner itself, so every launch-shaped test goes through `subprocess.run`.
The interactive choice/field readers (`_prompt_choice`/`_prompt_field`) don't
exec anything, so those get direct unit tests with a faked stdin instead —
still verifies the actual logic, since a real broker or a real terminal is
mocked out precisely at the seam this design put it behind, not further in.

Every test manifest here is the committed `tests/fixtures/sample-mappings.json`
(never `~/.config/ccd/mappings.json`) and every launcher is the stub script at
`tests/fixtures/stub-launcher`, put on a temp $PATH under the names the sample
manifest's entries use. No test announces into a live broker: `launch`'s own
throwaway broker (via `ccd broker start` on a temp socket) is what gets
reserved into, started and stopped per test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_MANIFEST = FIXTURES / "sample-mappings.json"
STUB_LAUNCHER = FIXTURES / "stub-launcher"

# Derived ids for the three sample-mappings.json entries (ADR-0009's own
# derivation — asserted once here so a future manifest edit fails loudly
# rather than silently invalidating every id string below). ID_KIMI's entry
# carries `billing: "api"`; billing plays no part in id derivation
# (ccd_mappings' own contract, hosting#131 phase 4 follow-up), so the id is
# unaffected — only ID_SONNET and ID_HAIKU's entries have no billing at all,
# which is what exercises both branches of the handle's optional suffix.
ID_SONNET = "claude-sonnet-5-medium"
ID_HAIKU = "claude-haiku-4-5"
ID_KIMI = "kimi-k3-1m-max"


@pytest.fixture
def stub_path_dir(tmp_path) -> Path:
    """A temp directory on $PATH holding the stub launcher under both names
    the sample manifest's entries name as `launcher`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("stub-claude", "stub-claude-openrouter"):
        dest = bindir / name
        shutil.copy(STUB_LAUNCHER, dest)
        dest.chmod(0o755)
    return bindir


@pytest.fixture
def cli_env(tmp_path, stub_path_dir, repo_root):
    """Environment for a hermetic `ccd` subprocess: the sample manifest, the
    stub launcher on $PATH, and a throwaway broker socket/pidfile — never the
    user's real ~/.config/ccd/mappings.json or live broker."""
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(SAMPLE_MANIFEST)
    env["CCD_SOCKET"] = str(tmp_path / "ccd.sock")
    env["CCD_PIDFILE"] = str(tmp_path / "ccd.pid")
    env["PATH"] = f"{stub_path_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(repo_root)
    env.pop("CCD_HANDLE", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CLAUDE_PID", None)
    return env


def run_ccd(env, *args, input_text: str | None = None):
    ccd = str(Path(__file__).resolve().parent.parent / "ccd")
    return subprocess.run([sys.executable, ccd, *args], env=env,
                          input=input_text, capture_output=True, text=True,
                          timeout=15)


@pytest.fixture
def with_broker(cli_env):
    """A running throwaway broker for the duration of one test."""
    run_ccd(cli_env, "broker", "start")
    try:
        yield cli_env
    finally:
        run_ccd(cli_env, "broker", "stop")


# ----------------------------------------------------------------------
# ccd pick
# ----------------------------------------------------------------------

def test_pick_refuses_when_stdin_is_not_a_tty(cli_env):
    # subprocess stdin (a pipe, even an empty one) is never a tty.
    out = run_ccd(cli_env, "pick", input_text="")
    assert out.returncode == 2
    assert "stdin is not a terminal" in out.stderr
    assert out.stdout == ""


def test_pick_reports_a_missing_manifest_without_touching_the_real_one(tmp_path, repo_root):
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(tmp_path / "does-not-exist.json")
    env["PYTHONPATH"] = str(repo_root)
    out = run_ccd(env, "pick")
    assert out.returncode == 1
    assert "no mapping manifest at" in out.stderr
    assert not (tmp_path / "does-not-exist.json").exists()


def test_pick_reports_an_invalid_manifest(tmp_path, repo_root):
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": 1, "mappings": [{"launcher": "x"}]}')
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(bad)
    env["PYTHONPATH"] = str(repo_root)
    out = run_ccd(env, "pick")
    assert out.returncode == 1
    assert "'model'" in out.stderr


def test_pick_takes_no_arguments(cli_env):
    out = run_ccd(cli_env, "pick", "extra")
    assert out.returncode == 2
    assert "usage: ccd pick" in out.stderr


# ----------------------------------------------------------------------
# _prompt_choice / _prompt_field — the interactive readers, unit-tested with
# a faked stdin since they never exec anything.
# ----------------------------------------------------------------------

class FakeStdin:
    def __init__(self, lines, isatty=True):
        self._lines = list(lines)
        self._isatty = isatty

    def isatty(self):
        return self._isatty

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


@pytest.fixture
def cli_module(repo_root):
    sys.path.insert(0, str(repo_root))
    import ccd_cli.cli as cli
    return cli


def test_prompt_choice_refuses_when_not_a_tty(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["2\n"], isatty=False))
    assert cli_module._prompt_choice("pick", 3) is None


def test_prompt_choice_accepts_a_valid_number(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["2\n"], isatty=True))
    assert cli_module._prompt_choice("pick", 3) == 2


def test_prompt_choice_rejects_empty_input(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["\n"], isatty=True))
    assert cli_module._prompt_choice("pick", 3) is None


def test_prompt_choice_rejects_eof(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin([], isatty=True))
    assert cli_module._prompt_choice("pick", 3) is None


def test_prompt_choice_rejects_out_of_range(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["9\n"], isatty=True))
    assert cli_module._prompt_choice("pick", 3) is None


def test_prompt_choice_rejects_non_numeric(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["nope\n"], isatty=True))
    assert cli_module._prompt_choice("pick", 3) is None


def test_prompt_field_refuses_when_not_a_tty(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["42\n"], isatty=False))
    assert cli_module._prompt_field("launch", "issue", "--issue") is None


def test_prompt_field_accepts_free_text(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["42\n"], isatty=True))
    assert cli_module._prompt_field("launch", "issue", "--issue") == "42"


def test_prompt_field_rejects_empty_input(cli_module, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(["   \n"], isatty=True))
    assert cli_module._prompt_field("launch", "issue", "--issue") is None


# ----------------------------------------------------------------------
# _broker_version_note — pure, unit-tested directly
# ----------------------------------------------------------------------

def test_broker_version_note_none_when_versions_match(cli_module):
    assert cli_module._broker_version_note("1.3.0", "1.3.0") is None


def test_broker_version_note_none_when_broker_reports_nothing(cli_module):
    assert cli_module._broker_version_note("", "1.3.0") is None


def test_broker_version_note_fires_when_broker_is_older(cli_module):
    note = cli_module._broker_version_note("1.2.0", "1.3.0")
    assert note is not None
    assert "1.2.0" in note and "1.3.0" in note and "continuing anyway" in note


def test_broker_version_note_fires_when_broker_is_newer(cli_module):
    note = cli_module._broker_version_note("1.4.0", "1.3.0")
    assert note is not None
    assert "1.4.0" in note and "1.3.0" in note


# ----------------------------------------------------------------------
# ccd launch — the happy paths, driven end to end via subprocess
# ----------------------------------------------------------------------

def test_launch_with_explicit_id_issue_and_phase_execs_the_right_launcher(with_broker):
    out = run_ccd(with_broker, "launch", ID_SONNET, "--issue", "42", "--phase", "1")
    assert out.returncode == 0, repr(out.stdout + out.stderr)
    data = json.loads(out.stdout)
    assert data["args"] == ["--name", "42-1-" + ID_SONNET,
                            "--model", "claude-sonnet-5", "--effort", "medium"]
    assert data["CCD_MAPPING"] == ID_SONNET
    assert data["CCD_HANDLE"] == "42-1-" + ID_SONNET
    assert data["CCD_MODEL"] is None
    assert data["CCD_EFFORT"] is None


def test_launch_omits_effort_flag_for_an_entry_with_none(with_broker):
    out = run_ccd(with_broker, "launch", ID_HAIKU, "--issue", "1", "--phase", "1")
    assert out.returncode == 0, repr(out.stdout + out.stderr)
    data = json.loads(out.stdout)
    assert data["args"] == ["--name", "1-1-" + ID_HAIKU,
                            "--model", "claude-haiku-4-5"]


def test_launch_reserves_the_handle_on_the_broker(with_broker):
    out = run_ccd(with_broker, "launch", ID_SONNET, "--issue", "7", "--phase", "2")
    assert out.returncode == 0, repr(out.stderr)
    ls = run_ccd(with_broker, "ls")
    assert f"7-2-{ID_SONNET}" in ls.stdout


# ----------------------------------------------------------------------
# ccd launch — the billing suffix (hosting ADR-0002, revised e782da4):
# <issue>-<phase>-<mapping-id>[-<billing>], appended only when the picked
# entry has one.
# ----------------------------------------------------------------------

def test_launch_appends_the_billing_suffix_when_the_entry_has_one(with_broker):
    out = run_ccd(with_broker, "launch", ID_KIMI, "--issue", "119", "--phase", "3")
    assert out.returncode == 0, repr(out.stderr)
    data = json.loads(out.stdout)
    assert data["CCD_HANDLE"] == f"119-3-{ID_KIMI}-api"
    assert data["args"][:2] == ["--name", f"119-3-{ID_KIMI}-api"]


def test_launch_produces_no_suffix_when_the_entry_has_no_billing(with_broker):
    out = run_ccd(with_broker, "launch", ID_SONNET, "--issue", "119", "--phase", "3")
    assert out.returncode == 0, repr(out.stderr)
    data = json.loads(out.stdout)
    # Exactly the three-part shape phase 4 shipped — not a trailing "-",
    # not a stray empty segment.
    assert data["CCD_HANDLE"] == f"119-3-{ID_SONNET}"


def test_launch_explicit_handle_never_gets_a_billing_suffix(with_broker):
    # --handle is a full override: the human stated the literal string they
    # want, so it is used verbatim even for an entry that has billing —
    # the suffix belongs to the *derived* shape only.
    out = run_ccd(with_broker, "launch", ID_KIMI, "--handle", "exact-name")
    assert out.returncode == 0, repr(out.stderr)
    data = json.loads(out.stdout)
    assert data["CCD_HANDLE"] == "exact-name"


def test_launch_still_sets_only_ccd_mapping_not_a_second_billing_variable(with_broker):
    # CCD_MAPPING stays the one environment variable a launched session
    # carries (ADR-0009) — billing changes the handle string, never a
    # second variable alongside it.
    out = run_ccd(with_broker, "launch", ID_KIMI, "--issue", "1", "--phase", "1")
    assert out.returncode == 0, repr(out.stderr)
    data = json.loads(out.stdout)
    assert data["CCD_MAPPING"] == ID_KIMI
    assert "CCD_BILLING" not in data
    assert all(a not in ("--billing", "CCD_BILLING") for a in data["args"])


def test_launch_refuses_a_manifest_where_two_entries_differ_only_in_billing(
    tmp_path, repo_root, stub_path_dir,
):
    # ccd_mappings' own contract (hosting#131 phase 4 follow-up): billing
    # plays no part in id derivation, so two entries agreeing on launcher,
    # model and effort but differing only in billing still derive the same
    # id and validate() refuses the manifest as a genuine duplicate before
    # `ccd launch` ever gets to pick between them — confirmed here at the
    # integration level rather than assumed from the schema tests alone.
    manifest = tmp_path / "dupe.json"
    manifest.write_text(json.dumps({
        "schema": 1,
        "mappings": [
            {"launcher": "stub-claude", "model": "claude-sonnet-5",
             "effort": "medium", "billing": "sub"},
            {"launcher": "stub-claude", "model": "claude-sonnet-5",
             "effort": "medium", "billing": "api"},
        ],
    }))
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(manifest)
    env["CCD_SOCKET"] = str(tmp_path / "ccd.sock")
    env["PATH"] = f"{stub_path_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(repo_root)
    out = run_ccd(env, "launch", "claude-sonnet-5-medium",
                  "--issue", "1", "--phase", "1")
    assert out.returncode == 1
    assert "both derive the id" in out.stderr


def test_launch_with_explicit_handle_skips_issue_and_phase(with_broker):
    out = run_ccd(with_broker, "launch", ID_KIMI, "--handle", "myhandle")
    assert out.returncode == 0, repr(out.stderr)
    data = json.loads(out.stdout)
    assert data["CCD_HANDLE"] == "myhandle"
    assert data["args"][:2] == ["--name", "myhandle"]


def test_launch_passes_through_extra_launcher_args(with_broker):
    out = run_ccd(with_broker, "launch", ID_HAIKU, "--handle", "h1",
                  "--", "-p", "reply with ok")
    assert out.returncode == 0, repr(out.stderr)
    data = json.loads(out.stdout)
    assert data["args"][-2:] == ["-p", "reply with ok"]


def test_launch_unknown_mapping_id_is_a_usage_error(with_broker):
    out = run_ccd(with_broker, "launch", "not-a-real-id", "--issue", "1", "--phase", "1")
    assert out.returncode == 2
    assert "not-a-real-id" in out.stderr
    assert run_ccd(with_broker, "ls").stdout.strip() == "(no workers announced)"


def test_launch_missing_issue_and_phase_refuses_without_a_tty(with_broker):
    out = run_ccd(with_broker, "launch", ID_SONNET, input_text="")
    assert out.returncode == 2
    assert "stdin is not a terminal" in out.stderr


# ----------------------------------------------------------------------
# ccd launch — collision and unreachable-broker behaviour
# ----------------------------------------------------------------------

def test_launch_of_the_same_explicit_handle_twice_gets_an_ordinal_suffix(with_broker):
    first = run_ccd(with_broker, "launch", ID_SONNET, "--handle", "dup")
    assert first.returncode == 0, repr(first.stderr)

    second = run_ccd(with_broker, "launch", ID_HAIKU, "--handle", "dup")
    assert second.returncode == 0, repr(second.stderr)
    data = json.loads(second.stdout)
    assert data["CCD_HANDLE"] == "dup-2"

    ls = run_ccd(with_broker, "ls").stdout
    assert "dup\t" in ls
    assert "dup-2\t" in ls


def test_launch_tolerates_an_unreachable_broker_and_still_execs(cli_env):
    # No broker started: cli_env's CCD_SOCKET points at a socket nothing is
    # listening on.
    out = run_ccd(cli_env, "launch", ID_SONNET, "--issue", "1", "--phase", "1")
    assert out.returncode == 0, repr(out.stdout + out.stderr)
    assert "broker unreachable" in out.stderr
    assert "without reserving it" in out.stderr
    data = json.loads(out.stdout)
    assert data["CCD_HANDLE"] == "1-1-" + ID_SONNET
    assert data["CCD_MAPPING"] == ID_SONNET


def test_launch_reports_a_missing_manifest_without_creating_one(tmp_path, repo_root):
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(tmp_path / "nope.json")
    env["CCD_SOCKET"] = str(tmp_path / "ccd.sock")
    env["PYTHONPATH"] = str(repo_root)
    out = run_ccd(env, "launch", "anything", "--issue", "1", "--phase", "1")
    assert out.returncode == 1
    assert "no mapping manifest at" in out.stderr
    assert not (tmp_path / "nope.json").exists()


def test_launch_reports_a_launcher_missing_from_path(tmp_path, repo_root):
    # A manifest naming a launcher this $PATH does not have.
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({
        "schema": 1,
        "mappings": [{"launcher": "definitely-not-installed-anywhere",
                      "model": "claude-sonnet-5", "effort": "medium"}],
    }))
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(manifest)
    env["CCD_SOCKET"] = str(tmp_path / "ccd.sock")
    env["PYTHONPATH"] = str(repo_root)
    out = run_ccd(env, "launch", "claude-sonnet-5-medium", "--issue", "1", "--phase", "1")
    assert out.returncode == 1
    assert "not on $PATH" in out.stderr
