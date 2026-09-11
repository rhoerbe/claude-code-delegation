"""Static invariants of the `ccd` CLI that no end-to-end run can prove.

These read `ccd_cli/cli.py`'s parse tree rather than running it: "the dashboard
never mutates the roster" and "the announce positional reaches no wire field"
are properties of the code, and a passing session only shows that one path
through it behaved.

Collected by pytest deliberately. A hand-run script sitting outside
`pytest tests/` is the same hazard the suite conversion removed — a green run
that proves nothing because the file was never executed.

`tests/characterize_cli.sh` stays bash and stays outside: it is the analogue of
`ccd_smoke.sh`, driving the real CLI as a subprocess against a real broker.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

MUTATING = ["send", "claim", "release", "retire", "announce"]
RETIRED_WORDS = ["tier", "vendor"]


def _cli_tree(repo_root: Path) -> ast.Module:
    return ast.parse((repo_root / "ccd_cli" / "cli.py").read_text(encoding="utf-8"))


def _function(repo_root: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_cli_tree(repo_root)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in ccd_cli/cli.py")


def _rpc_methods(function: ast.FunctionDef) -> list:
    """Every literal method name this function passes to `rpc(...)`.

    A call assembled through a variable comes back as "<computed>" rather than
    being missed, so an indirection cannot quietly satisfy these assertions.
    """
    found = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (target.id if isinstance(target, ast.Name)
                else getattr(target, "attr", None))
        if name != "rpc" or not node.args:
            continue
        first = node.args[0]
        found.append(first.value if isinstance(first, ast.Constant) else "<computed>")
    return found


def _wire_fields(function: ast.FunctionDef) -> set:
    """Every field name this function can put into an RPC's args.

    Keyword arguments are not enough on their own: `cmd_announce` builds a dict
    and splats it, so a keyword-only scan comes back empty and every assertion
    against it passes vacuously — which is exactly what the first version of
    this check did. Dict-literal keys and `args["x"] = ...` subscripts count
    too.
    """
    fields = set()
    for node in ast.walk(function):
        if isinstance(node, ast.keyword) and node.arg:
            fields.add(node.arg)
        elif isinstance(node, ast.Dict):
            fields |= {k.value for k in node.keys
                       if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):
                fields.add(node.slice.value)
    return fields


# ----------------------------------------------------------------------
# the dashboard is read-only (ADR-0008)
# ----------------------------------------------------------------------

def test_cmd_dashboard_only_calls_roster(repo_root):
    assert _rpc_methods(_function(repo_root, "cmd_dashboard")) == ["roster"]


@pytest.mark.parametrize("method", MUTATING)
def test_cmd_dashboard_never_calls_a_mutating_method(repo_root, method):
    assert method not in _rpc_methods(_function(repo_root, "cmd_dashboard"))


def test_cmd_ls_only_calls_roster(repo_root):
    assert _rpc_methods(_function(repo_root, "cmd_ls")) == ["roster"]


# ----------------------------------------------------------------------
# the announce positional has no wire destination
# ----------------------------------------------------------------------
# claude-code-delegation#13, second pass: the broker's `slot` field was dropped
# before release, and feeding the slot name into `model` instead would
# reintroduce exactly the conflation that change ended. The positional survives
# because the CLI surface is frozen, not because it reaches the broker.

@pytest.mark.parametrize("field", ["model", "slot"])
def test_cmd_announce_sends_no_slot_shaped_field(repo_root, field):
    assert field not in _wire_fields(_function(repo_root, "cmd_announce"))


def test_cmd_announce_still_sends_what_the_broker_needs(repo_root):
    """The negative above is only worth anything if the positive holds.

    Without this, `_wire_fields` returning nothing at all would satisfy every
    "does not send" assertion in this file.
    """
    sent = _wire_fields(_function(repo_root, "cmd_announce"))
    assert {"handle", "effort", "cwd", "session", "pid"} <= sent


# ----------------------------------------------------------------------
# retired vocabulary stays retired
# ----------------------------------------------------------------------
# This file is necessarily the one place the retired words appear, because it
# is the thing forbidding them — the scan covers ccd_cli/ and ./ccd, never
# itself. A repo-wide grep therefore returns these literals and nothing else;
# that is the expected count, not a leak.

def _cli_sources(repo_root: Path) -> list:
    return sorted((repo_root / "ccd_cli").glob("*.py")) + [repo_root / "ccd"]


@pytest.mark.parametrize("word", RETIRED_WORDS)
def test_the_cli_uses_no_retired_vocabulary(repo_root, word):
    offenders = [p.name for p in _cli_sources(repo_root)
                 if word in p.read_text(encoding="utf-8").lower()]
    assert not offenders, f"{word!r} appears in {offenders}"


# ----------------------------------------------------------------------
# there is one implementation, reached two ways
# ----------------------------------------------------------------------

def test_the_repo_root_ccd_is_a_stub_not_a_second_implementation(repo_root):
    """`ccd_smoke.sh` invokes $REPO_ROOT/ccd, so the file has to exist.

    It must stay a launcher: the hard cut in phase 3 was about never having two
    implementations of this CLI on one machine.
    """
    stub = (repo_root / "ccd").read_text(encoding="utf-8")
    assert stub.startswith("#!/usr/bin/env python3")
    assert "from ccd_cli.cli import main" in stub
    assert len([l for l in stub.splitlines()
                if l.strip() and not l.strip().startswith("#")]) < 20
