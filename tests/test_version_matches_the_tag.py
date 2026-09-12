"""`VERSION` must match the most recent release tag reachable from HEAD.

This exists because it did not, and the drift was invisible from the host. The
constant stayed at 1.4.0 while v1.5.0 and v1.5.1 were tagged, released and
deployed, so `ccd ping` and `uv tool list` both reported 1.4.0 on two machines
running different code. There was no way to tell from a host which of the three
releases it had.

That is the exact failure packaging was supposed to end (#6: "a vendored copy has
no version and no link back to the commit it came from... 'which build is that
host actually running?' is a question worth being able to answer"). Reading the
version from one constant means `ccd ping` and the distribution metadata cannot
disagree with EACH OTHER -- it says nothing about either agreeing with the tag the
deployment pins. This test closes that gap, which is why it compares against git
rather than against another copy of the same constant.

Skipped where it cannot be answered: outside a git checkout, or with no release
tags fetched. A skip is honest; passing on no evidence is what the old arrangement
did.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

REPO = Path(__file__).resolve().parent.parent


def _version_in_this_tree() -> str:
    """Read VERSION out of THIS tree's file rather than importing it.

    An import resolves through sys.path, which another test module may already
    have pointed at a different checkout -- and it did: run inside a git
    worktree, the import returned the primary checkout's 1.4.0 while the git
    query below described the worktree, so the test compared two different
    trees and failed on a tree that was correct. Reading the file under REPO
    makes the version and the tag come from the same place, which is the whole
    property under test.
    """
    for line in (REPO / "ccd_broker" / "broker.py").read_text().splitlines():
        if line.startswith("VERSION"):
            return line.split("=", 1)[1].strip().strip('"\'')
    raise AssertionError("no VERSION assignment in ccd_broker/broker.py")


VERSION = _version_in_this_tree()


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(("git", "-C", str(REPO)) + args,
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def test_version_is_a_plain_three_part_number():
    parts = VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), VERSION


def test_version_never_lags_the_latest_reachable_release_tag():
    """Ahead is fine; behind is the bug.

    A commit that has earned a bump but has not been tagged yet is legitimately
    ahead of the newest tag -- VERSION moves first, the tag follows the merge. So
    this forbids only the direction that actually failed: the constant sitting
    BELOW a tag that has already shipped, which is what let v1.5.0 and v1.5.1
    deploy while every host reported 1.4.0.
    """
    if _git("rev-parse", "--git-dir") is None:
        pytest.skip("not a git checkout")
    tag = _git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    if not tag:
        pytest.skip("no release tags reachable (a shallow or tagless clone)")

    def parts(text: str) -> tuple:
        return tuple(int(x) for x in text.lstrip("v").split("."))

    assert parts(VERSION) >= parts(tag), (
        f"VERSION is {VERSION!r}, behind the released tag {tag!r}. A host reports "
        f"this constant, so while it lags there is no way to tell from the host "
        f"which release it is running -- v1.5.0 and v1.5.1 both shipped reporting "
        f"1.4.0. Bump it in the change that earns the tag."
    )
