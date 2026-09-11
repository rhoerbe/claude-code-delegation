#!/usr/bin/env python3
"""Static invariants of the `ccd` CLI that no end-to-end run can prove.

`tests/test_dashboard.py` asserted the read-only property (ADR-0008) by
scraping the bash `ccd` for `_ccd_rpc` calls inside `cmd_dashboard`. That text
no longer exists, and `tests/test_*.py` belongs to the pytest-conversion track,
so the property moves here rather than being edited there — and rather than
being dropped, which is what would happen by default.

Run: tests/check_cli_invariants.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

FAILURES: list[str] = []
CLI = Path(__file__).resolve().parent.parent / "ccd_cli" / "cli.py"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        FAILURES.append(name)


def rpc_methods(fn: ast.FunctionDef) -> list:
    """Every literal method name this function passes to `rpc(...)`."""
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
        if name != "rpc" or not node.args:
            continue
        first = node.args[0]
        found.append(first.value if isinstance(first, ast.Constant) else "<computed>")
    return found


def main() -> int:
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}

    print("the dashboard is read-only (ADR-0008):")
    dash = functions.get("cmd_dashboard")
    check("cmd_dashboard exists", dash is not None)
    if dash is not None:
        calls = rpc_methods(dash)
        check("its only broker call is `roster`", calls == ["roster"], repr(calls))
        for method in ("send", "claim", "release", "retire", "announce"):
            check(f"it never calls {method}", method not in calls)

    print("\n`ccd ls` reads the roster and nothing else:")
    listing = functions.get("cmd_ls")
    check("cmd_ls exists", listing is not None)
    if listing is not None:
        calls = rpc_methods(listing)
        check("its only broker call is `roster`", calls == ["roster"], repr(calls))

    print("\nthe announce positional has no wire destination:")
    # claude-code-delegation#13, second pass: the broker's `slot` field was
    # dropped before release, and feeding the slot name into `model` instead
    # would reintroduce exactly the conflation that change ended. The
    # positional survives because the CLI surface is frozen, not because it
    # reaches the broker.
    announce = functions.get("cmd_announce")
    check("cmd_announce exists", announce is not None)
    if announce is not None:
        sends_model = any(
            isinstance(node, ast.keyword) and node.arg == "model"
            for node in ast.walk(announce))
        check("it sends no `model` argument to the broker", not sends_model)
        check("and it sends no `slot` argument either", not any(
            isinstance(node, ast.keyword) and node.arg == "slot"
            for node in ast.walk(announce)))

    print("\nretired vocabulary stays retired:")
    for path in sorted((CLI.parent).glob("*.py")) + [
            Path(__file__).resolve().parent.parent / "ccd"]:
        text = path.read_text(encoding="utf-8").lower()
        for word in ("tier", "vendor"):
            check(f"{path.name} contains no {word!r}", word not in text)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("PASS — the CLI's static invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
