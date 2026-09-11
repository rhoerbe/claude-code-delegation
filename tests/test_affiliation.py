"""Claim-based worker affiliation (ADR-0007, issue #7).

Exercises the broker directly — affiliation is broker logic and needs no
socket. The one scenario that does need concurrency (two dispatchers racing
for one worker) uses threads against the same broker, which is the shape that
actually happens.
"""
from __future__ import annotations

import threading

from ccd_broker.broker import VERSION, Broker


def owner_of(b: Broker, handle: str):
    return b.snapshot()["roster"][handle].get("owner")


def fresh(call, *workers: str) -> Broker:
    b = Broker()
    for w in workers:
        call(b, "announce", handle=w, effort="medium")
    return b


def test_announce_leaves_a_worker_unowned(call):
    b = fresh(call, "w1")
    assert "w1" in b.snapshot()["roster"]
    assert owner_of(b, "w1") is None


def test_first_claim_succeeds_and_is_recorded(call):
    b = fresh(call, "w1")
    assert call(b, "claim", handle="w1", owner="dispA").get("ok")
    assert owner_of(b, "w1") == "dispA"


def test_second_claim_is_refused(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    r = call(b, "claim", handle="w1", owner="dispB")
    assert not r.get("ok"), repr(r)
    assert owner_of(b, "w1") == "dispA"


def test_reclaim_by_the_same_owner_is_idempotent(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    assert call(b, "claim", handle="w1", owner="dispA").get("ok")


def test_force_takes_over_the_claim(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    assert call(b, "claim", handle="w1", owner="dispB", force=True).get("ok")
    assert owner_of(b, "w1") == "dispB"


def test_cannot_claim_an_unannounced_handle(call):
    b = fresh(call, "w1")
    assert not call(b, "claim", handle="ghost", owner="dispA").get("ok")


def test_cannot_claim_itself(call):
    b = fresh(call, "w1")
    assert not call(b, "claim", handle="w1", owner="w1").get("ok")


def test_two_dispatchers_racing_for_one_worker(call):
    # The case the lock exists for: both must not believe they won, over
    # enough attempts that a race would show up.
    for attempt in range(50):
        b = fresh(call, "w1")
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
        assert len(won) == 1, f"attempt {attempt}: {results!r}"
        assert owner_of(b, "w1") == won[0].get("owner"), f"attempt {attempt}"


def test_the_owning_dispatcher_may_send(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    assert call(b, "send", to="w1", msg="task", **{"from": "dispA"}).get("ok")


def _fresh_with_dispB_owning_w2(call):
    """w1 owned by dispA, w2 owned by dispB — dispB must itself own a worker
    to count as a dispatcher for send enforcement, per the broker's rule."""
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "announce", handle="w2", effort="medium")
    call(b, "claim", handle="w2", owner="dispB")
    return b


def test_another_dispatcher_may_not_send(call):
    b = _fresh_with_dispB_owning_w2(call)
    r = call(b, "send", to="w1", msg="task", **{"from": "dispB"})
    assert not r.get("ok"), repr(r)


def test_a_human_no_dash_f_may_always_send(call):
    b = _fresh_with_dispB_owning_w2(call)
    assert call(b, "send", to="w1", msg="poke", **{"from": "unknown"}).get("ok")


def test_a_sender_claiming_nothing_may_always_send(call):
    b = _fresh_with_dispB_owning_w2(call)
    assert call(b, "send", to="w1", msg="poke", **{"from": "some-worker"}).get("ok")


def test_an_unclaimed_worker_is_open_to_anyone(call):
    b = _fresh_with_dispB_owning_w2(call)
    assert call(b, "announce", handle="w3", effort="low").get("ok")
    assert call(b, "send", to="w3", msg="x", **{"from": "dispB"}).get("ok")


def test_release_frees_one_worker_and_leaves_the_other_held(call):
    b = fresh(call, "w1", "w2")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "claim", handle="w2", owner="dispA")
    assert call(b, "release", handle="w1").get("ok")
    assert owner_of(b, "w1") is None
    assert owner_of(b, "w2") == "dispA"


def test_retiring_a_dispatcher_frees_both_its_workers(call):
    b = fresh(call, "w1", "w2")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "claim", handle="w2", owner="dispA")
    call(b, "release", handle="w1")
    call(b, "claim", handle="w1", owner="dispA")
    call(b, "announce", handle="dispA", effort="high")
    call(b, "retire", handle="dispA")
    assert owner_of(b, "w1") is None
    assert owner_of(b, "w2") is None


def test_same_effort_reannounce_succeeds_and_preserves_the_claim(call):
    # Esc-interrupt recovery: a worker reclaiming its own handle.
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    assert call(b, "announce", handle="w1", effort="medium").get("ok")
    assert owner_of(b, "w1") == "dispA"


def test_different_effort_reannounce_is_refused(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    r = call(b, "announce", handle="w1", effort="high")
    assert not r.get("ok"), repr(r)


def test_exclusive_reservation_refuses_an_identical_effort(call):
    # A launcher reserving a name for a NEW session must be told no even when
    # the effort matches: two workers of the same effort on one issue/phase
    # is exactly what the ordinal suffix exists for, and the broker cannot
    # tell that from an Esc-interrupted worker reclaiming its own handle.
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    r = call(b, "announce", handle="w1", effort="medium", exclusive=True)
    assert not r.get("ok"), repr(r)


def test_exclusive_on_a_free_handle_succeeds_and_stays_idempotent(call):
    b = fresh(call, "w1")
    assert call(b, "announce", handle="fresh", effort="medium",
                exclusive=True).get("ok")
    assert call(b, "announce", handle="fresh", effort="medium").get("ok")


def test_force_overrides_a_different_effort(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    assert call(b, "announce", handle="w1", effort="high", force=True).get("ok")


def test_roster_reports_the_owner(call):
    b = fresh(call, "w1")
    call(b, "claim", handle="w1", owner="dispA")
    entry = (call(b, "roster").get("workers") or [{}])[0]
    assert entry.get("owner") == "dispA", repr(entry)


def test_ping_reports_version_and_start_time(call):
    b = fresh(call, "w1")
    ping = call(b, "ping")
    assert ping.get("version") == VERSION, repr(ping)
    assert isinstance(ping.get("started_at"), float)
