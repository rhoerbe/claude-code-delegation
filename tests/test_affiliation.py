#!/usr/bin/env python3
"""Claim-based worker affiliation (ADR-0007, issue #7).

Exercises the broker directly — affiliation is broker logic and needs no
socket. The one case that does need concurrency (two dispatchers racing for
one worker) uses threads against the same broker, which is the shape that
actually happens.

Run: tests/test_affiliation.py
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccd_broker.broker import VERSION, Broker  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
        FAILURES.append(name)


def call(b: Broker, method: str, **args) -> dict:
    return b.dispatch({"method": method, "args": args}) or {}


def owner_of(b: Broker, handle: str):
    return b.snapshot()["roster"][handle].get("owner")


def fresh(*workers: str) -> Broker:
    b = Broker()
    for w in workers:
        call(b, "announce", handle=w, slot="sonnet", effort="medium")
    return b


def main() -> int:
    print("announce leaves a worker unowned:")
    b = fresh("w1")
    check("announced", "w1" in b.snapshot()["roster"])
    check("owner is unset", owner_of(b, "w1") is None, repr(owner_of(b, "w1")))

    print("\nclaiming is exclusive:")
    b = fresh("w1")
    check("first claim succeeds", call(b, "claim", handle="w1", owner="dispA").get("ok"))
    check("owner recorded", owner_of(b, "w1") == "dispA")
    r = call(b, "claim", handle="w1", owner="dispB")
    check("second claim refused", not r.get("ok"), repr(r))
    check("owner unchanged", owner_of(b, "w1") == "dispA")
    check("re-claim by the same owner is idempotent",
          call(b, "claim", handle="w1", owner="dispA").get("ok"))
    check("force takes it over",
          call(b, "claim", handle="w1", owner="dispB", force=True).get("ok"))
    check("owner is now the forcer", owner_of(b, "w1") == "dispB")
    check("cannot claim an unannounced handle",
          not call(b, "claim", handle="ghost", owner="dispA").get("ok"))
    check("cannot claim itself",
          not call(b, "claim", handle="w1", owner="w1").get("ok"))

    print("\ntwo dispatchers racing for one worker:")
    # The case the lock exists for: both must not believe they won.
    for attempt in range(50):
        b = fresh("w1")
        results: list[dict] = []
        barrier = threading.Barrier(2)

        def race(who: str) -> None:
            barrier.wait()
            results.append(call(b, "claim", handle="w1", owner=who))

        ts = [threading.Thread(target=race, args=(w,)) for w in ("dispA", "dispB")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        won = [r for r in results if r.get("ok")]
        if len(won) != 1:
            check(f"exactly one winner (attempt {attempt})", False, repr(results))
            break
        if owner_of(b, "w1") != won[0].get("owner"):
            check(f"roster agrees with the winner (attempt {attempt})", False)
            break
    else:
        check("exactly one winner over 50 races", True)
        check("roster always agrees with the winner", True)

    print("\nsend enforcement:")
    b = fresh("w1")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "announce", handle="w2", slot="sonnet", effort="medium")
    call(b, "claim", handle="w2", owner="dispB")     # makes dispB a dispatcher
    check("the owning dispatcher may send",
          call(b, "send", to="w1", msg="task", **{"from": "dispA"}).get("ok"))
    r = call(b, "send", to="w1", msg="task", **{"from": "dispB"})
    check("another dispatcher may not", not r.get("ok"), repr(r))
    check("a human (no -f) may always send",
          call(b, "send", to="w1", msg="poke", **{"from": "unknown"}).get("ok"))
    check("a sender claiming nothing may always send",
          call(b, "send", to="w1", msg="poke", **{"from": "some-worker"}).get("ok"))
    check("an unclaimed worker is open to anyone",
          call(b, "announce", handle="w3", slot="haiku", effort="low").get("ok")
          and call(b, "send", to="w3", msg="x", **{"from": "dispB"}).get("ok"))

    print("\nrelease and retire-cascade:")
    b = fresh("w1", "w2")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "claim", handle="w2", owner="dispA")
    check("release frees one", call(b, "release", handle="w1").get("ok")
          and owner_of(b, "w1") is None)
    check("the other is still held", owner_of(b, "w2") == "dispA")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "announce", handle="dispA", slot="opus", effort="high")
    call(b, "retire", handle="dispA")
    check("retiring a dispatcher frees both its workers",
          owner_of(b, "w1") is None and owner_of(b, "w2") is None)

    print("\nannounce no longer silently overwrites:")
    b = fresh("w1")
    call(b, "claim", handle="w1", owner="dispA")
    check("same model slot re-announce succeeds (Esc-interrupt recovery)",
          call(b, "announce", handle="w1", slot="sonnet", effort="medium").get("ok"))
    check("and preserves the claim", owner_of(b, "w1") == "dispA")
    r = call(b, "announce", handle="w1", slot="opus", effort="high")
    check("different model slot is refused", not r.get("ok"), repr(r))
    # A launcher reserving a name for a NEW session must be told no even when
    # the slot matches: two workers of the same slot on one issue/phase is
    # exactly what the ordinal suffix exists for, and the broker cannot tell
    # that from an Esc-interrupted worker reclaiming its own handle.
    r = call(b, "announce", handle="w1", slot="sonnet", effort="medium",
             exclusive=True)
    check("exclusive reservation refuses an identical model slot",
          not r.get("ok"), repr(r))
    check("exclusive on a free handle succeeds",
          call(b, "announce", handle="fresh", slot="sonnet", effort="medium",
               exclusive=True).get("ok"))
    check("plain re-announce is still idempotent after that",
          call(b, "announce", handle="fresh", slot="sonnet",
               effort="medium").get("ok"))
    check("force overrides",
          call(b, "announce", handle="w1", slot="opus", effort="high",
               force=True).get("ok"))

    print("\nroster and ping:")
    b = fresh("w1")
    call(b, "claim", handle="w1", owner="dispA")
    entry = (call(b, "roster").get("workers") or [{}])[0]
    check("roster reports the owner", entry.get("owner") == "dispA", repr(entry))
    ping = call(b, "ping")
    check("ping reports the version", ping.get("version") == VERSION, repr(ping))
    check("ping reports broker start time", isinstance(ping.get("started_at"), float))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("PASS — claim-based affiliation behaves as ADR-0007 specifies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
