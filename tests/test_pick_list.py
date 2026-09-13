"""`ccd pick --list` — the manifest without a terminal (issue #27).

The dispatcher skill can hand work out but had no way to discover what work
there is to hand out from: `ccd pick` refuses a tool call, which is every call
a language model can make.

Every test here runs the CLI as a subprocess with **stdin explicitly not a
terminal**, because that is the entire property under test. A test that passed
only because pytest happened to run on a tty would be worthless, so the
`piped` fixture pins stdin to DEVNULL and one test asserts that the same
invocation on a real pseudo-terminal behaves the same way.
"""
from __future__ import annotations

import json
import os
import pty
import subprocess

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

EXPECTED = [
    ("claude-sonnet-5-medium", "claude-sonnet-5/medium"),
    ("claude-haiku-4-5", "claude-haiku-4-5"),
    ("kimi-k3-max", "kimi-k3/max"),
]


@pytest.fixture
def piped(tmp_path, repo_root):
    """Run `ccd ...` with stdin guaranteed not to be a terminal."""
    manifest = tmp_path / "mappings.json"
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(manifest)
    env["CCD_SOCKET"] = str(tmp_path / "s.sock")

    def run(*args, mappings=None):
        e = dict(env)
        if mappings is not None:
            e["CCD_MAPPINGS"] = str(mappings)
        return subprocess.run([str(repo_root / "ccd"), *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=e)

    return run


def rows(stdout: str) -> list:
    return [tuple(line.split("\t"))
            for line in stdout.splitlines() if line.strip()]


# ----------------------------------------------------------------------
# the property the change exists for
# ----------------------------------------------------------------------

def test_it_works_with_stdin_not_a_terminal(piped):
    out = piped("pick", "--list")
    assert out.returncode == 0, out.stderr
    assert rows(out.stdout) == EXPECTED


def test_it_prompts_for_nothing(piped):
    out = piped("pick", "--list")
    assert "choice" not in out.stdout + out.stderr
    assert "not a terminal" not in out.stderr


def test_the_listing_goes_to_stdout_not_stderr(piped):
    """A caller redirecting stderr away must still get the data."""
    out = piped("pick", "--list")
    assert out.stderr == ""
    assert out.stdout.strip()


def test_a_real_terminal_gives_the_same_answer(tmp_path, repo_root):
    """--list is non-interactive by nature, not by accident of redirection."""
    manifest = tmp_path / "mappings.json"
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    env = dict(os.environ)
    env["CCD_MAPPINGS"] = str(manifest)

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child execs away
        os.execvpe(str(repo_root / "ccd"),
                   [str(repo_root / "ccd"), "pick", "--list"], env)
    chunks = []
    try:
        while True:
            data = os.read(fd, 1024)
            if not data:
                break
            chunks.append(data)
    except OSError:
        pass
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    seen = b"".join(chunks).decode().replace("\r\n", "\n")
    assert rows(seen) == EXPECTED


# ----------------------------------------------------------------------
# the output a dispatcher has to act on
# ----------------------------------------------------------------------

def test_the_id_comes_first(piped):
    """It is what `ccd launch` takes; the label is not."""
    for ident, _label in rows(piped("pick", "--list").stdout):
        assert ident in [i for i, _ in EXPECTED]


def test_every_listed_id_is_one_launch_accepts(piped, repo_root, tmp_path):
    """The listing would be worse than useless if an id in it were rejected."""
    for ident, _label in rows(piped("pick", "--list").stdout):
        out = piped("launch", ident, "--handle", "probe")
        # No launcher on $PATH in this environment, so it gets as far as
        # resolving the entry and then fails on the launcher — which is proof
        # the id resolved. An unknown id fails earlier and differently.
        assert "no mapping" not in out.stderr, (ident, out.stderr)


def test_there_are_no_ordinals_to_mistake_for_ids(piped):
    """An ordinal would invite a model to send "3" to `ccd launch`."""
    for line in piped("pick", "--list").stdout.splitlines():
        assert not line.strip()[:2].rstrip(".").isdigit() or "\t" in line
        assert not line.startswith(("1.", "2.", "3."))


def test_there_is_no_header_row(piped):
    """Same rule as `ccd ls`: every non-blank line is one record."""
    first = piped("pick", "--list").stdout.splitlines()[0]
    assert first.split("\t")[0] == EXPECTED[0][0]


def test_an_entry_with_no_effort_lists_normally(piped):
    ids = [i for i, _ in rows(piped("pick", "--list").stdout)]
    assert "claude-haiku-4-5" in ids


# ----------------------------------------------------------------------
# bare `ccd pick` still refuses, and now refuses cleanly
# ----------------------------------------------------------------------

def test_bare_pick_still_refuses_a_pipe(piped):
    out = piped("pick")
    assert out.returncode == 2
    assert "stdin is not a terminal" in out.stderr


def test_the_refusal_prints_no_listing(piped):
    """It used to print every option and then refuse — emitting exactly the
    output it declined to be used for."""
    out = piped("pick")
    assert out.stdout == ""
    for _ident, label in EXPECTED:
        assert label not in out.stderr, label


def test_the_refusal_points_at_the_flag(piped):
    assert "ccd pick --list" in piped("pick").stderr


def test_launch_without_an_id_refuses_the_same_way(piped):
    """Same condition, so it must not have grown a second wording."""
    out = piped("launch")
    assert "stdin is not a terminal" in out.stderr
    assert "ccd pick --list" in out.stderr


# ----------------------------------------------------------------------
# argument and manifest errors
# ----------------------------------------------------------------------

def test_an_unknown_flag_is_a_usage_error(piped):
    out = piped("pick", "--wat")
    assert out.returncode == 2
    assert "usage: ccd pick [--list]" in out.stderr


def test_a_missing_manifest_fails_loudly(piped, tmp_path):
    out = piped("pick", "--list", mappings=tmp_path / "absent.json")
    assert out.returncode == 1
    assert "no mapping manifest at" in out.stderr
    assert out.stdout == ""


def test_an_empty_manifest_fails_rather_than_printing_nothing(piped, tmp_path):
    """Zero lines and exit 0 would read as "no mappings exist" — true, but
    indistinguishable from a listing that silently lost them."""
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"schema": 1, "mappings": []}), encoding="utf-8")
    out = piped("pick", "--list", mappings=empty)
    assert out.returncode == 1
    assert out.stdout == ""
