"""Fleet dashboard: metadata system-wide, content scoped (ADR-0008, issue #11).

Everything here runs against synthetic transcripts under a temporary root, so
the suite never reads the machine's real sessions and never needs a broker
socket. The broker section drives `Broker` directly, as `test_affiliation.py`
does — the announce fields are broker logic and need no transport.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccd_dashboard import dashboard, transcript

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
# announce carries cwd and session
# ----------------------------------------------------------------------

def test_roster_reports_cwd_and_session(call, broker):
    call(broker, "announce", handle="w1", effort="medium",
         cwd="/work/beta", session="s-w1")
    entry = (call(broker, "roster").get("workers") or [{}])[0]
    assert entry.get("cwd") == "/work/beta", repr(entry)
    assert entry.get("session") == "s-w1", repr(entry)


def test_empty_reannounce_preserves_cwd_session_and_claim(call, broker):
    call(broker, "announce", handle="w1", effort="medium",
         cwd="/work/beta", session="s-w1")
    call(broker, "claim", handle="w1", owner="disp")
    # A re-announce from a plain shell has no $CLAUDE_CODE_SESSION_ID and
    # sends an empty string; that must not blank what the session already
    # reported.
    call(broker, "announce", handle="w1", effort="medium", cwd="", session="")
    entry = (call(broker, "roster").get("workers") or [{}])[0]
    assert entry.get("cwd") == "/work/beta", repr(entry)
    assert entry.get("session") == "s-w1", repr(entry)
    assert entry.get("owner") == "disp"


def test_reannounce_that_supplies_them_updates_both(call, broker):
    call(broker, "announce", handle="w1", effort="medium",
         cwd="/work/beta", session="s-w1")
    call(broker, "announce", handle="w1", effort="medium",
         cwd="/work/moved", session="s-new")
    entry = (call(broker, "roster").get("workers") or [{}])[0]
    assert entry.get("cwd") == "/work/moved", repr(entry)
    assert entry.get("session") == "s-new", repr(entry)


def test_a_participant_announcing_neither_is_unlocated(call, broker):
    call(broker, "announce", handle="w9", effort="medium")
    entry = next(e for e in call(broker, "roster")["workers"] if e["handle"] == "w9")
    assert entry.get("cwd") is None, repr(entry)
    assert entry.get("session") is None, repr(entry)


# ----------------------------------------------------------------------
# locating a transcript
# ----------------------------------------------------------------------

def test_an_announced_session_id_names_the_file_exactly(tmp_path):
    write_transcript(tmp_path, "/work/beta", "s-w1", [user("hi")])
    time.sleep(0.01)
    write_transcript(tmp_path, "/work/beta", "s-other", [user("someone else")])
    located = transcript.locate("/work/beta", "s-w1", root=tmp_path)
    assert located.resolved_by == "session-id", repr(located)
    assert located.path.name == "s-w1.jsonl", repr(located)


def test_without_a_session_id_it_falls_back_to_the_newest(tmp_path):
    write_transcript(tmp_path, "/work/beta", "s-w1", [user("hi")])
    time.sleep(0.01)
    write_transcript(tmp_path, "/work/beta", "s-other", [user("someone else")])
    located = transcript.locate("/work/beta", None, root=tmp_path)
    assert located.resolved_by == "mtime", repr(located)
    assert located.path.name == "s-other.jsonl", repr(located)


def test_an_announced_session_with_no_file_resolves_to_nothing(tmp_path):
    write_transcript(tmp_path, "/work/beta", "s-w1", [user("hi")])
    assert not transcript.locate("/work/beta", "s-gone", root=tmp_path)


def test_no_cwd_means_no_transcript(tmp_path):
    write_transcript(tmp_path, "/work/beta", "s-w1", [user("hi")])
    assert not transcript.locate(None, "s-w1", root=tmp_path)


def test_an_unknown_tree_means_no_transcript(tmp_path):
    write_transcript(tmp_path, "/work/beta", "s-w1", [user("hi")])
    assert not transcript.locate("/work/nowhere", None, root=tmp_path)


# ----------------------------------------------------------------------
# reading one transcript
# ----------------------------------------------------------------------

def test_a_worker_blocked_in_ccd_recv_reads_as_parked(tmp_path):
    fleet(tmp_path)
    facts = transcript.parse(tmp_path / transcript.encode_project_dir(W1_CWD) / "s-w1.jsonl")
    assert facts.status == "parked", facts.status
    assert facts.last_task == W1_TASK, repr(facts.last_task)
    assert facts.tokens["total"] == 1 + 2 + 3 + 4 + 5 + 6, repr(facts.tokens)
    assert facts.branch == "main"


def test_the_first_substantive_message_skips_command_machinery(tmp_path):
    fleet(tmp_path)
    facts = transcript.parse(tmp_path / transcript.encode_project_dir(DISP_CWD) / "s-disp.jsonl")
    assert facts.first_message == DISP_GOAL, repr(facts.first_message)
    assert "not part of the goal" not in (facts.first_message or "")
    assert facts.issue == "#11", repr(facts.issue)
    assert facts.status == "idle", facts.status
    assert facts.tokens["cache_read"] == 1000, repr(facts.tokens)


def test_a_session_that_ran_nothing_is_unknown_not_idle(tmp_path):
    fleet(tmp_path)
    assert transcript.parse(tmp_path / "nope.jsonl").status == "unknown"


def test_a_ccd_recv_in_a_comment_is_not_a_parked_worker(tmp_path):
    write_transcript(tmp_path, "/work/meta", "s-meta", [
        user("work on ccd itself"),
        assistant(tool_use("t1", "grep -n '`ccd recv`' skills/ccd-worker.md"),
                  usage=_usage(1, 1)),
    ])
    facts = transcript.parse(tmp_path / transcript.encode_project_dir("/work/meta") / "s-meta.jsonl")
    assert facts.status == "working", facts.status


# ----------------------------------------------------------------------
# metadata is system-wide
# ----------------------------------------------------------------------

def test_every_announced_handle_is_present(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, None, root=tmp_path)
    assert [s["handle"] for s in model["sessions"]] == ["disp", "w1", "w2"], \
        repr([s["handle"] for s in model["sessions"]])


def test_role_owner_model_effort_tree_status_cost_are_reported(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, None, root=tmp_path)
    disp, w1 = by_handle(model, "disp"), by_handle(model, "w1")
    assert disp["role"] == "dispatcher"
    assert w1["role"] == "worker"  # role comes off the claim graph, not a declaration
    assert w1["owner"] == "disp"
    assert (w1["model"], w1["effort"]) == ("sonnet", "medium")
    assert w1["tree"]["cwd"] == W1_CWD
    assert [s["status"] for s in model["sessions"]] == ["idle", "parked", "idle"], \
        repr([s["status"] for s in model["sessions"]])
    assert all(s["cost"]["tokens"]["total"] > 0 for s in model["sessions"])
    assert all(s["cost"]["usd"] is None for s in model["sessions"])  # no rate table
    assert w1["transcript"]["resolved_by"] == "session-id"
    assert w1["transcript"]["path"].endswith("s-w1.jsonl")
    assert all(s["content"] is None for s in model["sessions"])  # nothing scoped


def test_the_rendered_view_lists_every_handle_with_no_content(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, None, root=tmp_path)
    rendered = dashboard.render_markdown(model)
    assert all(f"`{h}`" in rendered for h in ("disp", "w1", "w2"))
    assert not any(text in rendered for text in (DISP_GOAL, W1_TASK, W2_GOAL))


def test_a_handle_with_no_transcript_still_appears(tmp_path):
    model = dashboard.build(
        [{"handle": "opaque", "model": "local", "effort": "-", "owner": None}],
        None, root=tmp_path)
    only = model["sessions"][0]
    assert only["status"] == "unknown"  # not invented
    assert not only["cost"]["tokens"].get("total"), repr(only["cost"])
    assert "`opaque`" in dashboard.render_markdown(model)  # renders without blowing up


# ----------------------------------------------------------------------
# content is scoped to one handle
# ----------------------------------------------------------------------

def test_the_scoped_worker_gets_its_last_task_as_content(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "w1", root=tmp_path)
    assert by_handle(model, "w1")["content"]
    assert by_handle(model, "w1")["content"]["text"] == W1_TASK
    assert by_handle(model, "w1")["content"]["kind"] == "last-task"
    assert all(s["content"] is None for s in model["sessions"] if s["handle"] != "w1")


def test_the_rendered_scoped_view_shows_only_that_sessions_material(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "w1", root=tmp_path)
    rendered = dashboard.render_markdown(model)
    assert W1_TASK in rendered
    # The load-bearing constraint: no other client's material leaks in.
    assert DISP_GOAL not in rendered and W2_GOAL not in rendered
    assert "`w2`" in rendered  # every handle is still listed


def test_a_dispatchers_content_is_the_goal_it_was_given(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "disp", root=tmp_path)
    assert by_handle(model, "disp")["content"]["text"] == DISP_GOAL
    assert by_handle(model, "disp")["content"]["kind"] == "goal"
    assert by_handle(model, "disp")["content"]["issue"] == "#11"
    rendered = dashboard.render_markdown(model)
    assert W1_TASK not in rendered and W2_GOAL not in rendered


def test_a_worker_with_no_task_yet_falls_back_to_its_own_first_message(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "w2", root=tmp_path)
    assert by_handle(model, "w2")["content"] == {
        "kind": "goal", "text": W2_GOAL, "issue": None}, \
        repr(by_handle(model, "w2")["content"])


def test_scoping_an_unannounced_handle_is_an_error_not_a_silent_empty_view(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "ghost", root=tmp_path)
    assert model.get("scope_error") and "ghost" in model["scope_error"]
    assert all(s["content"] is None for s in model["sessions"])


# ----------------------------------------------------------------------
# cost in dollars, only when priced
# ----------------------------------------------------------------------

def test_a_priced_session_gets_a_dollar_figure(tmp_path):
    roster = fleet(tmp_path)
    rates = {"claude-sonnet-5": {"input": 3.0, "output": 15.0,
                                 "cache_read": 0.3, "cache_creation": 3.75}}
    model = dashboard.build(roster, None, root=tmp_path, rates=rates)
    assert by_handle(model, "w1")["cost"]["usd"] is not None, \
        repr(by_handle(model, "w1")["cost"])


def test_an_unpriced_model_stays_none_rather_than_undercounting(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, None, root=tmp_path, rates={"other": {"input": 1}})
    assert model["sessions"][0]["cost"]["usd"] is None


# ----------------------------------------------------------------------
# the JSON model refuses to land in a git working tree
# ----------------------------------------------------------------------

def test_a_path_nested_inside_a_repo_is_refused(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "w1", root=tmp_path)
    repo = tmp_path / "somerepo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    refused = False
    try:
        dashboard.write_json(model, str(repo / "docs" / "fleet.json"))
    except dashboard.WriteRefused as exc:
        refused = "somerepo" in str(exc)
    assert refused


def test_a_linked_worktree_git_as_a_file_is_refused_too(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "w1", root=tmp_path)
    worktree = tmp_path / "aworktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    refused = False
    try:
        dashboard.write_json(model, str(worktree / "fleet.json"))
    except dashboard.WriteRefused:
        refused = True
    assert refused


def test_outside_any_repository_it_is_written_and_reloads_intact(tmp_path):
    roster = fleet(tmp_path)
    model = dashboard.build(roster, "w1", root=tmp_path)
    outside = tmp_path / "state" / "fleet.json"
    written = dashboard.write_json(model, str(outside))
    assert written.is_file()
    reloaded = json.loads(written.read_text())
    assert reloaded["scope"] == "w1"
    assert reloaded["schema"] == dashboard.SCHEMA
    assert len(reloaded["sessions"]) == 3


# ----------------------------------------------------------------------
# through the CLI (`ccd dashboard` pipes a roster reply into stdin)
# ----------------------------------------------------------------------

def _cli_env_and_reply(tmp_path):
    roster = fleet(tmp_path)
    reply = json.dumps({"ok": True, "workers": roster})
    env = dict(os.environ, CCD_TRANSCRIPT_ROOT=str(tmp_path),
               PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    return reply, env


def _run_cli(reply, env, sandbox, *args):
    return subprocess.run(
        [sys.executable, "-m", "ccd_dashboard", *args],
        input=reply, capture_output=True, text=True, env=env, cwd=sandbox)


def test_cli_renders_markdown_by_default_with_no_content(tmp_path):
    reply, env = _cli_env_and_reply(tmp_path)
    sandbox = tmp_path / "cli"
    sandbox.mkdir()
    out = _run_cli(reply, env, sandbox)
    assert out.returncode == 0 and out.stdout.startswith("# ccd fleet"), \
        repr(out.stdout[:80] + out.stderr[:200])
    assert not any(t in out.stdout for t in (DISP_GOAL, W1_TASK, W2_GOAL))


def test_cli_scope_flag_adds_that_sessions_line(tmp_path):
    reply, env = _cli_env_and_reply(tmp_path)
    sandbox = tmp_path / "cli"
    sandbox.mkdir()
    out = _run_cli(reply, env, sandbox, "--scope", "w1")
    assert W1_TASK in out.stdout and DISP_GOAL not in out.stdout


def test_cli_json_flag_prints_the_model_and_writes_nothing(tmp_path):
    reply, env = _cli_env_and_reply(tmp_path)
    sandbox = tmp_path / "cli"
    sandbox.mkdir()
    out = _run_cli(reply, env, sandbox, "--json")
    assert json.loads(out.stdout)["schema"] == dashboard.SCHEMA
    assert not list(sandbox.iterdir()), repr(list(sandbox.iterdir()))


def test_cli_write_into_a_repo_refuses_and_leaves_no_file(tmp_path):
    reply, env = _cli_env_and_reply(tmp_path)
    sandbox = tmp_path / "cli"
    sandbox.mkdir()
    repo = tmp_path / "somerepo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    out = _run_cli(reply, env, sandbox, "--write", str(repo / "fleet.json"))
    assert out.returncode == 1 and "refusing to write" in out.stderr, \
        repr(out.stderr[:200])
    assert not (repo / "fleet.json").exists()


def test_cli_write_outside_a_repo_succeeds(tmp_path):
    reply, env = _cli_env_and_reply(tmp_path)
    sandbox = tmp_path / "cli"
    sandbox.mkdir()
    out = _run_cli(reply, env, sandbox, "--write", str(tmp_path / "state" / "cli.json"))
    assert out.returncode == 0 and (tmp_path / "state" / "cli.json").is_file()


def test_cli_a_malformed_invocation_fails_loudly(tmp_path):
    reply, env = _cli_env_and_reply(tmp_path)
    sandbox = tmp_path / "cli"
    sandbox.mkdir()
    out = _run_cli(reply, env, sandbox, "--scope")
    assert out.returncode != 0


# ----------------------------------------------------------------------
# the dashboard is read-only
# ----------------------------------------------------------------------

def test_ccd_dashboard_only_calls_roster(repo_root):
    cli = (repo_root / "ccd").read_text()
    body = cli.split("cmd_dashboard() {", 1)[1].split("\ncmd_ping()", 1)[0]
    calls = [line for line in body.splitlines() if "_ccd_rpc" in line]
    assert len(calls) == 1 and "_ccd_rpc roster" in calls[0], repr(calls)


@pytest.mark.parametrize("method", ["send", "claim", "release", "retire", "announce"])
def test_ccd_dashboard_never_calls_a_mutating_method(repo_root, method):
    cli = (repo_root / "ccd").read_text()
    body = cli.split("cmd_dashboard() {", 1)[1].split("\ncmd_ping()", 1)[0]
    assert f"_ccd_rpc {method}" not in body
