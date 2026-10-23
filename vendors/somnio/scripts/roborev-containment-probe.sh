#!/bin/sh
# roborev containment probe — ADR-0005 D-1/D-3, issue #45's day-one probe, #52.
#
# THE QUESTION. ADR-0005 D-1 scopes roborev to a per-repository opt-in, but
# `roborev agent-hook install` registers hooks in USER-scope ~/.claude/, which
# every Claude Code session on the machine loads. #52 asks whether the
# per-repository framing matches the hook's actual blast radius. Upstream
# supports containment four ways — the daemon reads repository registration from
# the review database (agent-hook.md:194), commit and failed-review accounting is
# "scoped to repository lineage" (:41), `commit_threshold` defaults to 0, and a
# queued reminder is rechecked and discarded when no failed reviews stand behind
# it (:219) — but NO SINGLE UPSTREAM SENTENCE STATES IT OUTRIGHT. Hence a probe.
#
# WHAT THIS SCRIPT IS FOR. #45 specifies the probe in prose across two repos.
# This makes it executable, and splits it into the half that needs nothing and
# the half that needs a registered repository:
#
#   PHASE A (default, needs nothing): in an UNREGISTERED repo, drive the hook
#     past its thresholds and assert it emits no reminder and blocks nothing.
#     Runs on any machine, registers nothing, sends no code anywhere.
#
#   PHASE B (--registered, pilot day one): the positive half. In a REGISTERED
#     repo with >= failed_review_threshold open failed reviews, assert the hook
#     DOES fire. Without this, phase A proves only that the hook was inert —
#     which is #45's own warning, quoted verbatim: "otherwise the probe proved
#     only that the hook was inert, nothing about scoping."
#
# ON FAILURE of phase A, #45 is explicit and this script repeats it: stop the
# pilot, register no second repository, and reopen ADR-0005 D-1 — per-repo opt-in
# would not be a real boundary and D-3's containment claim needs re-drafting.
#
# Read-only toward the review database: it never registers, never reviews, never
# deletes. It does increment this machine's agent-hook session counters, which
# `roborev agent-hook reset --all` clears; the script offers that at the end.
set -u

REGISTERED=0
[ "${1:-}" = "--registered" ] && REGISTERED=1

command -v roborev >/dev/null 2>&1 || { echo "roborev is not installed — nothing to probe" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required to read the hook's JSON" >&2; exit 2; }

REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
ko()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }

# emit <session> <event> [extra flags...] -> the hook's JSON on stdout
# The payload is built by json.dumps, not interpolated: a repo path containing a
# quote or a backslash produced malformed JSON, which roborev then rejected — and
# the run failed at the "well-formed payload" control with a misleading message
# about the parser rather than about the path.
#
# stdout and stderr are kept SEPARATE. Merging them meant a stderr warning
# printed alongside the JSON made the output unparseable, and an unparseable
# answer used to grade as "silent" — the probe passing for the wrong reason.
# Since 0.68.0 `agent-hook run` refuses a call without the profile and the
# ownership marker ("agent hook registration is outdated") before it reads the
# payload, so a bare call proves nothing. These are the exact arguments
# `agent-hook install --agent claude` registers in ~/.claude/settings.json.
HOOK_RUN_ARGS="--agent claude --source=roborev-agent-hook"

emit() {
  _sid="$1"; _evt="$2"; shift 2
  python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"transcript_path":"/dev/null","hook_event_name":sys.argv[2],"cwd":sys.argv[3],"stop_hook_active":False}))' \
    "$_sid" "$_evt" "$REPO" | roborev agent-hook run $HOOK_RUN_ARGS "$@" 2>/dev/null
}

# decision <json> -> "block" when the hook is asking the agent to do something
decision() {
  printf '%s' "$1" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("UNPARSEABLE"); raise SystemExit
if d.get("decision") or d.get("reason") or d.get("systemMessage") or d.get("additionalContext"):
    print("REMINDER")
else:
    print("silent")
' 2>/dev/null
}

echo "roborev containment probe — repo: $REPO"
echo "registered repositories in the review database:"
roborev repo list 2>&1 | sed 's/^/  /' | head -6
echo

# ---- control: the payload parser must be live -------------------------------
# Without this, "the hook emitted nothing" and "the hook never understood the
# payload" are indistinguishable, and the probe would pass for the wrong reason.
echo "controls (the probe is meaningless if the parser is not live):"
OUT="$(printf 'not json' | roborev agent-hook run $HOOK_RUN_ARGS 2>&1)"
case "$OUT" in *decode*|*invalid*) ok "malformed payload is rejected — the parser runs" ;;
  *) ko "malformed payload should error; got: $(printf %s "$OUT" | head -c 60)" ;; esac
OUT="$(printf '{}' | roborev agent-hook run $HOOK_RUN_ARGS 2>&1)"
# 0.67 named session_id; 0.68 normalizes first and names hook_event_name
# Matched on the FIELD name: a bare "missing" also matches a refusal made before
# the payload is read (a stale HOOK_RUN_ARGS), which would fake a live parser.
case "$OUT" in *hook_event_name*|*session_id*) ok "an empty payload is rejected for a missing field — required fields are validated" ;;
  *) ko "empty payload should name the missing field; got: $(printf %s "$OUT" | head -c 60)" ;; esac
OUT="$(emit control-valid Stop)"
case "$(decision "$OUT")" in UNPARSEABLE) ko "a well-formed payload produced unparseable output: $(printf %s "$OUT" | head -c 60)" ;;
  *) ok "a well-formed payload is accepted and answers with JSON" ;; esac
echo

if [ "$REGISTERED" -eq 0 ]; then
  # ---- PHASE A: the unregistered repo must stay silent ----------------------
  echo "phase A — unregistered repo (#45 steps 3-4, #52's open question):"
  SID="probe-$$"
  _fired=0; _broke=0
  i=1
  while [ "$i" -le 7 ]; do
    _out="$(emit "$SID" Stop)"; _rc=$?
    _d="$(decision "$_out")"
    # An unparseable answer or a non-zero exit is NOT silence. Treating it as
    # silence is how this probe would report "contained" while the hook was in
    # fact erroring on every event.
    [ "$_rc" -ne 0 ] && _broke=1
    [ "$_d" = "UNPARSEABLE" ] && _broke=1
    [ "$_d" = "REMINDER" ] && _fired=1
    i=$((i+1))
  done
  [ "$_broke" -eq 0 ] && ok "every Stop event answered with parseable JSON and exit 0" \
                      || ko "the hook errored or answered unparseably — this run proves nothing about containment"
  [ "$_fired" -eq 0 ] && ok "7 Stop events (threshold 5) inject no reminder" \
                      || ko "a reminder fired in an unregistered repo — STOP THE PILOT (see the header)"

  # Thresholds forced to their most permissive: if anything could fire, it would.
  _fired=0
  i=1
  while [ "$i" -le 3 ]; do
    if [ "$(decision "$(emit "force-$$" Stop --turn-threshold 1 --failed-review-threshold 1)")" = "REMINDER" ]; then _fired=1; fi
    i=$((i+1))
  done
  [ "$_fired" -eq 0 ] && ok "thresholds forced to 1 still inject nothing" \
                      || ko "a forced-threshold reminder fired unregistered — STOP THE PILOT"

  OUT="$(printf '{"session_id":"commit-%s","transcript_path":"/dev/null","hook_event_name":"PostToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"git commit -m probe"}}' "$$" "$REPO" | roborev agent-hook run $HOOK_RUN_ARGS --commit-threshold 1 2>&1)"
  [ "$(decision "$OUT")" != "REMINDER" ] && ok "a commit with the commit trigger forced on injects nothing" \
                                         || ko "a commit-triggered reminder fired unregistered — STOP THE PILOT"

  echo
  echo "phase A result: the hook is inert in this unregistered repository."
  echo "NOT PROVEN BY PHASE A, and #45 says so: that the hook fires when it SHOULD."
  echo "Run this on the pilot repo with --registered once it has open failed reviews;"
  echo "until then the containment argument rests on one half of the probe."
else
  # ---- PHASE B: the registered repo must fire ------------------------------
  echo "phase B — registered repo (#45 steps 1-2 and 5, the positive half):"
  # `status` is the JOB's state (queued|running|done|failed|canceled|...), NOT the
  # review's verdict: a `failed` job is one that CRASHED and produced no review.
  # What failed_review_threshold counts is an OPEN review with a FAILING verdict,
  # which is `closed == false` and `verdict == "F"` on a done job. The first
  # version of this counted status in (failed|fail|open), matched nothing, and so
  # phase B could never run — it always stopped at "produce them first".
  # `--open` is the CLI's own unresolved filter. Verified contract at 0.67.0:
  # verdict "F" is a failing review and "P" a passing one; `closed` is separate
  # from the job's `status`; and `list` WITHOUT --branch is scoped to the CURRENT
  # BRANCH, so this count is per-branch by design. If the pilot's failed reviews
  # sit on another branch, run this probe from that branch — an earlier comment
  # here claimed `--branch ''` cleared the filter, which the code never did and
  # which is not how the flag behaves.
  _n="$(roborev list --open --json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin) or []
except Exception:
    print(0); raise SystemExit
if not isinstance(rows, list):
    rows = rows.get("jobs") or []
print(sum(1 for r in rows
          if str(r.get("verdict", "")).upper() == "F"
          and not r.get("closed", False)))' 2>/dev/null || echo 0)"
  echo "  open reviews with a failing verdict: ${_n:-0}"
  if [ "${_n:-0}" -lt 4 ]; then
    ko "phase B needs >= failed_review_threshold (4) OPEN reviews with a failing verdict; found ${_n:-0} — produce them first (#45 step 1)"
  else
    _fired=0
    i=1
    while [ "$i" -le 7 ]; do
      # The threshold is passed EXPLICITLY so a machine whose config raises it
      # cannot make a threshold mismatch look like a scoping failure.
      if [ "$(decision "$(emit "probeB-$$" Stop --failed-review-threshold 4)")" = "REMINDER" ]; then _fired=1; fi
      i=$((i+1))
    done
    [ "$_fired" -eq 1 ] && ok "the reminder fires in the registered repo — scoping proven, not mere inertness" \
                        || ko "no reminder fired even in a registered repo with failed reviews: phase A proved only inertness (#45 step 5)"
  fi
fi

echo
echo "containment-probe: $PASS passed, $FAIL failed"
echo "session counters this probe incremented can be cleared with: roborev agent-hook reset --all"
[ "$FAIL" = 0 ]
