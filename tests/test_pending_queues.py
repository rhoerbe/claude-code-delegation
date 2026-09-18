"""Queued is not delivered, and some queues have nowhere to go (#39).

`ccd send` answering `sent (id=m2)` means this broker accepted the message. It
has never meant anything received it, and until now nothing could tell the two
apart from outside. In the run that prompted #39 a dispatcher sent two tasks, a
deploy restarted the broker underneath them, and the dispatcher went on
believing work was in flight while the worker sat idle -- discovered only
because a human relayed a message by hand.

Two counts, both read-only views of state the broker already held:

* `pending` per roster entry -- what this handle has not collected;
* `orphans` -- queues holding messages for a handle the roster does not have,
  which is the expensive half. Every roster-keyed view misses them by
  construction, because the missing handle is exactly the key.
"""
from __future__ import annotations

import subprocess

import pytest


def _roster(broker, call) -> dict:
    return call(broker, "roster")


def test_a_parked_worker_with_nothing_queued_reports_zero(broker, call):
    call(broker, "announce", handle="w1", effort="low")
    r = _roster(broker, call)
    assert r["workers"][0]["pending"] == 0


def test_an_accepted_message_shows_as_pending_until_collected(broker, call):
    """The distinction #39 needed: accepted, not delivered."""
    call(broker, "announce", handle="w1", effort="low")
    assert call(broker, "send", to="w1", msg="task", **{"from": "disp"})["ok"]
    assert _roster(broker, call)["workers"][0]["pending"] == 1

    call(broker, "recv", handle="w1", timeout=1)
    assert _roster(broker, call)["workers"][0]["pending"] == 0


def test_pending_counts_every_waiting_message(broker, call):
    call(broker, "announce", handle="w1", effort="low")
    for i in range(3):
        call(broker, "send", to="w1", msg=f"task {i}", **{"from": "disp"})
    assert _roster(broker, call)["workers"][0]["pending"] == 3


# ----------------------------------------------------------------------
# orphans: a queue with no session behind it
# ----------------------------------------------------------------------

def test_a_send_to_a_handle_that_never_announced_is_an_orphan(broker, call):
    """Accepted, queued, and collected by nobody. The broker creates the queue
    on send (deliberately -- sending to a worker before it announces is
    normal), so acceptance says nothing about whether anyone is there."""
    assert call(broker, "send", to="ghost", msg="hello?", **{"from": "disp"})["ok"]
    assert _roster(broker, call)["orphans"] == {"ghost": 1}


def test_a_handle_that_announces_later_stops_being_an_orphan(broker, call):
    """Why this is a warning and not an error: the pre-announce send is a
    supported pattern, so an orphan is 'nobody is there YET'."""
    call(broker, "send", to="w1", msg="early", **{"from": "disp"})
    assert _roster(broker, call)["orphans"] == {"w1": 1}

    call(broker, "announce", handle="w1", effort="low")
    r = _roster(broker, call)
    assert r["orphans"] == {}
    assert r["workers"][0]["pending"] == 1


def test_retiring_with_messages_still_queued_leaves_an_orphan(broker, call):
    """The shape that actually loses work: a session leaves, its queue does
    not, and the roster no longer mentions it."""
    call(broker, "announce", handle="w1", effort="low")
    call(broker, "send", to="w1", msg="unread", **{"from": "disp"})
    call(broker, "retire", handle="w1")

    r = _roster(broker, call)
    assert r["workers"] == []
    assert r["orphans"] == {"w1": 1}


def test_an_empty_queue_is_not_an_orphan(broker, call):
    """A drained queue leaves an empty deque behind; reporting it would make
    every handle ever sent to look like a problem forever."""
    call(broker, "send", to="w1", msg="x", **{"from": "disp"})
    call(broker, "announce", handle="w1", effort="low")
    call(broker, "recv", handle="w1", timeout=1)
    call(broker, "retire", handle="w1")
    assert _roster(broker, call)["orphans"] == {}


def test_orphans_is_always_present(broker, call):
    """Always a dict, never absent, so a consumer can tell "none" from "this
    broker is too old to know" -- the same rule `mapping` follows."""
    assert _roster(broker, call)["orphans"] == {}


def test_a_reaped_dead_session_leaves_its_queue_as_an_orphan(broker, call):
    """Reaping removes the roster entry, not the queue. Without `orphans` those
    messages would become invisible at the same moment they became
    undeliverable."""
    proc = subprocess.Popen(["sleep", "100"])   # real, then immediately dead
    proc.terminate()
    proc.wait()
    call(broker, "announce", handle="w1", effort="low", pid=proc.pid)
    call(broker, "send", to="w1", msg="stranded", **{"from": "disp"})
    _roster(broker, call)          # the read that discovers it dead
    r = _roster(broker, call)      # and the one after the reap
    assert r["workers"] == []
    assert r["orphans"] == {"w1": 1}
