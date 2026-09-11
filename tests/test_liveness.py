"""Broker 1.3.0: honest field names, pid-liveness reaping, drift
(claude-code-delegation#13).

Exercises the broker directly for schema/liveness/reaping — no socket needed,
same shape as test_affiliation.py. The drift section also runs `ccd ls`
end to end as a subprocess against a real broker and a synthetic transcript,
the same way test_dashboard.py's CLI section drives `ccd dashboard`.

Note: this schema went through two passes before anything shipped. The first
pass added a `slot` field (fable/opus/sonnet/haiku) alongside the renamed
`model`; a live probe done right after settled that naming a model directly
reaches it exactly as well as routing through a slot alias, so `slot` was
dropped again before release. What ships is `effort`, `model`, `pid`,
`mapping` — no `slot`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ccd_broker.broker import Broker
from ccd_dashboard import transcript


def entry_of(reply: dict, handle: str) -> dict:
    return next((w for w in reply.get("workers", []) if w.get("handle") == handle), {})


def spawn_and_kill() -> int:
    """A pid that is real, then immediately dead — for reaping tests."""
    proc = subprocess.Popen(["sleep", "100"])
    pid = proc.pid
    proc.terminate()
    proc.wait()
    return pid


def test_announce_carries_the_honest_field_names_no_slot(call, broker):
    call(broker, "announce", handle="w1", effort="medium",
         model="claude-sonnet-5", pid=os.getpid(), mapping="kimi-k3-max")
    entry = entry_of(call(broker, "roster"), "w1")
    assert "slot" not in entry, repr(entry)
    assert entry.get("model") == "claude-sonnet-5", repr(entry)
    assert entry.get("effort") == "medium", repr(entry)
    assert entry.get("pid") == os.getpid(), repr(entry)
    assert entry.get("mapping") == "kimi-k3-max", repr(entry)
    assert entry.get("alive") is True, repr(entry)


def test_model_and_mapping_carried_forward_but_pid_is_not(call, broker):
    call(broker, "announce", handle="w1", effort="medium",
         model="claude-sonnet-5", pid=os.getpid(), mapping="kimi-k3-max")
    call(broker, "announce", handle="w1", effort="medium")
    entry = entry_of(call(broker, "roster"), "w1")
    assert entry.get("model") == "claude-sonnet-5", repr(entry)
    assert entry.get("mapping") == "kimi-k3-max", repr(entry)
    assert entry.get("pid") is None, repr(entry)
    assert entry.get("alive") is None, repr(entry)


@pytest.mark.parametrize("read_number", [1, 2, 3])
def test_a_handle_with_no_pid_is_never_reaped(call, broker, read_number):
    # A plain shell announces with no pid at all.
    call(broker, "announce", handle="shell", effort="low")
    for _ in range(read_number):
        reply = call(broker, "roster")
    entry = entry_of(reply, "shell")
    assert entry, repr(reply)
    assert entry.get("alive") is None, repr(entry)


def test_a_dead_pid_is_shown_dead_once_then_reaped(call, broker):
    dead_pid = spawn_and_kill()
    assert not os.path.exists(f"/proc/{dead_pid}"), dead_pid

    call(broker, "announce", handle="w2", effort="high", pid=dead_pid)
    reply = call(broker, "roster")
    entry = entry_of(reply, "w2")
    assert entry.get("alive") is False, repr(entry)
    assert entry, repr(reply)  # ...and still returns it that one time

    reply2 = call(broker, "roster")
    assert not entry_of(reply2, "w2"), repr(reply2)  # the NEXT read drops it

    assert call(broker, "announce", handle="w2", effort="high",
                exclusive=True).get("ok")  # the name is reaped, so reusable


def test_a_live_pid_reads_alive_and_is_not_reaped(call, broker):
    call(broker, "announce", handle="me", effort="medium", pid=os.getpid())
    reply = call(broker, "roster")
    entry = entry_of(reply, "me")
    assert entry.get("alive") is True, repr(entry)
    assert entry_of(call(broker, "roster"), "me")  # still there on a 2nd read


def test_a_dead_dispatchers_claims_are_released_on_reap(call, broker):
    dead_pid = spawn_and_kill()
    call(broker, "announce", handle="disp", effort="high", pid=dead_pid)
    call(broker, "announce", handle="w3", effort="medium")
    call(broker, "claim", handle="w3", owner="disp")
    assert entry_of(call(broker, "roster"), "w3").get("owner") == "disp"

    call(broker, "roster")  # first read: shows disp dead, then reaps it
    assert entry_of(call(broker, "roster"), "w3").get("owner") is None


def test_reannounce_at_a_different_effort_is_refused(call, broker):
    call(broker, "announce", handle="w1", effort="medium", model="claude-sonnet-5")
    r = call(broker, "announce", handle="w1", effort="high")
    assert not r.get("ok"), repr(r)


def test_an_omitted_model_is_not_a_different_session(call, broker):
    """Model is optional and carried forward, so not repeating it says nothing.

    This is why `model` cannot simply join `effort` in the comparison: a plain
    re-announce from somewhere that has no manifest to resolve one would read
    as a hijack.
    """
    call(broker, "announce", handle="w1", effort="medium", model="claude-sonnet-5")
    r = call(broker, "announce", handle="w1", effort="medium")
    assert r.get("ok"), repr(r)
    assert entry_of(call(broker, "roster"), "w1").get("model") == "claude-sonnet-5"


def test_two_declared_models_that_disagree_are_refused(call, broker):
    """1.4.0: when BOTH sides declare a model and they differ, that is a
    different session taking the name — live the moment `ccd launch` starts
    sending one."""
    call(broker, "announce", handle="w1", effort="medium", model="claude-sonnet-5")
    r = call(broker, "announce", handle="w1", effort="medium",
             model="moonshotai/kimi-k3")
    assert not r.get("ok"), repr(r)
    assert "claude-sonnet-5" in r.get("reason", "")
    assert entry_of(call(broker, "roster"), "w1").get("model") == "claude-sonnet-5"


def test_a_disagreeing_model_is_overridable_with_force(call, broker):
    call(broker, "announce", handle="w1", effort="medium", model="claude-sonnet-5")
    r = call(broker, "announce", handle="w1", effort="medium",
             model="moonshotai/kimi-k3", force="1")
    assert r.get("ok"), repr(r)
    assert entry_of(call(broker, "roster"), "w1").get("model") == "moonshotai/kimi-k3"


def test_an_invalid_or_empty_pid_is_treated_as_no_pid(call, broker):
    assert call(broker, "announce", handle="w1", effort="medium", pid="").get("ok")
    assert entry_of(call(broker, "roster"), "w1").get("alive") is None

    assert call(broker, "announce", handle="w4", effort="medium",
                pid="not-a-pid").get("ok")
    assert entry_of(call(broker, "roster"), "w4").get("alive") is None


def test_observed_effort_and_model_come_off_the_transcript_not_the_broker(tmp_path):
    root = tmp_path / "transcripts"
    proj = root / "-work-drift"
    proj.mkdir(parents=True)
    path = proj / "s1.jsonl"
    path.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-09-11T10:00:00.000Z",
        "gitBranch": "main", "effort": "high",
        "message": {"role": "assistant", "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    }) + "\n", encoding="utf-8")
    facts = transcript.parse(path)
    assert facts.effort == "high", repr(facts.effort)
    assert facts.model == "claude-sonnet-5", repr(facts.model)


def test_ccd_ls_marks_drift_end_to_end(tmp_path, rpc):
    # Declared "medium", but the transcript below (s1, cwd /work/drift)
    # reports effort "high" — a real mismatch, injected the same way
    # cmd_announce's own cwd/session args work (raw RPC, since cmd_announce
    # only ever reports THIS test's own accurate pid/cwd).
    root = tmp_path / "transcripts"
    proj = root / "-work-drift"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-09-11T10:00:00.000Z",
        "gitBranch": "main", "effort": "high",
        "message": {"role": "assistant", "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    }) + "\n", encoding="utf-8")

    repo_root = Path(__file__).resolve().parent.parent
    ccd = str(repo_root / "ccd")
    sock = tmp_path / "ccd-liveness.sock"
    pidfile = tmp_path / "ccd-liveness.pid"
    env = dict(os.environ, CCD_SOCKET=str(sock), CCD_PIDFILE=str(pidfile),
               CCD_TRANSCRIPT_ROOT=str(root),
               PYTHONPATH=str(repo_root))

    def ccd_run(*args, **kw):
        return subprocess.run([ccd, *args], capture_output=True, text=True,
                              env=env, timeout=15, **kw)

    ccd_run("broker", "start")
    try:
        reply = rpc(str(sock), "announce", {
            "handle": "drifted", "effort": "medium",
            "cwd": "/work/drift", "session": "s1"})
        assert reply.get("ok") is True, repr(reply)

        out = ccd_run("ls")
        assert out.returncode == 0, repr(out.stderr)
        lines = out.stdout.strip().splitlines()
        # No header row, deliberately: hosting's ccd_overview.py treats every
        # non-blank line as one worker's data (claude-code-delegation#13) —
        # a header would misparse as a fake handle literally named "HANDLE".
        assert all(not l.startswith("HANDLE\t") for l in lines), repr(lines[:1])
        row = next((l for l in lines if l.startswith("drifted\t")), "")
        cols = row.split("\t")
        # handle, effort, owner, model, pid, status, drift — no slot column.
        assert len(cols) == 7, repr(cols)
        assert cols[1] == "medium", repr(cols)
        assert cols[-1] == "!", repr(cols)  # observed (high) != declared (medium)
    finally:
        ccd_run("broker", "stop")


# ----------------------------------------------------------------------
# handle identity by pid (1.4.0)
# ----------------------------------------------------------------------
# A pid is the one identity signal an impostor cannot simply declare: effort
# and model are supplied by whoever announces. So when both sides have one it
# decides, and the declared metadata does not get a vote.

@pytest.fixture
def live_pid():
    """A pid that is real and stays running for the duration of a test."""
    proc = subprocess.Popen(["sleep", "100"])
    yield proc.pid
    proc.terminate()
    proc.wait()


def test_a_same_pid_reannounce_is_always_allowed(call, broker, live_pid):
    """Rule (a): the Esc-interrupted worker reclaiming its own handle.

    Allowed whatever the metadata now says — a session that legitimately
    changed effort mid-life is still that session, and locking it out of its
    own identity would be worse than the hijack this guards against.
    """
    call(broker, "announce", handle="w1", effort="medium",
         model="claude-sonnet-5", pid=str(live_pid))

    r = call(broker, "announce", handle="w1", effort="max",
             model="moonshotai/kimi-k3", pid=str(live_pid))
    assert r.get("ok"), repr(r)
    entry = entry_of(call(broker, "roster"), "w1")
    assert entry.get("effort") == "max"
    assert entry.get("model") == "moonshotai/kimi-k3"


def test_a_different_live_pid_is_refused_even_when_metadata_matches(
        call, broker, live_pid):
    """Rule (b): matching metadata is what two workers of the same shape look
    like, not evidence of one session returning."""
    other = subprocess.Popen(["sleep", "100"])
    try:
        call(broker, "announce", handle="w1", effort="medium",
             model="claude-sonnet-5", pid=str(live_pid))
        r = call(broker, "announce", handle="w1", effort="medium",
                 model="claude-sonnet-5", pid=str(other.pid))
        assert not r.get("ok"), repr(r)
        assert f"live pid {live_pid}" in r.get("reason", "")
        assert entry_of(call(broker, "roster"), "w1").get("pid") == live_pid
    finally:
        other.terminate()
        other.wait()


def test_the_hijack_refusal_is_overridable_with_force(call, broker, live_pid):
    other = subprocess.Popen(["sleep", "100"])
    try:
        call(broker, "announce", handle="w1", effort="medium", pid=str(live_pid))
        r = call(broker, "announce", handle="w1", effort="medium",
                 pid=str(other.pid), force="1")
        assert r.get("ok"), repr(r)
        assert entry_of(call(broker, "roster"), "w1").get("pid") == other.pid
    finally:
        other.terminate()
        other.wait()


def test_a_dead_recorded_pid_lets_a_new_session_take_the_name(call, broker):
    """Rule (c), and the reason it cannot be left to the sweep.

    Reaping happens on a `roster` read ONLY. Without this rule a dead
    session's handle stays unusable until someone happens to run `ccd ls` —
    the stale-handle failure #13 is about, in its most confusing form. Note
    there is deliberately NO roster read before the re-announce here.
    """
    dead = spawn_and_kill()
    call(broker, "announce", handle="w1", effort="medium",
         model="claude-sonnet-5", pid=str(dead))

    r = call(broker, "announce", handle="w1", effort="max",
             model="moonshotai/kimi-k3", pid=str(os.getpid()))
    assert r.get("ok"), repr(r)
    entry = entry_of(call(broker, "roster"), "w1")
    assert entry.get("pid") == os.getpid()
    assert entry.get("effort") == "max"


def test_taking_a_dead_handle_overwrites_rather_than_reaping(call, broker):
    """`roster` stays the only place an entry is reaped: the announce simply
    assigns over it, so the handle is never absent in between."""
    dead = spawn_and_kill()
    call(broker, "announce", handle="w1", effort="medium", pid=str(dead))
    call(broker, "announce", handle="w1", effort="medium", pid=str(os.getpid()))
    workers = call(broker, "roster").get("workers", [])
    assert [w["handle"] for w in workers] == ["w1"]


def test_a_pid_less_participant_keeps_the_old_metadata_rules(call, broker):
    """Rule (d): ADR-0005 has interactive/shell participants first-class, so a
    pid-less announce is never refused by a rule it cannot satisfy."""
    call(broker, "announce", handle="w1", effort="medium")
    assert call(broker, "announce", handle="w1", effort="medium").get("ok")
    assert not call(broker, "announce", handle="w1", effort="high").get("ok")


@pytest.mark.parametrize("who_has_the_pid", ["recorded", "announcing"])
def test_a_one_sided_pid_falls_back_to_metadata(call, broker, live_pid,
                                                who_has_the_pid):
    """Rule (e). The absence of a pid is not evidence about who is announcing,
    so it decides nothing; the comparison falls back to the declared metadata,
    exactly as before 1.4.0.

    Refusing the pid-less side instead would make a plain shell — and any
    backend that is not Claude Code — second-class, which ADR-0005 forbids.
    """
    first = {"pid": str(live_pid)} if who_has_the_pid == "recorded" else {}
    second = {} if who_has_the_pid == "recorded" else {"pid": str(live_pid)}

    # Separate handles: re-announcing the matching one would leave both sides
    # holding the same pid, which is rule (a) and a different state entirely.
    call(broker, "announce", handle="w-same", effort="medium", **first)
    assert call(broker, "announce", handle="w-same", effort="medium",
                **second).get("ok")

    call(broker, "announce", handle="w-diff", effort="medium", **first)
    assert not call(broker, "announce", handle="w-diff", effort="high",
                    **second).get("ok")
