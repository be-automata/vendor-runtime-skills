#!/bin/sh
# The bound on /pr-sync auto-push (ADR-0008). Deterministic; the session calls it and obeys it.
#
#   autopush-guard.sh arm     --state DIR --pr N --repo owner/name --who NAME [--max-cycles K]
#                             [--source default|flag]
#   autopush-guard.sh decline --repo owner/name --pr N --who NAME [--lift]
#   autopush-guard.sh armed   --state DIR --pr N --repo owner/name
#   autopush-guard.sh check   --state DIR --pr N --repo owner/name --graded SHA
#                             --gates "label=rc ..." --findings "<dump.json> ..." [--max-lines N]
#   autopush-guard.sh verify  --state DIR --pr N --repo owner/name
#   autopush-guard.sh record  --state DIR --pr N --pushed SHA --findings "<dump.json> ..."
#                             --gates "label=rc ..."
#
# arm     writes <state>/pr-<n>/autopush.armed (who, date, K, source). `default` is /pr-sync arming
#         itself (D-11); `flag` is a person's explicit answer: dev-execution's run (D-9) or
#         loop-mode's start question (D-12). The source is a label for the record and the
#         trailer, not proof: the caller states it.
#         Refuses while ADR-0008 is not accepted, under SOMNIO_PR_SYNC_AUTOPUSH=off, and on a
#         PR a person declined.
# decline records a person's No for one PR (D-12), in a per-machine directory every attach
#         reads: <XDG_STATE_HOME>/somnio-pr-watch/declined/, and under the account's real home
#         (from the password database, not $HOME), so neither variable can hide it. A No is absolute: arm (any source),
#         armed --repo and check all refuse it. Only `decline --lift`, a person's act, removes
#         it; the lift is appended to declined/lifted.jsonl.
# armed   exit 0 when armed, not disarmed and not declined, else 1. Nothing printed but the reason.
# check   run from the PR's local checkout with the fix STAGED and not yet committed: it grades
#         the index against the graded head, so its TRAILER lines go into that commit. Prints:
#           AUTOPUSH ok cycle <i>/<K>          then TRAILER lines for the commit message
#           AUTOPUSH refuse <reason> — <detail>
#         Exit 0 on ok, 1 on refuse. A refusal means: ask the person, as on an unarmed PR.
#         On ok it writes autopush.pending: the graded sha, the staged tree and the finding keys.
#         --graded may be the 7-character sha an event line prints; it is resolved in the
#         checkout. Each gate is one token, `label=rc` (e.g. `pnpm-test=0`).
# verify  after the commit, BEFORE the push: the PR is still armed and not declined, and HEAD
#         carries exactly the pending tree with the graded head as its only parent. A hook that
#         rewrote the commit, or a No given after check, is caught here, not after the push.
# A finding drives a push only when its author is the repo's recorded bot or a collaborator with
# write access. The bot is read by review-mode.sh from .claude/harness.json AT THE PR'S BASE
# (never the PR's own head, the working tree or the environment) and must be a Bot account (user_type); a
# collaborator (`role`, GitHub's author_association) must hold write, maintain or admin on the
# repo (read live in check). Any other account, a Bot included, is asked.
# record  after the push, from the same checkout: refuses unless the pushed commit carries
#         exactly the pending tree, has the graded head as its only parent, and the findings are
#         the pending ones. Then appends one line to autopush.jsonl and the findings'
#         fingerprints to autopush.fixed, and consumes the pending approval.
# Exit 2 on bad arguments everywhere.
#
# Network: `check` reads pulls/<n>, pulls/<n>/files, branches/<head> and a collaborator's
# permission through gh-rest.sh (curl, sandboxed), always at api.github.com.
# Nothing the guard grades comes from the caller's environment: the API host is fixed here, and
# internal state uses a prefix no caller sets.
set -u
umask 077
HERE=$(dirname "$0")
GH_REST_API=https://api.github.com
. "$HERE/gh-rest.sh"

# Flipped to 1 only by the PR that records ADR-0008 as accepted. A test ties the two together.
ADR_0008_ACCEPTED=1
DEFAULT_CYCLES=3
MAX_CYCLES=5
DEFAULT_LINES=1000

usage() { sed -n '4,12p' "$0" | sed 's/^# //' >&2; exit 2; }
[ $# -ge 1 ] || usage
CMD=$1; shift
_AG_RBOT=""; _AG_BOTREV=""; STATE=""; PR=""; WHO=""; SOURCE=flag; LIFT=0; K=$DEFAULT_CYCLES; REPO=""; GRADED=""; GATES=""; FINDINGS=""; PUSHED=""; LINES=$DEFAULT_LINES
while [ $# -gt 0 ]; do
  [ "$1" = --lift ] && { LIFT=1; shift; continue; }
  [ $# -ge 2 ] || usage
  case "$1" in
    --state) STATE=$2;; --pr) PR=$2;; --who) WHO=$2;; --max-cycles) K=$2;; --repo) REPO=$2;;
    --graded) GRADED=$2;; --gates) GATES=$2;; --findings) FINDINGS=$2;; --pushed) PUSHED=$2;;
    --max-lines) LINES=$2;; --source) SOURCE=$2;;
    *) echo "autopush-guard: unknown argument '$1'" >&2; usage;;
  esac
  shift 2
done
case "$PR" in ''|*[!0-9]*|0*) echo "autopush-guard: --pr must be a positive integer" >&2; exit 2;; esac
[ "$CMD" = decline ] || case "$STATE" in /*) ;; *) echo "autopush-guard: --state must be an absolute path" >&2; exit 2;; esac
DIR="$STATE/pr-$PR"
DECLINED_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/somnio-pr-watch/declined"
REALHOME=$(eval "printf '%s' ~$(id -un)")   # getpwnam: the account's home, not $HOME
DECLINED_HOME="$REALHOME/.local/state/somnio-pr-watch/declined"
DECLINED_ENVHOME="$HOME/.local/state/somnio-pr-watch/declined"
ARMED="$DIR/autopush.armed"; LOG="$DIR/autopush.jsonl"; FIXED="$DIR/autopush.fixed"; PENDING="$DIR/autopush.pending"
command -v jq >/dev/null 2>&1 || { echo "autopush-guard: jq is required" >&2; exit 2; }

refuse() { echo "AUTOPUSH refuse $1 — $2"; exit 1; }
# Every git read runs from the top of the checkout and never relative (diff.relative would hide
# changes outside the cwd).
to_top() { TOP=$(git rev-parse --show-toplevel 2>/dev/null) && cd "$TOP" || refuse local "not inside a git checkout"; }
pending_matches() {  # pending_matches <commit>: its tree and sole parent are what check approved
  jq -e '.graded and .tree and .keys' "$PENDING" >/dev/null 2>&1 \
    || refuse "$CMD" "no pending approval for PR-$PR: run check on the staged fix first"
  [ "$(git rev-parse "$1^{tree}")" = "$(jq -r .tree "$PENDING")" ] \
    || refuse "$CMD" "the commit's tree is not the tree check approved"
  { [ "$(git rev-parse -q --verify "$1^1" 2>/dev/null)" = "$(jq -r .graded "$PENDING")" ] \
    && ! git rev-parse -q --verify "$1^2" >/dev/null 2>&1; } \
    || refuse "$CMD" "the commit's only parent must be the graded head"
}
not_accepted() { [ "$ADR_0008_ACCEPTED" = 1 ] || refuse adr-0008-not-accepted \
  "ADR-0008 is proposed; auto-push stays inert until the TL accepts it. Ask before this push."; }
disarmed() { [ "${SOMNIO_PR_SYNC_AUTOPUSH:-}" = off ]; }
# A who with a control character could print forged TRAILER lines; refuse it at the door.
who_ok() {
  [ -n "$(printf '%s' "$WHO" | tr -d '[:space:]')" ] || { echo "autopush-guard: $CMD needs --who" >&2; exit 2; }
  [ "$(printf '%s' "$WHO" | tr -d '[:cntrl:]')" = "$WHO" ] || { echo "autopush-guard: --who must not contain control characters" >&2; exit 2; }
}
repo_ok() {
  case "$REPO" in */*/*|*[!A-Za-z0-9_./-]*|.*|-*) echo "autopush-guard: --repo must be owner/name" >&2; exit 2;;
    */*) ;; *) echo "autopush-guard: --repo must be owner/name" >&2; exit 2;; esac
}
# GitHub names are case-insensitive; `+` never appears in one, so the map is one-to-one.
declined_name() { printf '%s__pr-%s' "$(printf '%s' "$REPO" | tr 'A-Z/' 'a-z+')" "$PR"; }
# The marker that holds, in either directory; empty when none does.
declined_any() {
  for d in "$DECLINED_DIR" "$DECLINED_HOME" "$DECLINED_ENVHOME"; do [ -f "$d/$(declined_name)" ] && { printf '%s/%s' "$d" "$(declined_name)"; return 0; }; done
  return 1
}
refuse_declined() {
  _ag_m=$(declined_any) || return 0
  refuse declined "$(jq -r '"\(.who) declined auto-push on this PR at \(.date)"' "$_ag_m" 2>/dev/null \
    || echo "a person declined auto-push on this PR"); every push is asked until a person runs decline --lift"
}
short() { printf '%.7s' "$1"; }
# Every staged-diff read in one place, so the scope, mode, binary and size checks grade one diff.
staged() { git diff --cached --no-renames --no-relative --ignore-submodules=none "$@" "$GRADED"; }
# The bot whose findings may drive a push: the one the repo records AT $_AG_BOTREV (the PR's
# base), read by the same reader the watch arms from. Neither the PR's own head nor a
# working-tree edit can change it. A base with no record trusts review-mode's default bot.
recorded_bot() {
  [ -n "$_AG_RBOT" ] && return 0
  git cat-file -e "$_AG_BOTREV^{commit}" 2>/dev/null \
    || refuse local "the PR's base $(short "$_AG_BOTREV") is not in this checkout: git fetch"
  _ag_root=$(mktemp -d "${TMPDIR:-/tmp}/autopush-bot.XXXXXX") || refuse findings "cannot make a temp directory"
  mkdir -p "$_ag_root/.claude"
  git show "$_AG_BOTREV:.claude/harness.json" > "$_ag_root/.claude/harness.json" 2>/dev/null \
    || rm -f "$_ag_root/.claude/harness.json"
  _AG_RBOT=$(sh "$HERE/review-mode.sh" "$_ag_root" 2>/dev/null | sed -n 's/^BOT=//p')
  rm -rf "$_ag_root"
  [ -n "$_AG_RBOT" ] || refuse findings "cannot read the repo's recorded reviewer at $(short "$_AG_BOTREV")"
}
# A dump counts only when the watch wrote it: a regular file under this PR's state directory,
# marked worklist=yes, written by the recorded bot (a Bot account with that login) or a
# collaborator (D-2). check and record both apply it; check also reads the collaborator's
# permission.
dump_ok() {
  [ -f "$1" ] || refuse findings "dump missing: $1"
  [ ! -L "$1" ] && [ "$(cd "$(dirname "$1")" 2>/dev/null && pwd -P)" = "$(cd "$DIR" 2>/dev/null && pwd -P)" ] \
    || refuse findings "$(basename "$1") is not a dump under $DIR"
  jq -e '.worklist == true' "$1" >/dev/null 2>&1 || refuse findings "$(basename "$1") is not worklist=yes"
  recorded_bot
  jq -e --arg b "$_AG_RBOT" 'def norm: ascii_downcase | sub("\\[bot\\]$"; "");
      if (.role // "") != "" then true
      else ($b != "none" and (.user_type // "") == "Bot" and ((.who // "") | norm) == ($b | norm)) end' "$1" >/dev/null 2>&1 \
    || refuse untrusted-author "$(basename "$1") is from $(jq -r '.who // "an unknown author"' "$1" | tr -d '[:cntrl:]'), not the recorded bot or a collaborator"
}
finding_keys() { for f in $FINDINGS; do jq -r '.key // .id // "?"' "$f"; done | sort -u | tr '\n' ' ' | sed 's/ $//'; }
cycles_done() { [ -f "$LOG" ] && grep -c . "$LOG" | head -1 || echo 0; }
# One fingerprint per finding: its path plus its body, lowercased, whitespace-collapsed,
# first 300 chars. Reworded findings escape it; a finding that comes back verbatim does not.
fingerprint() {
  jq -r '((.path // "") + "\n" + ((.body // "") | ascii_downcase | gsub("\\s+"; " ") | .[0:300]))' "$1" 2>/dev/null \
    | { shasum -a 256 2>/dev/null || sha256sum; } | cut -c1-16
}

case "$CMD" in
  arm)
    not_accepted
    disarmed && refuse disarmed "SOMNIO_PR_SYNC_AUTOPUSH=off — this PR stays unarmed; every push is asked"
    case "$SOURCE" in default|flag) ;; *) echo "autopush-guard: --source must be default or flag" >&2; exit 2;; esac
    who_ok; repo_ok; refuse_declined
    case "$K" in ''|*[!0-9]*|0*) echo "autopush-guard: --max-cycles must be 1..$MAX_CYCLES" >&2; exit 2;; esac
    [ "$K" -le "$MAX_CYCLES" ] || { echo "autopush-guard: --max-cycles must be 1..$MAX_CYCLES" >&2; exit 2; }
    mkdir -p "$DIR" && [ ! -L "$DIR" ] || { echo "autopush-guard: cannot use $DIR" >&2; exit 2; }
    jq -n --arg who "$WHO" --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson k "$K" --arg src "$SOURCE" \
      '{who: $who, date: $date, max_cycles: $k, source: $src}' > "$ARMED"
    echo "AUTOPUSH armed PR-$PR by $WHO ($SOURCE) — up to $K cycle(s); merge, replies and review requests stay yours"
    exit 0;;
  decline)
    repo_ok; who_ok
    for d in "$DECLINED_DIR" "$DECLINED_HOME"; do
      mkdir -p "$d" && [ ! -L "$d" ] || { echo "autopush-guard: cannot use $d" >&2; exit 2; }
    done
    if [ "$LIFT" = 1 ]; then
      _ag_m=$(declined_any) || { echo "AUTOPUSH no decline recorded for $REPO PR-$PR"; exit 0; }
      jq -cn --arg repo "$REPO" --argjson pr "$PR" --arg who "$WHO" --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --slurpfile d "$_ag_m" '{repo: $repo, pr: $pr, lifted_by: $who, at: $date, declined: $d[0]}' \
        >> "$DECLINED_DIR/lifted.jsonl" && rm -f "$DECLINED_DIR/$(declined_name)" "$DECLINED_HOME/$(declined_name)" "$DECLINED_ENVHOME/$(declined_name)"
      echo "AUTOPUSH decline lifted on $REPO PR-$PR by $WHO — arming is allowed again"
      exit 0
    fi
    jq -n --arg who "$WHO" --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{who: $who, date: $date}' > "$DECLINED_DIR/$(declined_name)"
    [ "$DECLINED_HOME" = "$DECLINED_DIR" ] || cp "$DECLINED_DIR/$(declined_name)" "$DECLINED_HOME/$(declined_name)"
    echo "AUTOPUSH declined $REPO PR-$PR by $WHO — nothing arms or pushes it until a person lifts the decline"
    exit 0;;
  armed)
    disarmed && { echo "disarmed by SOMNIO_PR_SYNC_AUTOPUSH=off"; exit 1; }
    [ "$ADR_0008_ACCEPTED" = 1 ] || { echo "ADR-0008 is not accepted"; exit 1; }
    jq -e '.who and .max_cycles' "$ARMED" >/dev/null 2>&1 || { echo "PR-$PR is not armed"; exit 1; }
    repo_ok; ! declined_any >/dev/null || { echo "PR-$PR was declined by a person"; exit 1; }
    exit 0;;
  record)
    case "$PUSHED" in ''|*[!0-9a-f]*) echo "autopush-guard: --pushed must be a commit sha" >&2; exit 2;; esac
    jq -e . "$ARMED" >/dev/null 2>&1 || { echo "autopush-guard: PR-$PR is not armed" >&2; exit 2; }
    to_top
    # Only what check approved is recorded: the same tree, on the graded head, for the same findings.
    [ -n "$FINDINGS" ] || refuse record "no findings named"
    FULL=$(git rev-parse --verify -q "$PUSHED^{commit}" 2>/dev/null) || refuse record "$PUSHED is not a commit in this checkout"
    pending_matches "$FULL"
    _AG_BOTREV=$(jq -r '.base // .graded' "$PENDING")
    for f in $FINDINGS; do dump_ok "$f"; done
    [ "$(finding_keys)" = "$(jq -r '.keys' "$PENDING")" ] \
      || refuse record "findings differ from the ones check approved ($(jq -r '.keys' "$PENDING"))"
    PUSHED=$FULL
    for f in $FINDINGS; do printf '%s %s\n' "$(fingerprint "$f")" "$(jq -r '.key // .id // "?"' "$f")" >> "$FIXED"; done
    ids=$(finding_keys)
    rm -f "$PENDING"
    jq -cn --arg sha "$PUSHED" --arg ids "$ids" --arg gates "$GATES" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --argjson n "$(( $(cycles_done) + 1 ))" \
      '{cycle: $n, at: $at, pushed: $sha, findings: ($ids | split(" ") | map(select(. != ""))), gates: $gates}' >> "$LOG"
    echo "AUTOPUSH recorded cycle $(cycles_done) at $(short "$PUSHED")"
    exit 0;;
  verify)
    not_accepted
    disarmed && refuse disarmed "SOMNIO_PR_SYNC_AUTOPUSH=off"
    jq -e '.who and .max_cycles' "$ARMED" >/dev/null 2>&1 || refuse not-armed "PR-$PR is not armed in this session"
    repo_ok; refuse_declined
    to_top
    pending_matches HEAD
    H=$(git rev-parse HEAD)
    echo "AUTOPUSH verified $(short "$H") — push it: git push origin HEAD:<head.ref>"
    exit 0;;
  check) ;;
  *) usage;;
esac

# ---- check ------------------------------------------------------------------
not_accepted
disarmed && refuse disarmed "SOMNIO_PR_SYNC_AUTOPUSH=off"
jq -e '.who and .max_cycles' "$ARMED" >/dev/null 2>&1 || refuse not-armed "PR-$PR is not armed in this session (--no-auto-push, or no /pr-sync attach)"
repo_ok
refuse_declined
K=$(jq -r .max_cycles "$ARMED"); WHO=$(jq -r .who "$ARMED" | tr -d '[:cntrl:]'); ADATE=$(jq -r .date "$ARMED" | tr -d '[:cntrl:]')
# A record written before D-11 has no source: it was a person's flag.
ASRC=$(jq -r '.source // "flag"' "$ARMED"); ASUFFIX=""; [ "$ASRC" = default ] && ASUFFIX=" (default)"
case "$K" in ''|*[!0-9]*|0*) refuse cap "the armed record's max_cycles '$K' is not 1..$MAX_CYCLES";; esac
[ "$K" -le "$MAX_CYCLES" ] || refuse cap "the armed record's max_cycles $K exceeds $MAX_CYCLES"
DONE=$(cycles_done)
[ "$DONE" -lt "$K" ] || refuse cap "$DONE of $K cycles spent"
case "$GRADED" in ''|*[!0-9a-f]*) echo "autopush-guard: --graded must be a commit sha" >&2; exit 2;; esac
case "$LINES" in ''|*[!0-9]*|0*) echo "autopush-guard: --max-lines must be 1..$DEFAULT_LINES" >&2; exit 2;; esac
[ "$LINES" -le "$DEFAULT_LINES" ] || { echo "autopush-guard: --max-lines can only lower the cap (1..$DEFAULT_LINES)" >&2; exit 2; }
to_top
# An event line prints 7 characters; GitHub reports 40. Compare full shas.
SHORT=$GRADED
GRADED=$(git rev-parse -q --verify "$GRADED^{commit}" 2>/dev/null) \
  || refuse local "the graded head $SHORT is not in this checkout: git fetch, then fast-forward to it"
# The last approval's commit reached the head, even with commits on top of it (an "Update
# branch" merge, another push): it was pushed and never recorded. Counting it first keeps the
# cycle cap and the recurrence memory honest.
if jq -e '.graded and .tree' "$PENDING" >/dev/null 2>&1; then
  PG=$(jq -r .graded "$PENDING"); PT=$(jq -r .tree "$PENDING")
  if [ "$PG" != "$GRADED" ] && git merge-base --is-ancestor "$PG" "$GRADED" 2>/dev/null; then
    for c in $(git rev-list "$PG..$GRADED"); do
      [ "$(git rev-parse -q --verify "$c^1" 2>/dev/null)" = "$PG" ] && [ "$(git rev-parse "$c^{tree}")" = "$PT" ] \
        && refuse unrecorded-push "the fix approved last cycle was pushed ($(short "$c")) but never recorded: run record --pushed $(short "$c") first"
    done
  fi
fi

# Rule 9: every gate the session ran exited 0, and it ran at least one.
NGATES=0
for g in $GATES; do NGATES=$((NGATES + 1)); done
[ "$NGATES" -gt 0 ] || refuse gates "no gate results given — run the repo's verification commands first"
for g in $GATES; do
  case "$g" in *=0) ;; *) refuse gates "$g did not exit 0";; esac
done

# The PR, read fresh: state, head branch, fork, remote head, base.
GH_REST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/autopush-guard.XXXXXX") || exit 1
trap 'rm -rf "$GH_REST_DIR"' EXIT
api_get "repos/$REPO/pulls/$PR" "$GH_REST_DIR/pull.json" "$GH_REST_DIR/err" \
  || refuse unreadable "cannot read PR-$PR: $(head -1 "$GH_REST_DIR/err")"
_AG_BOTREV=$(jq -r '.base.sha // ""' "$GH_REST_DIR/pull.json")
case "$_AG_BOTREV" in ''|*[!0-9a-f]*) refuse unreadable "PR-$PR names no base commit";; esac

# Findings: each dump exists and none has come back after an earlier fix.
NF=0
for f in $FINDINGS; do NF=$((NF + 1)); done
[ "$NF" -gt 0 ] || refuse findings "no findings named for this push"
for f in $FINDINGS; do
  dump_ok "$f"
  fp=$(fingerprint "$f"); key=$(jq -r '.key // .id // "?"' "$f")
  [ -f "$FIXED" ] || continue
  # The same key again is a standing review nobody re-reviewed; a new key with the same text
  # is a finding that came back.
  grep -qxF "$fp $key" "$FIXED" && refuse already-fixed \
    "$(basename "$f") ($key) was fixed in an earlier cycle and its reviewer has not re-reviewed; omit it from --findings"
  grep -q "^$fp\( \|\$\)" "$FIXED" && refuse recurring "$(basename "$f") was fixed in an earlier cycle and came back"
done

# A merged or closed PR takes no push: the fix would never reach its base.
jq -e '.state == "open" and (.merged | not)' "$GH_REST_DIR/pull.json" >/dev/null 2>&1 \
  || refuse pr-not-open "PR-$PR is $(jq -r 'if .merged then "merged" else (.state // "unreadable") end' "$GH_REST_DIR/pull.json")"
REF=$(jq -r '.head.ref // ""' "$GH_REST_DIR/pull.json")
case "$REF" in
  ''|main|master|develop|development|staging|production|prod|trunk|release/*|release-*|hotfix/*)
    refuse long-lived-branch "head branch '$REF' is long-lived; fix it through a PR into it" ;;
esac
jq -e '.head.repo.full_name == .base.repo.full_name' "$GH_REST_DIR/pull.json" >/dev/null 2>&1 \
  || refuse fork "the head lives in $(jq -r '.head.repo.full_name // "a deleted fork"' "$GH_REST_DIR/pull.json")"
# A protected head is long-lived whatever its name (`qa`, `uat`, an environment branch).
REFQ=$(jq -rn --arg r "$REF" '$r | @uri' | sed 's|%2F|/|g')
api_get "repos/$REPO/branches/$REFQ" "$GH_REST_DIR/branch.json" "$GH_REST_DIR/err" \
  || refuse unreadable "cannot read branch '$REF': $(head -1 "$GH_REST_DIR/err")"
jq -e '.protected == false' "$GH_REST_DIR/branch.json" >/dev/null 2>&1 \
  || refuse protected-branch "head branch '$REF' is protected on GitHub; fix it through a PR into it"
REMOTE=$(jq -r '.head.sha // ""' "$GH_REST_DIR/pull.json")
[ "$REMOTE" = "$GRADED" ] || refuse head-moved "remote head is $(short "$REMOTE"), findings were graded at $(short "$GRADED")"

# A collaborator's finding drives a push only when they can push themselves: author_association
# says nothing about access (a read-only org member is MEMBER). The recorded bot needs no read.
# `permission` is the base level (admin/write/read/none) whatever role name a custom role carries.
PERMOK=" "
for f in $FINDINGS; do
  role=$(jq -r '.role // ""' "$f"); [ -n "$role" ] || continue
  who=$(jq -r '.who // ""' "$f")
  case "$who" in ''|*[!A-Za-z0-9-]*|-*) refuse untrusted-author "$(basename "$f") names no valid login";; esac
  case "$PERMOK" in *" $who "*) continue;; esac
  api_get "repos/$REPO/collaborators/$who/permission" "$GH_REST_DIR/perm.json" "$GH_REST_DIR/err" \
    || refuse untrusted-author "cannot read $who's permission ($(head -1 "$GH_REST_DIR/err")); ask"
  jq -e '(.permission // "") == "admin" or (.permission // "") == "write" or (.user.permissions.push // false)' \
      "$GH_REST_DIR/perm.json" >/dev/null 2>&1 \
    || refuse untrusted-author "$who ($role) has $(jq -r '.role_name // .permission // "no"' "$GH_REST_DIR/perm.json") access, not write"
  PERMOK="$PERMOK$who "
done

# The local checkout: on the head branch, at the graded head, something staged. HEAD must BE the
# graded head: unpushed local commits would ride along ungraded.
BR=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || refuse local "HEAD is detached"
[ "$BR" = "$REF" ] || refuse local "checked out '$BR', the PR head is '$REF'"
[ "$(git rev-parse HEAD 2>/dev/null)" = "$GRADED" ] \
  || refuse local "HEAD is not the graded head $GRADED (unpushed local commits?)"
staged --quiet 2>/dev/null && refuse local "nothing staged against $GRADED"

# The diff (index vs the graded head): in scope, not protected, not sensitive, not large.
# --no-renames: a rename must show both paths, or a protected file could move into a PR path
# unseen. -z: raw names, compared byte for byte with GitHub's, never git's quoted spelling.
staged -z --name-only > "$GH_REST_DIR/names" \
  || refuse local "cannot list the staged files"
[ "$(tr -cd '\n' < "$GH_REST_DIR/names" | wc -c)" -eq 0 ] || refuse out-of-scope "a staged file name contains a newline"
CHANGED=$(tr '\000' '\n' < "$GH_REST_DIR/names")
# A fix edits file contents: no symlink, no submodule, no mode change.
BADMODE=$(staged --raw | awk '{ o = substr($1, 2); n = $2
  if (n == "120000" || n == "160000" || (n != "000000" && n != "100644" && n != "100755") \
      || (o != n && o != "000000" && n != "000000")) { print o " -> " n; exit } }')
[ -z "$BADMODE" ] || refuse mode-change "a staged entry changes its file type or mode ($BADMODE)"
api_get_all "repos/$REPO/pulls/$PR/files" "$GH_REST_DIR/err" > "$GH_REST_DIR/files.json" 2>/dev/null \
  || refuse unreadable "cannot read the PR's files: $(head -1 "$GH_REST_DIR/err")"
PRFILES=$(jq -r '.[].filename' "$GH_REST_DIR/files.json")
# Matched case-insensitively. The harness's own guard and plugin are protected: a fix may never edit what grades it.
PROTECTED_RE='(^|/)(\.github|\.claude|\.loop|\.husky)/|^plugins/somnio-engineering-ai/|^skills/pr-sync/scripts/|(^|/)(CODEOWNERS|\.gitattributes|\.gitmodules|\.pre-commit-config\.yaml|\.npmrc|\.envrc)$|(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|pubspec\.lock|poetry\.lock|uv\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock|package\.json|pubspec\.yaml|pyproject\.toml|requirements[^/]*\.txt|Pipfile|go\.mod|Cargo\.toml|Gemfile|composer\.json)$'
# A path pattern, not a call: `terraform/` names a directory. The marker below opts this line out of check_network_cli.py.
SENSITIVE_RE='(^|/)(migrations?|migrate|db/migrate|prisma/migrations|alembic/versions|changelog)/|\.bicep$|(^|/)[^/]*(openapi|swagger)[^/]*\.(ya?ml|json)$|(^|/)(infra|infrastructure|terraform|k8s|kubernetes|helm|charts|deploy|ops)/|\.tf$|\.tfvars$|(^|/)Dockerfile[^/]*$|(^|/)docker-compose[^/]*$'  # network-cli: prose
# One name per line, never split on spaces or expanded as a glob.
set -f; OIFS=$IFS; IFS='
'
for f in $CHANGED; do
  printf '%s\n' "$f" | grep -Eiq "$PROTECTED_RE" && refuse protected-path "$f"
  printf '%s\n' "$f" | grep -Eiq "$SENSITIVE_RE" && refuse sensitive-path "$f (rule 6: the TL reviews it)"
  # Binary by content, not by attributes: an untracked .gitattributes can make numstat call a blob text.
  if [ -n "$(git ls-files -- ":(literal)$f")" ]; then
    git cat-file -e ":./$f" 2>/dev/null || refuse local "cannot read the staged blob of $f"
    [ "$(git cat-file -s ":./$f")" -eq "$(git cat-file -p ":./$f" | tr -d '\000' | wc -c | tr -d ' ')" ] \
      || refuse binary "$f is a binary change; the line cap cannot measure it"
  fi
  # Only files the PR already changes (D-11): a new file, test or not, is the person's call.
  printf '%s\n' "$PRFILES" | grep -qxF "$f" && continue
  refuse out-of-scope "$f is not one of the PR's files"
done
IFS=$OIFS; set +f
NUMSTAT=$(staged --numstat)
BIN=$(printf '%s\n' "$NUMSTAT" | awk -F '\t' '$1 == "-" || $2 == "-" { print $3; exit }')
[ -z "$BIN" ] || refuse binary "$BIN is a binary change; the line cap cannot measure it"
N=$(printf '%s\n' "$NUMSTAT" | awk '{ n += $1 + $2 } END { print n + 0 }')
[ "$N" -le "$LINES" ] || refuse too-large "$N changed lines, cap $LINES"

TREE=$(git write-tree) || refuse local "cannot write the staged tree"
jq -n --arg g "$GRADED" --arg t "$TREE" --arg k "$(finding_keys)" --arg b "$_AG_BOTREV" \
  '{graded: $g, tree: $t, keys: $k, base: $b}' > "$PENDING"
echo "AUTOPUSH ok cycle $((DONE + 1))/$K — ${N} line(s) in $(printf '%s\n' "$CHANGED" | grep -c .) file(s)"
echo "TRAILER Review-Fix: pr-sync auto cycle $((DONE + 1))/$K"
echo "TRAILER Review-Findings: $(finding_keys)"
echo "TRAILER Armed-By: $WHO $ADATE$ASUFFIX"
echo "TRAILER Gates: $GATES"
exit 0
