#!/usr/bin/env python3
"""Fleet dashboard: metadata system-wide, content scoped (ADR-0008, issue #11).

Everything here runs against synthetic transcripts under a temporary root, so
the suite never reads the machine's real sessions and never needs a broker
socket. The broker section drives `Broker` directly, as `test_affiliation.py`
does — the announce fields are broker logic and need no transport.

Run: tests/test_dashboard.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccd_broker.broker import Broker  # noqa: E402
from ccd_dashboard import dashboard, transcript  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        FAILURES.append(name)


def call(b: Broker, method: str, **args) -> dict:
    return b.dispatch({"method": method, "args": args}) or {}


# ----------------------------------------------------------------------
# synthetic transcripts
# ----------------------------------------------------------------------

STAMP = "2026-01-02T03:04:05.000Z"


def user(text: str, **extra) -> dict:
    return dict({"type": "user", "timestamp": STAMP, "gitBranch": "main",
                 "message": {"role": "user", "content": text}}, **extra)


def assistant(*blocks, usage: dict = None, model: str = "claude-sonnet-5",
              **extra) -> dict:
    return dict({"type": "assistant", "timestamp": STAMP, "gitBranch": "main",
                 "message": {"role": "assistant", "model": model,
                             "content": list(blocks),
                             "usage": usage or _usage(10, 5)}}, **extra)


def _usage(inp: int, out: int, cache_read: int = 0, cache_creation: int = 0) -> dict:
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation}


def tool_use(tid: str, command: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": "Bash",
            "input": {"command": command}}


def tool_result(tid: str, text: str, **extra) -> dict:
    return dict({"type": "user", "timestamp": STAMP,
                 "message": {"role": "user",
                             "content": [dict({"type": "tool_result",
                                               "tool_use_id": tid,
                                               "content": text}, **extra)]}})


def write_transcript(root: Path, cwd: str, session: str, records: list) -> Path:
    project = root / transcript.encode_project_dir(cwd)
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


# The three sessions the rendering tests run against. Each carries a content
# line that must never appear except under its own scope.
DISP_GOAL = "Ship the fleet dashboard for issue #11 end to end"
W1_TASK = "disp: Summarise the failure modes in tests/ccd_smoke.sh"
W2_GOAL = "Third client material that must never leak into another view"

DISP_CWD = "/work/alpha"
W1_CWD = "/work/beta"
W2_CWD = "/work/gamma"


def fleet(root: Path) -> list:
    """Three announced handles with transcripts: a dispatcher and two workers."""
    write_transcript(root, DISP_CWD, "s-disp", [
        user("<local-command-caveat>Caveat: ignore this</local-command-caveat>",
             isMeta=True),
        user("<command-name>/clear</command-name>"),
        user(DISP_GOAL + " <system-reminder>not part of the goal</system-reminder>"),
        assistant({"type": "text", "text": "on it"},
                  usage=_usage(100, 200, cache_read=1000)),
    ])
    write_transcript(root, W1_CWD, "s-w1", [
        user("You are a ccd worker."),
        assistant(tool_use("t1", 'ccd recv "$CCD_HANDLE" -t 86400'),
                  usage=_usage(1, 2)),
        tool_result("t1", W1_TASK),
        assistant({"type": "text", "text": "working on it"}, usage=_usage(3, 4)),
        assistant(tool_use("t2", 'ccd recv "$CCD_HANDLE" -t 86400'),
                  usage=_usage(5, 6)),
    ])
    write_transcript(root, W2_CWD, "s-w2", [
        user(W2_GOAL),
        assistant({"type": "text", "text": "ack"}, usage=_usage(7, 8)),
    ])
    return [
        {"handle": "disp", "model": "opus", "effort": "high", "owner": None,
         "cwd": DISP_CWD, "session": "s-disp"},
        {"handle": "w1", "model": "sonnet", "effort": "medium", "owner": "disp",
         "cwd": W1_CWD, "session": "s-w1"},
        {"handle": "w2", "model": "haiku", "effort": "low", "owner": "disp",
         "cwd": W2_CWD, "session": "s-w2"},
    ]


def by_handle(model: dict, handle: str) -> dict:
    return next(s for s in model["sessions"] if s["handle"] == handle)


# ----------------------------------------------------------------------


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ccd-dash-"))
    try:
        return run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(tmp: Path) -> int:
    root = tmp / "transcripts"
    root.mkdir()

    print("announce carries cwd and session (the only handle->transcript link):")
    b = Broker()
    call(b, "announce", handle="w1", effort="medium",
         cwd="/work/beta", session="s-w1")
    entry = (call(b, "roster").get("workers") or [{}])[0]
    check("roster reports cwd", entry.get("cwd") == "/work/beta", repr(entry))
    check("roster reports session", entry.get("session") == "s-w1", repr(entry))
    call(b, "claim", handle="w1", owner="disp")
    # A re-announce from a plain shell has no $CLAUDE_CODE_SESSION_ID and sends
    # an empty string; that must not blank what the session already reported.
    call(b, "announce", handle="w1", effort="medium",
         cwd="", session="")
    entry = (call(b, "roster").get("workers") or [{}])[0]
    check("an empty re-announce preserves cwd/session",
          entry.get("cwd") == "/work/beta" and entry.get("session") == "s-w1",
          repr(entry))
    check("and still preserves the claim", entry.get("owner") == "disp")
    call(b, "announce", handle="w1", effort="medium",
         cwd="/work/moved", session="s-new")
    entry = (call(b, "roster").get("workers") or [{}])[0]
    check("a re-announce that supplies them updates both",
          entry.get("cwd") == "/work/moved" and entry.get("session") == "s-new",
          repr(entry))
    call(b, "announce", handle="w9", effort="medium")
    entry = next(e for e in call(b, "roster")["workers"] if e["handle"] == "w9")
    check("a participant that announces neither is simply unlocated",
          entry.get("cwd") is None and entry.get("session") is None, repr(entry))

    print("\nlocating a transcript:")
    write_transcript(root, "/work/beta", "s-w1", [user("hi")])
    time.sleep(0.01)
    write_transcript(root, "/work/beta", "s-other", [user("someone else")])
    located = transcript.locate("/work/beta", "s-w1", root=root)
    check("an announced session id names the file exactly",
          located.resolved_by == "session-id"
          and located.path.name == "s-w1.jsonl", repr(located))
    located = transcript.locate("/work/beta", None, root=root)
    check("without one it falls back to the newest in that tree, and says so",
          located.resolved_by == "mtime" and located.path.name == "s-other.jsonl",
          repr(located))
    check("an announced session with no file resolves to nothing rather than "
          "guessing another session",
          not transcript.locate("/work/beta", "s-gone", root=root))
    check("no cwd means no transcript",
          not transcript.locate(None, "s-w1", root=root))
    check("an unknown tree means no transcript",
          not transcript.locate("/work/nowhere", None, root=root))

    print("\nreading one transcript:")
    fleet(root)
    facts = transcript.parse(root / transcript.encode_project_dir(W1_CWD) / "s-w1.jsonl")
    check("a worker blocked in `ccd recv` reads as parked",
          facts.status == "parked", facts.status)
    check("its last task is the message that recv delivered",
          facts.last_task == W1_TASK, repr(facts.last_task))
    check("tokens are summed across turns",
          facts.tokens["total"] == 1 + 2 + 3 + 4 + 5 + 6, repr(facts.tokens))
    check("the branch comes off the transcript", facts.branch == "main")
    facts = transcript.parse(root / transcript.encode_project_dir(DISP_CWD) / "s-disp.jsonl")
    check("the first substantive message skips command machinery",
          facts.first_message == DISP_GOAL, repr(facts.first_message))
    check("and system-reminders are stripped out of it",
          "not part of the goal" not in (facts.first_message or ""))
    check("its issue tag is extracted", facts.issue == "#11", repr(facts.issue))
    check("a finished turn reads as idle", facts.status == "idle", facts.status)
    check("cache tokens are counted",
          facts.tokens["cache_read"] == 1000, repr(facts.tokens))
    check("a session that ran nothing is unknown, not idle",
          transcript.parse(root / "nope.jsonl").status == "unknown")

    print("\na `ccd recv` in a comment is not a parked worker:")
    write_transcript(root, "/work/meta", "s-meta", [
        user("work on ccd itself"),
        assistant(tool_use("t1", "grep -n '`ccd recv`' skills/ccd-worker.md"),
                  usage=_usage(1, 1)),
    ])
    facts = transcript.parse(root / transcript.encode_project_dir("/work/meta") / "s-meta.jsonl")
    check("a mention inside another command reads as working, not parked",
          facts.status == "working", facts.status)

    print("\nmetadata is system-wide:")
    roster = fleet(root)
    model = dashboard.build(roster, None, root=root)
    check("every announced handle is present",
          [s["handle"] for s in model["sessions"]] == ["disp", "w1", "w2"],
          repr([s["handle"] for s in model["sessions"]]))
    disp, w1 = by_handle(model, "disp"), by_handle(model, "w1")
    check("the role comes off the claim graph, not a declaration",
          disp["role"] == "dispatcher" and w1["role"] == "worker")
    check("the owner is carried through", w1["owner"] == "disp")
    check("model and effort are carried through",
          (w1["model"], w1["effort"]) == ("sonnet", "medium"))
    check("the working tree is reported", w1["tree"]["cwd"] == W1_CWD)
    check("status is reported for every entry",
          [s["status"] for s in model["sessions"]] == ["idle", "parked", "idle"],
          repr([s["status"] for s in model["sessions"]]))
    check("cost is reported for every entry",
          all(s["cost"]["tokens"]["total"] > 0 for s in model["sessions"]))
    check("cost is tokens-only without a rate table",
          all(s["cost"]["usd"] is None for s in model["sessions"]))
    check("the transcript that was read is named, with how it was found",
          w1["transcript"]["resolved_by"] == "session-id"
          and w1["transcript"]["path"].endswith("s-w1.jsonl"))
    check("no entry carries content when nothing is scoped",
          all(s["content"] is None for s in model["sessions"]))

    rendered = dashboard.render_markdown(model)
    check("the rendered view lists every handle",
          all(f"`{h}`" in rendered for h in ("disp", "w1", "w2")))
    check("and contains no session's content",
          not any(text in rendered for text in (DISP_GOAL, W1_TASK, W2_GOAL)))

    print("\na handle with no transcript still appears:")
    model = dashboard.build(
        [{"handle": "opaque", "model": "local", "effort": "-", "owner": None}],
        None, root=root)
    only = model["sessions"][0]
    check("status is unknown rather than invented", only["status"] == "unknown")
    check("cost is empty rather than zero-that-looks-real",
          not only["cost"]["tokens"].get("total"), repr(only["cost"]))
    check("it renders without blowing up",
          "`opaque`" in dashboard.render_markdown(model))

    print("\ncontent is scoped to one handle:")
    model = dashboard.build(roster, "w1", root=root)
    check("the scoped handle has content", by_handle(model, "w1")["content"])
    check("its content is the last task it received",
          by_handle(model, "w1")["content"]["text"] == W1_TASK)
    check("labelled as such", by_handle(model, "w1")["content"]["kind"] == "last-task")
    check("no other handle does",
          all(s["content"] is None for s in model["sessions"]
              if s["handle"] != "w1"))
    rendered = dashboard.render_markdown(model)
    check("the rendered view shows the scoped session's line", W1_TASK in rendered)
    check("and no other client's material — the load-bearing constraint",
          DISP_GOAL not in rendered and W2_GOAL not in rendered)
    check("every handle is still listed", "`w2`" in rendered)

    model = dashboard.build(roster, "disp", root=root)
    check("a dispatcher's content is the goal it was given",
          by_handle(model, "disp")["content"]["text"] == DISP_GOAL)
    check("labelled as such",
          by_handle(model, "disp")["content"]["kind"] == "goal")
    check("with its issue tag",
          by_handle(model, "disp")["content"]["issue"] == "#11")
    rendered = dashboard.render_markdown(model)
    check("and the workers' material stays out of that call",
          W1_TASK not in rendered and W2_GOAL not in rendered)

    model = dashboard.build(roster, "w2", root=root)
    check("a worker with no task yet falls back to its own first message",
          by_handle(model, "w2")["content"] == {"kind": "goal", "text": W2_GOAL,
                                                "issue": None},
          repr(by_handle(model, "w2")["content"]))

    model = dashboard.build(roster, "ghost", root=root)
    check("scoping an unannounced handle is an error, not a silent empty view",
          model.get("scope_error") and "ghost" in model["scope_error"])
    check("and no content leaks in its place",
          all(s["content"] is None for s in model["sessions"]))

    print("\ncost in dollars, only when priced:")
    rates = {"claude-sonnet-5": {"input": 3.0, "output": 15.0,
                                 "cache_read": 0.3, "cache_creation": 3.75}}
    model = dashboard.build(roster, None, root=root, rates=rates)
    check("a priced session gets a figure",
          by_handle(model, "w1")["cost"]["usd"] is not None,
          repr(by_handle(model, "w1")["cost"]))
    check("an unpriced model stays None rather than undercounting",
          dashboard.build(roster, None, root=root,
                          rates={"other": {"input": 1}})["sessions"][0]["cost"]["usd"]
          is None)

    print("\nthe JSON model refuses to land in a git working tree:")
    repo = tmp / "somerepo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    model = dashboard.build(roster, "w1", root=root)
    refused = False
    try:
        dashboard.write_json(model, str(repo / "docs" / "fleet.json"))
    except dashboard.WriteRefused as exc:
        refused = "somerepo" in str(exc)
    check("a path nested inside a repo is refused", refused)

    worktree = tmp / "aworktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    refused = False
    try:
        dashboard.write_json(model, str(worktree / "fleet.json"))
    except dashboard.WriteRefused:
        refused = True
    check("a linked worktree (.git as a file) is refused too", refused)

    outside = tmp / "state" / "fleet.json"
    written = dashboard.write_json(model, str(outside))
    check("outside any repository it is written", written.is_file())
    reloaded = json.loads(written.read_text())
    check("and what lands is the model that was rendered",
          reloaded["scope"] == "w1"
          and reloaded["schema"] == dashboard.SCHEMA
          and len(reloaded["sessions"]) == 3)

    print("\nthrough the CLI (`ccd dashboard` pipes a roster reply into stdin):")
    reply = json.dumps({"ok": True, "workers": roster})
    env = dict(os.environ, CCD_TRANSCRIPT_ROOT=str(root),
               PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    sandbox = tmp / "cli"
    sandbox.mkdir()

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "ccd_dashboard", *args],
            input=reply, capture_output=True, text=True, env=env, cwd=sandbox)

    out = cli()
    check("it renders markdown by default",
          out.returncode == 0 and out.stdout.startswith("# ccd fleet"),
          repr(out.stdout[:80] + out.stderr[:200]))
    check("with no content in it",
          not any(t in out.stdout for t in (DISP_GOAL, W1_TASK, W2_GOAL)))
    out = cli("--scope", "w1")
    check("--scope adds that one session's line",
          W1_TASK in out.stdout and DISP_GOAL not in out.stdout)
    out = cli("--json")
    check("--json prints the model",
          json.loads(out.stdout)["schema"] == dashboard.SCHEMA)
    check("and nothing is written by default — the model is ephemeral",
          not list(sandbox.iterdir()), repr(list(sandbox.iterdir())))
    out = cli("--write", str(repo / "fleet.json"))
    check("--write into a repo exits non-zero and says why",
          out.returncode == 1 and "refusing to write" in out.stderr,
          repr(out.stderr[:200]))
    check("and leaves no file behind", not (repo / "fleet.json").exists())
    out = cli("--write", str(tmp / "state" / "cli.json"))
    check("--write outside one succeeds",
          out.returncode == 0 and (tmp / "state" / "cli.json").is_file())
    out = cli("--scope")
    check("a malformed invocation fails loudly", out.returncode != 0)

    print("\nthe dashboard is read-only:")
    cli = (Path(__file__).resolve().parent.parent / "ccd").read_text()
    body = cli.split("cmd_dashboard() {", 1)[1].split("\ncmd_ping()", 1)[0]
    calls = [line for line in body.splitlines() if "_ccd_rpc" in line]
    check("its only broker call is `roster`",
          len(calls) == 1 and "_ccd_rpc roster" in calls[0], repr(calls))
    for method in ("send", "claim", "release", "retire", "announce"):
        check(f"it never calls {method}", f"_ccd_rpc {method}" not in body)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("PASS — metadata is system-wide, content is scoped, "
          "and the writer stays out of git trees")
    return 0


if __name__ == "__main__":
    sys.exit(main())
