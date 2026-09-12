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
# normalised to <PID>, <TMP>, <TS>, <UP>, <REPO>, <VERSION> so two runs of the
# same
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
# `ccd pick`/`ccd launch` read this before anything else. Point it at a path
# that never exists so every pick/launch case here hits the same
# no-manifest error regardless of whatever this machine's real
# ~/.config/ccd/mappings.json does or doesn't contain (hosting#131 phase 4)
# — this instrument must never depend on, or touch, that file. The
# manifest-present cases further below override this per-case rather than
# changing the default, so every OTHER case keeps proving the CLI works with
# no manifest at all.
export CCD_MAPPINGS="$WORK/mappings.json"

# The committed fixture manifest (tests/fixtures/sample-mappings.json) and a
# stub launcher standing in for `claude`/`claude-openrouter` (hosting#131
# phase 4 follow-up: the billing suffix) — never a real launcher, never
# ~/.config/ccd/mappings.json. Copied under both launcher names the fixture
# manifest's entries use, onto a $PATH prefix used only by the cases below
# that explicitly opt into it.
SAMPLE_MANIFEST="$SCRIPT_DIR/fixtures/sample-mappings.json"
STUB_BIN="$WORK/bin"
mkdir -p "$STUB_BIN"
cp "$SCRIPT_DIR/fixtures/stub-launcher" "$STUB_BIN/stub-claude"
cp "$SCRIPT_DIR/fixtures/stub-launcher" "$STUB_BIN/stub-claude-openrouter"
chmod +x "$STUB_BIN/stub-claude" "$STUB_BIN/stub-claude-openrouter"
STUB_PATH="$STUB_BIN:$PATH"

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
      -e "s/ccd-broker [0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*/ccd-broker <VERSION>/g" \
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
  # stdin is always /dev/null, never this script's own: `ccd pick`/`ccd
  # launch` (hosting#131 phase 4) read stdin when it IS a terminal, and this
  # script's own stdin is a terminal whenever someone runs it by hand — which
  # would otherwise block the recording on a read nothing ever answers. Every
  # other subcommand here already ignores stdin, so this changes nothing for
  # them and removes a real hang for these two.
  out="$(env "${envs[@]+"${envs[@]}"}" "$CCD" "$@" </dev/null 2>"$WORK/.err")"; rc=$?
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
# pick/launch read the manifest before touching the broker at all, so these
# fail the same way whether the broker is up or down — recorded here rather
# than duplicated in both sections. The interactive choice/field prompts are
# deliberately NOT characterised here: they refuse outright whenever stdin is
# not a terminal (which this script's own stdin never is), and that refusal
# path plus the successful-choice path are unit-tested directly against a
# faked stdin in tests/test_pick_launch.py instead.
record_case pick-usage -- pick extra-argument
record_case pick-no-manifest -- pick
record_case launch-no-manifest -- launch
record_case launch-unknown-arg -- launch --wat
record_case launch-issue-no-value -- launch some-id --issue

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

# Handle identity, broker 1.4.0. Placed after the roster is empty so these
# leave the earlier goldens alone, and on their own handles for the same
# reason.
#
# These exist because of a trap this harness set for itself. It exports
# CLAUDE_PID="$$" so a recording session's own pid cannot leak into a golden —
# which means every announce above shares one pid, so the broker reads them all
# as the same process re-announcing (rule a) and the metadata comparison is
# never reached. A fixture that makes the subject deterministic made it
# unrepresentative. The cases below vary the pid on purpose.
echo "=== handle identity (broker 1.4.0) ==="
record_case identity-announce -- announce w-id a-model low
# Same pid, different effort: allowed. The Esc-interrupted worker reclaiming
# its own handle.
record_case identity-same-pid-new-effort -- announce w-id a-model high
# A different LIVE pid on a live handle: refused as a hijack, even though the
# effort now matches what is recorded.
record_case identity-hijack CLAUDE_PID=1 -- announce w-id a-model high
record_case identity-hijack-force CLAUDE_PID=1 -- announce w-id a-model high --force
# Neither side has a pid: falls back to the declared metadata, exactly as
# before 1.4.0. This is the path the harness could no longer reach on its own.
record_case identity-nopid-announce CLAUDE_PID= -- announce w-id2 a-model low
record_case identity-nopid-same-effort CLAUDE_PID= -- announce w-id2 a-model low
record_case identity-nopid-new-effort CLAUDE_PID= -- announce w-id2 a-model high
record_case identity-nopid-force CLAUDE_PID= -- announce w-id2 a-model high --force
record_case identity-ret -- ret w-id
record_case identity-ret2 -- ret w-id2


# Launch against a real manifest (hosting#131 phase 4 follow-up: the billing
# suffix, ADR-0002 revised e782da4). Placed after the roster is empty and on
# their own handles, same reason as the identity cases above — these use
# CCD_MAPPINGS/PATH overrides rather than the harness defaults, so only these
# cases ever see a manifest or a launcher.
echo "=== launch (real manifest, stub launcher) ==="
record_case pick-real-manifest CCD_MAPPINGS="$SAMPLE_MANIFEST" -- pick
record_case launch-with-billing \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" PATH="$STUB_PATH" \
  -- launch kimi-k3-1m-max --issue 119 --phase 3
record_case launch-without-billing \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" PATH="$STUB_PATH" \
  -- launch claude-sonnet-5-medium --issue 119 --phase 3
record_case launch-explicit-handle-ignores-billing \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" PATH="$STUB_PATH" \
  -- launch kimi-k3-1m-max --handle exact-name
record_case launch-unknown-mapping \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" \
  -- launch not-a-real-id --issue 1 --phase 1
record_case launch-ls-after CCD_MAPPINGS="$SAMPLE_MANIFEST" -- ls
record_case launch-ret-billed -- ret "119-3-kimi-k3-1m-max-api"
record_case launch-ret-unbilled -- ret "119-3-claude-sonnet-5-medium"
record_case launch-ret-explicit -- ret exact-name
record_case launch-ls-clean -- ls

# `ccd announce` resolving model and effort from $CCD_MAPPING. The bug behind
# these: `ccd launch` removes CCD_MODEL/CCD_EFFORT from the environment it
# hands on, while the worker skill still announced with them — so a launched
# session announced two empty strings and landed on the roster with no effort
# at all. Same convention as the launch cases above: only these pass
# CCD_MAPPINGS, since the harness default deliberately points at no manifest.
echo "=== announce from a mapping ==="
record_case announce-from-mapping \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" CCD_HANDLE=w-map CCD_MAPPING=kimi-k3-1m-max \
  -- announce
# An entry with no effort announces without one, and the confirmation drops
# the trailing slash rather than printing "(name/)".
record_case announce-from-mapping-no-effort \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" CCD_HANDLE=w-map2 CCD_MAPPING=claude-haiku-4-5 \
  -- announce
# A handle positional is still allowed; the rest comes from the mapping.
record_case announce-mapping-handle-only \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" CCD_MAPPING=claude-sonnet-5-medium \
  -- announce w-map3
# Typed values win: an operator who types them means them.
record_case announce-mapping-positionals-win \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" CCD_HANDLE=w-map4 CCD_MAPPING=kimi-k3-1m-max \
  -- announce typed-model low
# Failing loudly beats announcing empty strings, which was the bug.
record_case announce-mapping-unknown-id \
  CCD_MAPPINGS="$SAMPLE_MANIFEST" CCD_HANDLE=w-map5 CCD_MAPPING=no-such-id \
  -- announce
# $CCD_MAPPING set but no manifest to resolve it against.
record_case announce-mapping-no-manifest \
  CCD_HANDLE=w-map6 CCD_MAPPING=kimi-k3-1m-max -- announce
record_case announce-mapping-ls -- ls
record_case announce-mapping-ret -- ret w-map
record_case announce-mapping-ret2 -- ret w-map2
record_case announce-mapping-ret3 -- ret w-map3
record_case announce-mapping-ret4 -- ret w-map4

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
# The version is normalised above, so the recordings no longer freeze it — a
# release bump used to turn this suite red until someone re-recorded ping-up,
# which is churn that teaches people to re-record rather than to look. But
# normalising a value means nothing checks it, and `ccd ping`'s whole job is to
# report the version, so it is asserted here directly and against the SOURCE
# rather than against a frozen copy of it: that is strictly stronger than the
# recording was, because a wrong version now fails even if someone re-recorded
# it to match.
# Starts its own broker: the recorded cases above deliberately end with the
# broker stopped, so a ping here would report "down" and the check would fail
# for a reason that has nothing to do with the version.
_tree_version="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$REPO_ROOT/ccd_broker/broker.py")"
"$CCD" broker start >/dev/null 2>&1 || true
_reported="$("$CCD" ping 2>&1 || true)"
"$CCD" broker stop >/dev/null 2>&1 || true
if [ -z "$_tree_version" ]; then
  echo "  FAIL      ping-reports-tree-version (no VERSION in ccd_broker/broker.py)" >&2
  FAIL=$((FAIL + 1)); FAILED_CASES+=("ping-reports-tree-version")
elif printf '%s' "$_reported" | grep -q "ccd-broker $_tree_version"; then
  echo "  ok        ping-reports-tree-version ($_tree_version)"
  PASS=$((PASS + 1))
else
  echo "  FAIL      ping-reports-tree-version: tree says $_tree_version, ccd ping said: $_reported" >&2
  FAIL=$((FAIL + 1)); FAILED_CASES+=("ping-reports-tree-version")
fi

if [ "$FAIL" -ne 0 ]; then
  echo "characterize_cli: $PASS passed, $FAIL FAILED — ${FAILED_CASES[*]}" >&2
  exit 1
fi

echo "characterize_cli: $PASS passed, 0 failed"
