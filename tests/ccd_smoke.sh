#!/usr/bin/env bash
# tests/ccd_smoke.sh — smoke test for ccd_broker + the ccd CLI (PLAN-ccd-v2.md
# §5.4). Generic: no host specifics, no personal handles. Exercises the real
# broker (ccd_broker/) and the real CLI (ccd) end to end using throwaway
# handles t-disp/t-work.
#
# Runs its own PRIVATE broker instance (its own $CCD_SOCKET/$CCD_PIDFILE in a
# throwaway tmpdir) so it never touches a broker the user may already have
# running, and so re-running this script twice in a row is safe (idempotent):
# the trap always stops the broker it started and removes its tmpdir, even on
# failure or Ctrl-C.
#
# Exit code: 0 iff every step below passed. Non-zero (and a FAIL line for the
# offending step) otherwise.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CCD="$REPO_ROOT/ccd"

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/ccd-smoke.XXXXXX")"
export CCD_SOCKET="$TMPDIR/ccd-smoke.sock"
export CCD_PIDFILE="$TMPDIR/ccd-smoke.pid"
RECV_OUT="$TMPDIR/recv.out"

RECV_PID=""
PASS=0
FAIL=0

cleanup() {
  # Kill any still-parked backgrounded recv from step 2-4.
  if [ -n "$RECV_PID" ] && kill -0 "$RECV_PID" 2>/dev/null; then
    kill -TERM "$RECV_PID" 2>/dev/null
    wait "$RECV_PID" 2>/dev/null
  fi
  # Idempotent: fine to call even if the broker already stopped, or never
  # started (e.g. step 1 itself failed).
  "$CCD" broker stop >/dev/null 2>&1
  rm -rf "$TMPDIR"
}
trap cleanup EXIT INT TERM

ok() {
  echo "PASS: $1"
  PASS=$((PASS + 1))
}

fail() {
  echo "FAIL: $1" >&2
  FAIL=$((FAIL + 1))
}

# ---------------------------------------------------------------------------
# Step 1: ccd broker start; ccd ping -> ok
# ---------------------------------------------------------------------------
echo "=== Step 1: ccd broker start; ccd ping ==="
start_out="$("$CCD" broker start 2>&1)"; start_rc=$?
echo "$start_out"
if [ "$start_rc" -ne 0 ]; then
  fail "ccd broker start exited $start_rc"
else
  ok "ccd broker start exited 0"
fi

ping_out="$("$CCD" ping 2>&1)"; ping_rc=$?
echo "$ping_out"
if [ "$ping_rc" -eq 0 ] && printf '%s' "$ping_out" | grep -q '^ok'; then
  ok "ccd ping reports ok ($ping_out)"
else
  fail "ccd ping did not report ok (rc=$ping_rc): $ping_out"
fi

# ---------------------------------------------------------------------------
# Steps 2-4: background a parked recv on t-work, send to it from another
# process, confirm it unblocks with the right message.
# ---------------------------------------------------------------------------
echo
echo "=== Step 2: background 'ccd recv t-work -t 10' (simulated parked worker) ==="
"$CCD" recv t-work -t 10 >"$RECV_OUT" 2>&1 &
RECV_PID=$!
sleep 1
if kill -0 "$RECV_PID" 2>/dev/null; then
  ok "backgrounded recv is parked (pid $RECV_PID, still running after 1s)"
else
  fail "backgrounded recv exited before a message was sent to it"
fi

echo
echo "=== Step 3: ccd send t-work \"hello\" -f t-disp (from another process) ==="
send_out="$("$CCD" send t-work "hello" -f t-disp 2>&1)"; send_rc=$?
echo "$send_out"
if [ "$send_rc" -eq 0 ] && printf '%s' "$send_out" | grep -q '^sent'; then
  ok "ccd send t-work succeeded ($send_out)"
else
  fail "ccd send t-work failed (rc=$send_rc): $send_out"
fi

echo
echo "=== Step 4: backgrounded recv unblocks and prints the message ==="
waited=0
while kill -0 "$RECV_PID" 2>/dev/null && [ "$waited" -lt 50 ]; do
  sleep 0.1
  waited=$((waited + 1))
done
if kill -0 "$RECV_PID" 2>/dev/null; then
  fail "backgrounded recv did not unblock within 5s of the send"
  kill -TERM "$RECV_PID" 2>/dev/null
  wait "$RECV_PID" 2>/dev/null
else
  wait "$RECV_PID" 2>/dev/null
  recv_result="$(cat "$RECV_OUT")"
  echo "recv output: $recv_result"
  if printf '%s' "$recv_result" | grep -q '^t-disp: hello$'; then
    ok "backgrounded recv unblocked with 't-disp: hello'"
  else
    fail "backgrounded recv printed unexpected output: $recv_result"
  fi
fi
RECV_PID=""

# ---------------------------------------------------------------------------
# Step 5: announce t-work, confirm ls includes it.
# ---------------------------------------------------------------------------
echo
echo "=== Step 5: ccd announce t-work dummy-model low; ccd ls includes t-work ==="
announce_out="$("$CCD" announce t-work dummy-model low 2>&1)"; announce_rc=$?
echo "$announce_out"
if [ "$announce_rc" -eq 0 ]; then
  ok "ccd announce t-work succeeded"
else
  fail "ccd announce t-work failed (rc=$announce_rc): $announce_out"
fi

ls_out="$("$CCD" ls 2>&1)"
echo "$ls_out"
if printf '%s\n' "$ls_out" | grep -q '^t-work'; then
  ok "ccd ls includes t-work after announce"
else
  fail "ccd ls did not include t-work after announce: $ls_out"
fi

# ---------------------------------------------------------------------------
# Step 6: retire t-work, confirm ls excludes it.
# ---------------------------------------------------------------------------
echo
echo "=== Step 6: ccd ret t-work; ccd ls excludes t-work ==="
ret_out="$("$CCD" ret t-work 2>&1)"; ret_rc=$?
echo "$ret_out"
if [ "$ret_rc" -eq 0 ]; then
  ok "ccd ret t-work succeeded"
else
  fail "ccd ret t-work failed (rc=$ret_rc): $ret_out"
fi

ls_out2="$("$CCD" ls 2>&1)"
echo "$ls_out2"
if printf '%s\n' "$ls_out2" | grep -q '^t-work'; then
  fail "ccd ls still includes t-work after ret: $ls_out2"
else
  ok "ccd ls excludes t-work after ret"
fi

# ---------------------------------------------------------------------------
# Step 7: token-spend verification (PLAN §5.4/§7) is explicitly MANUAL and
# out of scope here — it needs a real Claude worker session and the hosting
# repo's cc_token_usage.py, and is blocked on a hosting-repo deploy. Not
# scripted; see PLAN-ccd-v2.md §7 for the manual procedure.
# ---------------------------------------------------------------------------
echo
echo "=== Step 7: token-spend verification — MANUAL, not scripted (see PLAN-ccd-v2.md §7) ==="

# ---------------------------------------------------------------------------
# Step 8: ccd broker stop.
# ---------------------------------------------------------------------------
echo
echo "=== Step 8: ccd broker stop ==="
stop_out="$("$CCD" broker stop 2>&1)"; stop_rc=$?
echo "$stop_out"
if [ "$stop_rc" -eq 0 ]; then
  ok "ccd broker stop succeeded"
else
  fail "ccd broker stop failed (rc=$stop_rc): $stop_out"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "==================================="
echo "ccd_smoke: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "ccd_smoke: FAIL"
  exit 1
fi
echo "ccd_smoke: PASS"
exit 0
