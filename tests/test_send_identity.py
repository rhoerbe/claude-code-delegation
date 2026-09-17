"""`send` carries a sender, and the anonymous stamp is not an address (#36).

A real delegation run (lobotom-y#294, 2026-09-13) lost a worker's whole
fact-finding report to three defects lining up:

* `ccd send` was the only identity-taking subcommand with no `$CCD_HANDLE`
  default, so a dispatcher following its skill sent anonymously;
* the broker stamps an absent sender as the literal `unknown`, which made a
  forgotten flag indistinguishable from ADR-0007's deliberate anonymous sender;
* the worker's skill says to reply to the sender `recv` printed — so the reply
  went to `unknown`, was accepted with `ok` and a message id, and sat on a queue
  no roster entry could ever drain until the dispatcher noticed and drained it
  by hand.

What is asserted here is that each layer now fails in the direction that is
recoverable: the sender travels by default, and an address that cannot be
collected from is refused loudly instead of accepted silently.

The CLI's default is checked against `cli.py`'s parse tree rather than by
running it, for the reason `test_cli_invariants.py` gives: a passing run shows
one path behaved. `tests/characterize_cli.sh` drives the real binary.
"""
from __future__ import annotations

import ast

import pytest

from ccd_broker.broker import ANON_SENDER


# ----------------------------------------------------------------------
# the broker: the stamp is reserved, the escape hatch is not
# ----------------------------------------------------------------------

def test_an_absent_sender_is_still_stamped(broker, call):
    """ADR-0007's hatch is untouched — this is the behaviour being worked
    around, not removed. A shell with no `$CCD_HANDLE` reaches it."""
    call(broker, "announce", handle="w1", effort="medium")
    assert call(broker, "send", to="w1", msg="poke", **{"from": ""}).get("ok")
    got = call(broker, "recv", handle="w1", timeout=1)
    assert got.get("from") == ANON_SENDER, got


def test_sending_to_the_stamp_is_refused(broker, call):
    r = call(broker, "send", to=ANON_SENDER, msg="my report")
    assert not r.get("ok"), repr(r)
    assert "not a handle" in r.get("reason", "")


def test_the_refusal_says_where_to_reply_instead(broker, call):
    """A worker reads this reason and has to be able to act on it. Naming the
    fallback is the difference between a dead end and a recovery."""
    reason = call(broker, "send", to=ANON_SENDER, msg="x").get("reason", "")
    assert "message text" in reason and "human" in reason


def test_the_stamp_cannot_be_announced(broker, call):
    """A handle `send` refuses as a destination must not be holdable: a session
    named `unknown` could be listed, claimed and dispatched to, and never
    replied to."""
    r = call(broker, "announce", handle=ANON_SENDER, effort="medium")
    assert not r.get("ok"), repr(r)
    assert "reserved" in r.get("reason", "")


def test_a_handle_merely_containing_the_stamp_is_fine(broker, call):
    """The reservation is the exact string, not a substring — `unknown-model`
    is a legitimate name and nothing here may swallow it."""
    assert call(broker, "announce", handle="unknown-model", effort="low").get("ok")
    assert call(broker, "send", to="unknown-model", msg="x").get("ok")


def test_a_send_to_a_not_yet_announced_handle_still_works(broker, call):
    """The fix is not a roster check. Workers are routinely sent to before they
    announce, and turning this into "must exist" would break that."""
    assert call(broker, "send", to="not-here-yet", msg="x").get("ok")


# ----------------------------------------------------------------------
# the CLI: send defaults its sender like every other subcommand
# ----------------------------------------------------------------------

def _cmd_send(repo_root) -> ast.FunctionDef:
    tree = ast.parse((repo_root / "ccd_cli" / "cli.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_send":
            return node
    raise AssertionError("cmd_send not found in ccd_cli/cli.py")


def test_cmd_send_seeds_the_sender_from_the_environment(repo_root):
    """`sender = _env_handle()`, not `sender = ""`. The empty-string default is
    the defect: it shipped an absent identity to the one RPC that cannot
    recover it afterwards."""
    fn = _cmd_send(repo_root)
    seeds = [
        node.value for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "sender" for t in node.targets)
    ]
    assert seeds, "cmd_send assigns no sender at all"
    first = seeds[0]
    assert isinstance(first, ast.Call), ast.dump(first)
    assert getattr(first.func, "id", None) == "_env_handle", ast.dump(first)


def test_cmd_send_still_lets_dash_f_win(repo_root):
    """Including `-f ''`: an explicit choice of anonymity must keep reaching the
    hatch, which is the whole distinction the default introduces."""
    src = ast.unparse(_cmd_send(repo_root))
    assert "sender = rest[1]" in src


@pytest.mark.parametrize("command", ["recv", "announce", "ret", "send"])
def test_every_identity_taking_subcommand_defaults_its_handle(repo_root, command):
    """The property `send` was missing, stated once for all four so a fifth
    subcommand cannot be added without it."""
    tree = ast.parse((repo_root / "ccd_cli" / "cli.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == f"cmd_{command}")
    assert "_env_handle()" in ast.unparse(fn), command


# ----------------------------------------------------------------------
# a handle the roster cannot render is refused where it enters (#27)
# ----------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["a\tb", "a\nb", "a\rb", "a\x00b"])
def test_a_handle_the_roster_cannot_represent_is_refused(broker, call, bad):
    """`ccd ls` joins columns with tabs and prints one record per line, so a
    handle carrying either renders as extra columns or as a phantom record --
    and every consumer of that output counts fields or lines."""
    r = call(broker, "announce", handle=bad, effort="medium")
    assert not r.get("ok"), repr(r)
    assert "cannot represent" in r.get("reason", "")


def test_a_handle_containing_a_space_is_still_allowed(broker, call):
    """Deliberately not refused. A space breaks no output format -- it only
    forces callers to quote -- and ADR-0005 keeps hand-announced participants
    first-class rather than holding them to `ccd launch`'s conventions.
    `ccd launch` slugs its own derived handles regardless."""
    assert call(broker, "announce", handle="hand rolled", effort="low").get("ok")
