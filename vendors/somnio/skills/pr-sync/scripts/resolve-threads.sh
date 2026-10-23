#!/bin/sh
# Resolve the review threads an auto-pushed commit fixed (ADR-0008 D-3b). Nothing else.
#
#   resolve-threads.sh --state DIR --repo owner/name --pr N --pushed SHA --comments "<inline id> ..."
#
# Resolves a thread only when its ROOT comment's id is in --comments AND the cycle that pushed
# SHA recorded that inline finding (autopush-guard.sh record, autopush.jsonl). Any other id is
# REFUSED: a thread the commit did not fix stays the person's. A reply inside someone else's
# thread never resolves that thread.
# Refuses unless the PR is armed and not declined (autopush-guard.sh armed --repo) — on an unarmed
# PR (--no-auto-push, SOMNIO_PR_SYNC_AUTOPUSH=off, a recorded decline), resolving stays the
# person's action. --pushed may be the full sha record wrote or its 7+ character prefix. Prints one line per thread:
#   RESOLVED <comment id> | SKIP <id> <why> | REFUSED <id> <why>
# Exit: 0 every named thread resolved or already resolved · 1 any failure or refusal · 2 bad arguments.
#
# Network: GraphQL over curl through gh-rest.sh (api.github.com/graphql, sandboxed).
set -u
umask 077
HERE=$(dirname "$0")
GH_REST_API=https://api.github.com   # never the caller's environment
. "$HERE/gh-rest.sh"

STATE=""; REPO=""; PR=""; IDS=""; PUSHED=""
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || { echo "resolve-threads: $1 needs a value" >&2; exit 2; }
  case "$1" in
    --state) STATE=$2;; --repo) REPO=$2;; --pr) PR=$2;; --comments) IDS=$2;; --pushed) PUSHED=$2;;
    *) echo "resolve-threads: unknown argument '$1'" >&2; exit 2;;
  esac
  shift 2
done
case "$PR" in ''|*[!0-9]*|0*) echo "resolve-threads: --pr must be a positive integer" >&2; exit 2;; esac
case "$REPO" in */*/*|*[!A-Za-z0-9_./-]*|.*|-*) echo "resolve-threads: --repo must be owner/name" >&2; exit 2;; */*) ;; *) echo "resolve-threads: --repo must be owner/name" >&2; exit 2;; esac
[ -n "$IDS" ] || { echo "resolve-threads: --comments names no comment" >&2; exit 2; }
case "$PUSHED" in ''|*[!0-9a-f]*) echo "resolve-threads: --pushed must be the commit sha the cycle recorded (7+ hex)" >&2; exit 2;; esac
[ "${#PUSHED}" -ge 7 ] || { echo "resolve-threads: --pushed must be the commit sha the cycle recorded (7+ hex)" >&2; exit 2; }
for i in $IDS; do case "$i" in *[!0-9]*) echo "resolve-threads: comment ids are integers, got '$i'" >&2; exit 2;; esac; done
command -v jq >/dev/null 2>&1 || { echo "resolve-threads: jq is required" >&2; exit 2; }

why=$(sh "$HERE/autopush-guard.sh" armed --state "$STATE" --pr "$PR" --repo "$REPO") \
  || { echo "resolve-threads: refused — $why; resolving threads stays the person's action"; exit 1; }

# The inline findings the cycle that pushed this sha recorded: the only threads it may resolve.
FIXED_IDS=$(jq -r --arg s "$PUSHED" 'select(.pushed | startswith($s)) | .findings[]' "$STATE/pr-$PR/autopush.jsonl" 2>/dev/null \
  | sed -n 's/^inline:\([0-9][0-9]*\)$/\1/p')
[ -n "$FIXED_IDS" ] || { echo "resolve-threads: refused — no recorded cycle pushed ${PUSHED%"${PUSHED#???????}"} with an inline finding"; exit 1; }

GH_REST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/resolve-threads.XXXXXX") || exit 1
trap 'rm -rf "$GH_REST_DIR"' EXIT
D=$GH_REST_DIR
OWNER=${REPO%%/*}; NAME=${REPO#*/}

# Map root comment databaseId -> thread id. 100 threads cover every PR we have seen; more
# than that is refused rather than read partially.
jq -cn --arg o "$OWNER" --arg n "$NAME" --argjson pr "$PR" '{
  query: "query($o:String!,$n:String!,$pr:Int!){repository(owner:$o,name:$n){pullRequest(number:$pr){reviewThreads(first:100){totalCount nodes{id isResolved comments(first:1){nodes{databaseId}}}}}}}",
  variables: {o: $o, n: $n, pr: $pr}}' > "$D/q.json"
api_post graphql "$D/q.json" "$D/threads.json" "$D/err" \
  || { echo "resolve-threads: cannot read threads — $(head -1 "$D/err")"; exit 1; }
jq -e '.errors | not' "$D/threads.json" >/dev/null 2>&1 \
  || { echo "resolve-threads: GraphQL error — $(jq -r '.errors[0].message' "$D/threads.json")"; exit 1; }
jq -e '.data.repository.pullRequest.reviewThreads | .totalCount <= (.nodes | length)' "$D/threads.json" >/dev/null 2>&1 \
  || { echo "resolve-threads: more than 100 review threads — refusing to resolve from a partial list"; exit 1; }

RC=0
for i in $IDS; do
  printf '%s\n' "$FIXED_IDS" | grep -qx "$i" \
    || { echo "REFUSED $i not a finding the cycle at ${PUSHED%"${PUSHED#???????}"} fixed"; RC=1; continue; }
  row=$(jq -r --argjson id "$i" '.data.repository.pullRequest.reviewThreads.nodes[]
          | select(.comments.nodes[0].databaseId == $id) | "\(.id) \(.isResolved)"' "$D/threads.json" | head -1)
  if [ -z "$row" ]; then echo "SKIP $i not the root of any review thread"; continue; fi
  tid=${row% *}; resolved=${row#* }
  if [ "$resolved" = true ]; then echo "SKIP $i already resolved"; continue; fi
  jq -cn --arg t "$tid" '{query: "mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}", variables: {t: $t}}' > "$D/m.json"
  if api_post graphql "$D/m.json" "$D/r.json" "$D/err" \
     && jq -e '.data.resolveReviewThread.thread.isResolved == true' "$D/r.json" >/dev/null 2>&1; then
    echo "RESOLVED $i"
  else
    echo "FAILED $i — $(head -1 "$D/err" 2>/dev/null)$(jq -r '.errors[0].message // empty' "$D/r.json" 2>/dev/null)"; RC=1
  fi
done
exit $RC
