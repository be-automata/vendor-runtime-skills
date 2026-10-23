#!/bin/sh
# Classify the linked worktrees under <main>/.claude/worktrees and remove the ones a person picks
# (ENG-EXEC-07). Read-only unless --remove is given.
#
#   worktree-recycle.sh [--min-idle MINUTES]            classify every worktree (default mode)
#   worktree-recycle.sh --summary [--min-idle MINUTES]  one line, for the doctor
#   worktree-recycle.sh --remove PATH... [--delete-branch] [--min-idle MINUTES]
#
# One line per worktree:  <VERDICT> <reason> <path> branch=<b|-> pr=<#n|-> head=<sha7> ignored=<n>[ — detail]
#   RECYCLE merged|closed|in-base    safe to remove: PR merged or closed at this HEAD, or HEAD in the base
#   PRUNE   prunable                 the directory is gone; `git worktree prune` drops the metadata
#   KEEP    current|foreign|locked|in-use|dirty|unpushed|open-pr|no-pr|unknown
# The main checkout is never listed. Anything not proven safe is KEEP.
#
# --remove re-classifies each PATH first and removes only a RECYCLE row, with a plain
# `git worktree remove` (never --force: git's own dirty check is the second guard), then prunes.
# The branch ref survives; --delete-branch also runs `git branch -D`, only for a merged PR.
#
# Exit: 0 ran (or removed everything asked), 1 not inside a git repository, 2 bad arguments,
#       3 --remove refused at least one path (each refused path is left exactly as it was).
#
# Network: one REST read per branch, `repos/<o>/<r>/pulls?head=<o>:<branch>&state=all`, through
# ../../pr-sync/scripts/gh-rest.sh (curl; `api.github.com` in allowedDomains). `gh` is used for
# the token only (ADR-0004). No network, no token, or a non-GitHub origin -> KEEP unknown.
set -u
umask 077
HERE=$(dirname "$0")
. "$HERE/../../pr-sync/scripts/gh-rest.sh"

usage() { sed -n '5,7p' "$0" | sed 's/^# //' >&2; exit 2; }
MODE=classify; MIN_IDLE=120; DELETE_BRANCH=0; TARGETS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --summary) MODE=summary; shift;;
    --remove) MODE=remove; shift
      while [ $# -gt 0 ]; do case "$1" in --*) break;; *) TARGETS="$TARGETS
$1"; shift;; esac; done;;
    --delete-branch) DELETE_BRANCH=1; shift;;
    --min-idle) [ $# -ge 2 ] || usage; MIN_IDLE=$2; shift 2;;
    *) echo "worktree-recycle: unknown argument '$1'" >&2; usage;;
  esac
done
case "$MIN_IDLE" in ''|*[!0-9]*) echo "worktree-recycle: --min-idle must be minutes" >&2; exit 2;; esac
[ "$MODE" != remove ] || [ -n "$TARGETS" ] || { echo "worktree-recycle: --remove needs a path" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "worktree-recycle: jq is required" >&2; exit 2; }

real() { (cd "$1" 2>/dev/null && pwd -P); }
COMMON=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || { echo "worktree-recycle: not inside a git repository" >&2; exit 1; }
MAIN=$(git worktree list --porcelain | sed -n '1s/^worktree //p')
MAIN=$(real "$MAIN") || { echo "worktree-recycle: cannot resolve the main checkout" >&2; exit 1; }
CURRENT=$(real "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || true)
POOL="$MAIN/.claude/worktrees/"

# The repo on GitHub, from the main checkout's origin; empty when it is not github.com.
REPO=""; OWNER=""
ORIGIN=$(git -C "$MAIN" remote get-url origin 2>/dev/null || true)
case "$ORIGIN" in
  git@github.com:*) R=${ORIGIN#git@github.com:};; ssh://git@github.com/*) R=${ORIGIN#ssh://git@github.com/};;
  https://github.com/*) R=${ORIGIN#https://github.com/};; *) R="";;
esac
R=${R%/}; R=${R%.git}; R=${R%/}
case "$R" in */*/*|/*|*/) R="";; */*) OWNER=${R%%/*}; REPO=$R;; *) R="";; esac
case "$REPO" in *[!A-Za-z0-9_./-]*) REPO="";; esac

GH_REST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/worktree-recycle.XXXXXX") || exit 1
trap 'gh_rest_cleanup; rm -rf "$GH_REST_DIR"' EXIT

# GNU first: GNU `stat -f` means filesystem status and SUCCEEDS with a report, so trying the
# BSD spelling first on Linux yields prose, not a timestamp. BSD `stat -c` fails cleanly.
mtime() {
  _m=$(stat -c %Y "$1" 2>/dev/null) || _m=$(stat -f %m "$1" 2>/dev/null) || _m=0
  case "$_m" in ''|*[!0-9]*) _m=0;; esac
  echo "$_m"
}
in_use_by_process() {  # a process whose cwd is inside $1 (skipped when lsof is absent)
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -a -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | while IFS= read -r d; do
    case "$d/" in "$1"/*) echo y; break;; esac
  done | grep -q y
}
default_ref() {
  _d=$(git -C "$MAIN" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null) && { echo "$_d"; return; }
  for _b in main master; do git -C "$MAIN" rev-parse --verify --quiet "refs/remotes/origin/$_b" >/dev/null && { echo "refs/remotes/origin/$_b"; return; }; done
}

# classify <path> <head-sha> <branch|-> <locked 0|1> <lock-reason> <prunable 0|1>
#   sets V (verdict), WHY (reason), DETAIL, PRN (#n or -), IGN (ignored-file count)
classify() {
  p=$1; head=$2; br=$3; locked=$4; lreason=$5; prunable=$6
  V=KEEP; WHY=""; DETAIL=""; PRN=-; IGN=0
  [ "$prunable" = 1 ] && { V=PRUNE; WHY=prunable; return; }
  rp=$(real "$p") || { V=PRUNE; WHY=prunable; return; }
  [ "$rp" = "$MAIN" ] && { WHY=main; return; }                                                      # GUARD:main
  [ -n "$CURRENT" ] && [ "$rp" = "$CURRENT" ] && { WHY=current; return; }                            # GUARD:current
  case "$rp/" in "$POOL"*) ;; *) WHY=foreign; DETAIL="outside .claude/worktrees"; return;; esac     # GUARD:foreign
  [ "$locked" = 1 ] && { WHY=locked; DETAIL=$lreason; return; }                                      # GUARD:locked
  gd=$(git -C "$rp" rev-parse --path-format=absolute --git-dir 2>/dev/null)
  lg="$gd/logs/HEAD"; [ -f "$lg" ] || lg="$gd/HEAD"
  idle=$(( ( $(date +%s) - $(mtime "$lg") ) / 60 ))
  in_use_by_process "$rp" && { WHY=in-use; DETAIL="a process has its cwd here"; return; }           # GUARD:in-use
  [ "$idle" -lt "$MIN_IDLE" ] && { WHY=in-use; DETAIL="HEAD moved ${idle} min ago"; return; }       # GUARD:idle
  st=$(git -C "$rp" status --porcelain --untracked-files=all --ignored 2>/dev/null) || { WHY=unknown; DETAIL="git status failed"; return; }
  IGN=$(printf '%s\n' "$st" | grep -c '^!!')
  nmod=$(printf '%s\n' "$st" | grep -v '^!!' | grep -v '^??' | grep -c .)
  nunt=$(printf '%s\n' "$st" | grep -c '^??')
  [ $((nmod + nunt)) -gt 0 ] && { WHY=dirty; DETAIL="$nmod modified, $nunt untracked"; return; }  # GUARD:dirty
  if [ "$br" = - ]; then
    ref=$(default_ref)
    [ -n "$ref" ] && git -C "$MAIN" merge-base --is-ancestor "$head" "$ref" 2>/dev/null \
      && { V=RECYCLE; WHY=in-base; DETAIL="detached, HEAD is in ${ref#refs/remotes/}"; return; }
    WHY=no-pr; DETAIL="detached, HEAD not in the base"; return
  fi
  [ -n "$REPO" ] || { WHY=unknown; DETAIL="origin is not github.com"; return; }                    # GUARD:unknown
  q=$(printf '%s:%s' "$OWNER" "$br" | jq -sRr @uri)
  # The call and its guard are separate statements so the guard line can be
  # deleted on its own (the mutation suite); a `\` continuation cannot.
  _api_rc=0; api_get "repos/$REPO/pulls?head=$q&state=all&per_page=100" "$GH_REST_DIR/pulls.json" "$GH_REST_DIR/err" || _api_rc=$?
  [ "$_api_rc" = 0 ] || { WHY=unknown; DETAIL=$(head -1 "$GH_REST_DIR/err"); return; }               # GUARD:api
  pr=$(jq -c 'if type == "array" then (sort_by(.created_at) | last) else empty end' "$GH_REST_DIR/pulls.json" 2>/dev/null)
  if [ -z "$pr" ] || [ "$pr" = null ]; then
    ref=$(default_ref)
    [ -n "$ref" ] && git -C "$MAIN" merge-base --is-ancestor "$head" "$ref" 2>/dev/null \
      && { V=RECYCLE; WHY=in-base; DETAIL="no PR, HEAD is in ${ref#refs/remotes/}"; return; }
    WHY=no-pr; DETAIL="no PR for $br"; return
  fi
  PRN="#$(printf '%s' "$pr" | jq -r .number)"
  state=$(printf '%s' "$pr" | jq -r .state); merged=$(printf '%s' "$pr" | jq -r '.merged_at != null')
  prhead=$(printf '%s' "$pr" | jq -r '.head.sha // ""')
  [ "$state" = open ] && { WHY=open-pr; return; }                                                    # GUARD:open-pr
  # One statement per marked line: a `\` continuation cannot carry a marker, and
  # deleting the marked lines around it left a malformed mutant, not an unguarded one.
  if [ "$head" != "$prhead" ] && ! git -C "$MAIN" merge-base --is-ancestor "$head" "$prhead" 2>/dev/null; then  # GUARD:unpushed
    WHY=unpushed; DETAIL="HEAD ${head%"${head#???????}"} is not in the PR head ${prhead%"${prhead#???????}"}"; return  # GUARD:unpushed
  fi                                                                                                 # GUARD:unpushed
  V=RECYCLE
  if [ "$merged" = true ]; then WHY=merged; else WHY=closed; DETAIL="closed without merging; branch kept"; fi
}

# Walk the porcelain listing; emit one tab-separated record per linked worktree.
records() {
  git -C "$MAIN" worktree list --porcelain | awk '
    function flush() { if (path != "" && n > 1) printf "%s\t%s\t%s\t%d\t%s\t%d\n", path, head, br, lk, lr, pr; }
    /^worktree / { flush(); n++; path = substr($0, 10); head = "-"; br = "-"; lk = 0; lr = ""; pr = 0; next }
    /^HEAD /     { head = substr($0, 6); next }
    /^branch /   { br = substr($0, 8); sub(/^refs\/heads\//, "", br); next }
    /^locked/    { lk = 1; lr = substr($0, 8); next }
    /^prunable/  { pr = 1; next }
    END          { flush() }'
}

line() { printf '%s %s %s branch=%s pr=%s head=%s ignored=%s%s\n' "$V" "$WHY" "$1" "$2" "$PRN" "$(printf '%.7s' "$3")" "$IGN" "${DETAIL:+ — $DETAIL}"; }

TAB=$(printf '\t')
case "$MODE" in
  classify|summary)
    nr=0; nk=0; np=0; kinds=""; out=""
    while IFS="$TAB" read -r p h b lk lr pr; do
      classify "$p" "$h" "$b" "$lk" "$lr" "$pr"
      [ "$WHY" = main ] && continue
      l=$(line "$p" "$b" "$h"); out="$out$l
"
      case "$V" in RECYCLE) nr=$((nr + 1));; PRUNE) np=$((np + 1));; *) nk=$((nk + 1)); kinds="$kinds $WHY";; esac
    done <<EOF
$(records)
EOF
    if [ "$MODE" = summary ]; then
      k=""
      [ -n "$kinds" ] && k=$(printf '%s\n' $kinds | sort | uniq -c | awk '{printf "%s%s %s", (NR > 1 ? ", " : ""), $1, $2}')
      echo "worktrees: $nr recyclable, $np prunable, $nk kept${k:+ ($k)}"
      exit 0
    fi
    [ -n "$out" ] || { echo "no linked worktrees"; exit 0; }
    printf '%s' "$out"
    printf '%s' "$out" | while read -r v why p rest; do
      [ "$v" = RECYCLE ] && echo "  run: sh $0 --remove $p"
    done
    [ "$np" -gt 0 ] && echo "  run: git -C $MAIN worktree prune"
    exit 0;;
esac

# ---- --remove ------------------------------------------------------------------
rc=0
printf '%s\n' "$TARGETS" | while IFS= read -r t; do
  [ -n "$t" ] || continue
  rt=$(real "$t") || { echo "REFUSE $t — not a directory"; echo 3 > "$GH_REST_DIR/rc"; continue; }
  [ "$rt" = "$MAIN" ] && { echo "REFUSE $t — the main checkout"; echo 3 > "$GH_REST_DIR/rc"; continue; }  # GUARD:main
  found=""
  while IFS="$TAB" read -r p h b lk lr pr; do
    rp=$(real "$p" 2>/dev/null || echo "$p")
    [ "$rp" = "$rt" ] || continue
    found=1; classify "$p" "$h" "$b" "$lk" "$lr" "$pr"
    l=$(line "$p" "$b" "$h")
    [ "$V" = RECYCLE ] || { echo "REFUSE $l"; echo 3 > "$GH_REST_DIR/rc"; break; }                        # GUARD:recheck
    if err=$(git -C "$MAIN" worktree remove "$p" 2>&1); then
      echo "REMOVED $p (branch $b kept)"
    else
      echo "REFUSE $p — git: $(printf '%s' "$err" | head -1)"; echo 3 > "$GH_REST_DIR/rc"; break
    fi
    if [ "$DELETE_BRANCH" = 1 ] && [ "$b" != - ]; then
      if [ "$WHY" = merged ]; then
        git -C "$MAIN" branch -D "$b" >/dev/null 2>&1 && echo "DELETED branch $b ($PRN merged)" || echo "KEPT branch $b — git branch -D failed"
      else
        echo "KEPT branch $b — $WHY, not merged: --delete-branch applies to a merged PR only"
      fi
    fi
    break
  done <<EOF
$(records)
EOF
  [ -n "$found" ] || { echo "REFUSE $t — not a linked worktree of this repository"; echo 3 > "$GH_REST_DIR/rc"; }
done
git -C "$MAIN" worktree prune 2>/dev/null
[ -f "$GH_REST_DIR/rc" ] && rc=$(cat "$GH_REST_DIR/rc")
exit "$rc"
