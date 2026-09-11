#!/usr/bin/env bash
# tests/characterize_cli.sh — the instrument behind "the CLI surface is frozen".
#
# Runs every subcommand against a throwaway broker on a temp socket and records
# exit code, stdout and stderr verbatim into tests/golden/. `ccd_smoke.sh`
# proves the happy path works; this proves the *wording* and the *exit codes*
# did not move, which is the part a rewrite silently breaks.
#
#   tests/characterize_cli.sh --record   # write tests/golden/*.txt
#   tests/characterize_cli.sh            # compare against them; non-zero on drift
#
# Volatile values (pids, temp paths, timestamps, uptimes, the repo path) are
# normalised to <PID>, <TMP>, <TS>, <UP>, <REPO> so two runs of the same
# implementation agree. Everything else is compared byte for byte.
#
# This file is deliberately NOT named tests/test_*.py: those are a parallel
# track's territory.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# The dashboard's `tree` column renders the checkout's directory NAME, not its
# path, so the $REPO_ROOT rule below never reaches it and a golden recorded in
# one checkout failed in another whose directory happened to be named
# differently. Matched inside the table cell rather than bare: a checkout named
# something short and common would otherwise rewrite unrelated text.
REPO_NAME="$(basename "$REPO_ROOT")"
CCD="$REPO_ROOT/ccd"
GOLDEN_DIR="$SCRIPT_DIR/golden"

MODE="compare"
[ "${1:-}" = "--record" ] && MODE="record"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ccd-char.XXXXXX")"
export CCD_SOCKET="$WORK/ccd.sock"
export CCD_PIDFILE="$WORK/ccd.pid"
unset CCD_HANDLE 2>/dev/null || true
# The dashboard reads transcripts from here; point it at an empty directory so
# a golden never depends on the machine's real sessions.
export CCD_TRANSCRIPT_ROOT="$WORK/transcripts"
mkdir -p "$CCD_TRANSCRIPT_ROOT"
# `ccd announce` forwards these from whatever session runs it. Left alone, the
# recording session's own pid and transcript id would be baked into the
# goldens, so pin them: this shell's pid is certainly alive, which is what the
# roster's `up` column needs, and no session id keeps transcript lookup empty.
export CLAUDE_PID="$$"
export CLAUDE_CODE_SESSION_ID=""

PASS=0
FAIL=0
FAILED_CASES=()

cleanup() {
  "$CCD" broker stop >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

# Collapse everything that legitimately differs between two identical runs.
normalise() {
  sed -e "s#$WORK#<TMP>#g" \
      -e "s#$REPO_ROOT#<REPO>#g" \
      -e "s#| $REPO_NAME |#| <REPO> |#g" \
      -e "s#$HOME#<HOME>#g" \
      -e "s/pid [0-9][0-9]*/pid <PID>/g" \
      -e "s/\t[0-9][0-9]*\t/\t<PID>\t/g" \
      -e "s/up [0-9][0-9]*h\{0,1\}[0-9][0-9]*m/up <UP>/g" \
      -e "s/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z/<TS>/g" \
      -e "s/^\([0-9][0-9]*\)$/<NUM>/g"
}

# record_case <name> [env=value ...] -- <argv...>
record_case() {
  local name="$1"; shift
  local -a envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift

  local out err rc
  out="$(env "${envs[@]+"${envs[@]}"}" "$CCD" "$@" 2>"$WORK/.err")"; rc=$?
  err="$(cat "$WORK/.err")"

  local rendered
  rendered="$(printf 'argv: ccd %s\nexit: %s\n--- stdout ---\n%s\n--- stderr ---\n%s\n' \
    "$*" "$rc" "$out" "$err" | normalise)"

  local golden="$GOLDEN_DIR/$name.txt"
  if [ "$MODE" = "record" ]; then
    mkdir -p "$GOLDEN_DIR"
    printf '%s\n' "$rendered" > "$golden"
    echo "  recorded  $name (exit $rc)"
    return 0
  fi

  if [ ! -f "$golden" ]; then
    echo "  FAIL      $name — no golden at ${golden#$REPO_ROOT/}" >&2
    FAIL=$((FAIL + 1)); FAILED_CASES+=("$name")
    return 0
  fi
  if printf '%s\n' "$rendered" | diff -u "$golden" - > "$WORK/.diff"; then
    echo "  ok        $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL      $name" >&2
    sed 's/^/            /' "$WORK/.diff" >&2
    FAIL=$((FAIL + 1)); FAILED_CASES+=("$name")
  fi
}

echo "=== broker down: usage, argument handling, unreachable ==="
record_case no-subcommand --
record_case help-flag -- --help
record_case help-word -- help
record_case unknown-subcommand -- frobnicate
record_case ping-down -- ping
record_case broker-status-down -- broker status
record_case broker-stop-not-running -- broker stop
record_case broker-unknown-sub -- broker wat
record_case send-usage -- send
record_case send-one-arg -- send only
record_case send-unknown-arg -- send a b --wat
record_case send-unreachable -- send a b -f c
# The bash implementation looped forever on a flag whose value is missing
# (`shift 2` with one argument left fails quietly under `set -u`), so these two
# have no golden from it — an infinite loop is not a surface worth freezing.
# They are a usage error here, and this is the deliberate deviation.
record_case send-dash-f-no-value -- send a b -f
record_case recv-dash-t-no-value -- recv w1 -t
record_case recv-no-handle -- recv
record_case recv-unknown-flag -- recv -x
record_case recv-two-positionals -- recv a b
record_case announce-usage -- announce
record_case announce-no-handle -- announce a-model low
record_case ret-too-many -- ret a b
record_case ret-no-handle -- ret
record_case claim-usage -- claim
record_case claim-no-dispatcher -- claim w1
record_case release-usage -- release
record_case ls-unreachable -- ls
record_case dashboard-help -- dashboard --help
record_case dashboard-unknown-arg -- dashboard --wat
record_case dashboard-missing-value -- dashboard --scope
record_case dashboard-unreachable -- dashboard

echo "=== broker up ==="
record_case broker-start -- broker start
record_case ping-up -- ping
record_case broker-status-up -- broker status
record_case broker-start-again -- broker start

echo "=== roster ==="
record_case announce-ok -- announce w1 a-model low
record_case announce-same-again -- announce w1 a-model low
record_case announce-different-effort -- announce w1 a-model high
record_case announce-exclusive-taken -- announce w1 a-model low --exclusive
record_case announce-env-handle CCD_HANDLE=disp -- announce a-model high
record_case announce-no-pid CLAUDE_PID= -- announce w-nopid a-model low
record_case ls-populated -- ls
record_case claim-ok -- claim w1 disp
record_case claim-other-dispatcher -- claim w1 someone-else
record_case claim-force -- claim w1 someone-else --force
record_case release-ok CCD_HANDLE=someone-else -- release w1
record_case release-unowned CCD_HANDLE=someone-else -- release w1

echo "=== messages ==="
record_case send-ok -- send w1 hello -f disp
record_case recv-ok -- recv w1 -t 5
record_case recv-timeout -- recv w1 -t 1
record_case recv-env-handle CCD_HANDLE=w1 -- recv -t 1

echo "=== dashboard ==="
record_case dashboard-default -- dashboard
record_case dashboard-scope -- dashboard --scope w1
record_case dashboard-scope-unknown -- dashboard --scope nobody
record_case dashboard-json -- dashboard --json
record_case dashboard-write-in-tree -- dashboard --write "$REPO_ROOT/fleet.json"
record_case dashboard-write-ok -- dashboard --write "$WORK/fleet.json"

echo "=== teardown ==="
record_case ret-ok -- ret w1
record_case ret-nopid -- ret w-nopid
record_case ret-unknown -- ret nobody
record_case ls-after-retire -- ls
record_case broker-stop -- broker stop
record_case broker-stop-again -- broker stop

echo
if [ "$MODE" = "record" ]; then
  echo "characterize_cli: recorded $(ls -1 "$GOLDEN_DIR" | wc -l) goldens into ${GOLDEN_DIR#$REPO_ROOT/}"
  exit 0
fi
if [ "$FAIL" -ne 0 ]; then
  echo "characterize_cli: $PASS passed, $FAIL FAILED — ${FAILED_CASES[*]}" >&2
  exit 1
fi
echo "characterize_cli: $PASS passed, 0 failed"
