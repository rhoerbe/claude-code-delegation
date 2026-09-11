#!/usr/bin/env python3
"""Broker 1.3.0: honest field names, pid-liveness reaping, drift
(claude-code-delegation#13).

Exercises the broker directly for schema/liveness/reaping — no socket needed,
same shape as test_affiliation.py. The drift section also runs `ccd ls`
end to end as a subprocess against a real broker and a synthetic transcript,
the same way test_dashboard.py's CLI section drives `ccd dashboard`.

Run: tests/test_liveness.py
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
from ccd_dashboard import transcript  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        FAILURES.append(name)


def call(b: Broker, method: str, **args) -> dict:
    return b.dispatch({"method": method, "args": args}) or {}


def entry_of(reply: dict, handle: str) -> dict:
    return next((w for w in reply.get("workers", []) if w.get("handle") == handle), {})


def spawn_and_kill() -> int:
    """A pid that is real, then immediately dead — for reaping tests."""
    proc = subprocess.Popen(["sleep", "100"])
    pid = proc.pid
    proc.terminate()
    proc.wait()
    return pid


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ccd-liveness-"))
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: Path) -> int:
    print("announce carries the honest field names (#13):")
    b = Broker()
    call(b, "announce", handle="w1", slot="sonnet", effort="medium",
         model="claude-sonnet-5", pid=os.getpid(), mapping="kimi-k3-max")
    entry = entry_of(call(b, "roster"), "w1")
    check("slot is the closed vocabulary, not the resolved id",
          entry.get("slot") == "sonnet", repr(entry))
    check("model is the resolved id", entry.get("model") == "claude-sonnet-5",
          repr(entry))
    check("pid is carried", entry.get("pid") == os.getpid(), repr(entry))
    check("mapping is carried", entry.get("mapping") == "kimi-k3-max", repr(entry))
    check("this process's own pid reads alive", entry.get("alive") is True,
          repr(entry))

    print("\nmodel/mapping are carried forward when omitted, pid is NOT:")
    call(b, "announce", handle="w1", slot="sonnet", effort="medium")
    entry = entry_of(call(b, "roster"), "w1")
    check("model survives an announce that does not repeat it",
          entry.get("model") == "claude-sonnet-5", repr(entry))
    check("mapping survives too", entry.get("mapping") == "kimi-k3-max", repr(entry))
    check("pid resets to None instead of inheriting a stale identity",
          entry.get("pid") is None, repr(entry))
    check("an unset pid reads alive=None (nothing to check), not False",
          entry.get("alive") is None, repr(entry))

    print("\na handle with no pid at all is never reaped (a plain shell):")
    call(b, "announce", handle="shell", slot="haiku", effort="low")
    for i in range(3):
        reply = call(b, "roster")
        entry = entry_of(reply, "shell")
        check(f"still on the roster after read {i + 1}", bool(entry), repr(reply))
        check(f"alive is None, not False (read {i + 1})",
              entry.get("alive") is None, repr(entry))

    print("\na dead pid is shown dead once, then reaped:")
    b = Broker()
    dead_pid = spawn_and_kill()
    check("the spawned pid is actually gone",
          not os.path.exists(f"/proc/{dead_pid}"), dead_pid)
    call(b, "announce", handle="w2", slot="opus", effort="high", pid=dead_pid)
    reply = call(b, "roster")
    entry = entry_of(reply, "w2")
    check("the read that discovers it shows alive=false",
          entry.get("alive") is False, repr(entry))
    check("...and still returns it that one time", bool(entry), repr(reply))
    reply2 = call(b, "roster")
    check("the NEXT read no longer carries it",
          not entry_of(reply2, "w2"), repr(reply2))
    check("--exclusive can reuse the name now that it is reaped",
          call(b, "announce", handle="w2", slot="opus", effort="high",
               exclusive=True).get("ok"))

    print("\na live pid reads alive=true and is not reaped:")
    b = Broker()
    call(b, "announce", handle="me", slot="sonnet", effort="medium", pid=os.getpid())
    reply = call(b, "roster")
    entry = entry_of(reply, "me")
    check("alive is true for this running process", entry.get("alive") is True,
          repr(entry))
    check("still there on a second read",
          bool(entry_of(call(b, "roster"), "me")))

    print("\na dead dispatcher's claims are released on reap, like retire:")
    b = Broker()
    dead_pid = spawn_and_kill()
    call(b, "announce", handle="disp", slot="opus", effort="high", pid=dead_pid)
    call(b, "announce", handle="w3", slot="sonnet", effort="medium")
    call(b, "claim", handle="w3", owner="disp")
    check("w3 is claimed before the reap",
          entry_of(call(b, "roster"), "w3").get("owner") == "disp")
    call(b, "roster")  # first read: shows disp dead, then reaps it
    check("w3's claim is released once its dispatcher is reaped",
          entry_of(call(b, "roster"), "w3").get("owner") is None)

    print("\nre-announcing a live handle at a different slot is still refused:")
    b = Broker()
    call(b, "announce", handle="w1", slot="sonnet", effort="medium")
    r = call(b, "announce", handle="w1", slot="opus", effort="medium")
    check("different slot is refused", not r.get("ok"), repr(r))
    r = call(b, "announce", handle="w1", slot="sonnet", effort="high")
    check("different effort is refused too", not r.get("ok"), repr(r))

    print("\nan invalid/empty pid is treated as no pid, not an error:")
    b = Broker()
    check("empty string pid is accepted as absent",
          call(b, "announce", handle="w1", slot="sonnet", effort="medium",
               pid="").get("ok"))
    check("and reads as alive=None",
          entry_of(call(b, "roster"), "w1").get("alive") is None)
    check("garbage pid is accepted as absent, not a crash",
          call(b, "announce", handle="w4", slot="sonnet", effort="medium",
               pid="not-a-pid").get("ok"))
    check("...and reads as alive=None too",
          entry_of(call(b, "roster"), "w4").get("alive") is None)

    print("\nobserved effort/model come off the transcript, not the broker:")
    root = tmp / "transcripts"
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
    check("the transcript's own top-level effort is the observed effort",
          facts.effort == "high", repr(facts.effort))
    check("message.model is the observed model",
          facts.model == "claude-sonnet-5", repr(facts.model))

    print("\n`ccd ls` marks drift end to end (subprocess, real broker):")
    repo_root = Path(__file__).resolve().parent.parent
    ccd = str(repo_root / "ccd")
    sock = tmp / "ccd-liveness.sock"
    pidfile = tmp / "ccd-liveness.pid"
    env = dict(os.environ, CCD_SOCKET=str(sock), CCD_PIDFILE=str(pidfile),
               CCD_TRANSCRIPT_ROOT=str(root),
               PYTHONPATH=str(repo_root))

    def ccd_run(*args, **kw):
        return subprocess.run([ccd, *args], capture_output=True, text=True,
                              env=env, timeout=15, **kw)

    ccd_run("broker", "start")
    try:
        # Declared "medium", but the transcript above (s1, cwd /work/drift)
        # reports effort "high" — a real mismatch, injected the same way
        # cmd_announce's own cwd/session args work (raw RPC, since
        # cmd_announce only ever reports THIS test's own accurate pid/cwd).
        rpc = subprocess.run(
            [sys.executable, "-c",
             "import json,socket,sys\n"
             "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
             "s.connect(sys.argv[1])\n"
             "s.sendall((json.dumps({'method':'announce','args':{"
             "'handle':'drifted','slot':'sonnet','effort':'medium',"
             "'cwd':'/work/drift','session':'s1'}})+chr(10)).encode())\n"
             "print(s.recv(4096).decode())\n",
             str(sock)],
            capture_output=True, text=True, timeout=10)
        check("raw announce for the drift fixture succeeded",
              json.loads(rpc.stdout).get("ok") is True,
              repr(rpc.stdout + rpc.stderr))

        out = ccd_run("ls")
        check("ccd ls exits 0", out.returncode == 0, repr(out.stderr))
        lines = out.stdout.strip().splitlines()
        check("a header row is printed", lines and lines[0].startswith("HANDLE"),
              repr(lines[:1]))
        row = next((l for l in lines if l.startswith("drifted\t")), "")
        cols = row.split("\t")
        check("the row has slot/effort/owner/model/pid/status/drift columns",
              len(cols) == 8, repr(cols))
        check("declared effort is shown (medium)", cols[2] == "medium", repr(cols))
        check("drift is marked '!' — observed (high) != declared (medium)",
              cols[-1] == "!", repr(cols))
    finally:
        ccd_run("broker", "stop")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("PASS — honest field names, pid-liveness reaping and drift "
          "behave as claude-code-delegation#13 specifies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
