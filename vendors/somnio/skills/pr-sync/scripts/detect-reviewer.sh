#!/bin/sh
# Evidence for the PR reviewer question: does a review bot actually review this
# repository's PRs? Read-only. The output is shown to the person BEFORE they answer
# (harness-init Step 6b, pr-sync Step 1b); it is never recorded on its own.
#
#   detect-reviewer.sh --repo owner/name [--bot automata-ai-bot] [--sample 10]
#
# Reads, through gh-rest.sh (curl, sandbox-safe, `api.github.com` in allowedDomains):
#   pulls?state=all          the most recently updated --sample PRs, open or closed
#   pulls/<n>/reviews        per sampled PR: reviews by the bot login
#   issues/<n>/comments      per sampled PR: comments by the bot login
#   repos/<repo>             the default branch
#   rules/branches/<branch>  required_approving_review_count from rulesets (read access)
# Classic branch protection needs admin to read, so it is not consulted; no ruleset
# with a pull_request rule prints REQUIRED_APPROVALS=unknown, never 0.
#
# Prints shell-sourceable KEY=VALUE lines:
#   BOT=<login> PRS_SAMPLED=<n> BOT_PRS=<k> BOT_REVIEWS=<m> LAST_BOT_REVIEW=<iso|none>
#   BOT_REQUESTED_OPEN=<k>        open PRs with the bot in requested_reviewers
#   REQUIRED_APPROVALS=<n|unknown>
#   SUGGEST=automata|collaborators|unknown   unknown = no PRs to learn from (greenfield)
#   SUGGEST never names automata_only: its evidence is whether the bot reviewed, which cannot
#   separate "Automata + collaborators" from "Automata only". The caller pre-selects the wider
#   mode and the person narrows it. Do not invent a heuristic here — no collaborator review in
#   the sample is not a policy.
#   EVIDENCE='<one line>'         what record_pr_review.py --evidence takes
# Exit: 0 printed · 1 the API could not be read · 2 bad arguments.
set -u
umask 077
. "$(dirname "$0")/gh-rest.sh"

USAGE='usage: detect-reviewer.sh --repo owner/name [--bot LOGIN] [--sample N]'
REPO=""; BOT=automata-ai-bot; SAMPLE=10
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }; REPO=$2; shift 2;;
    --bot)    [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }; BOT=$2; shift 2;;
    --sample) [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }; SAMPLE=$2; shift 2;;
    *) echo "detect-reviewer: unknown argument '$1'" >&2; echo "$USAGE" >&2; exit 2;;
  esac
done
case "$REPO" in
  */*/*|/*|*/|*[!A-Za-z0-9_./-]*|.*|*/.*|-*|*/-*|'') echo "detect-reviewer: --repo must be owner/name, got '$REPO'" >&2; exit 2;;
  */*) ;;
  *) echo "detect-reviewer: --repo must be owner/name, got '$REPO'" >&2; exit 2;;
esac
case "$BOT" in ''|*[!A-Za-z0-9_.\[\]-]*|any|none) echo "detect-reviewer: --bot must be a GitHub login" >&2; exit 2;; esac
case "$SAMPLE" in ''|*[!0-9]*|0*|???*) echo "detect-reviewer: --sample must be an integer from 1 to 99" >&2; exit 2;; esac
for tool in curl jq; do command -v "$tool" >/dev/null 2>&1 || { echo "detect-reviewer: $tool is required" >&2; exit 2; }; done

GH_REST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/detect-reviewer.XXXXXX") || exit 1
trap 'rm -rf "$GH_REST_DIR"' EXIT
D=$GH_REST_DIR
fail() { echo "detect-reviewer: cannot read $1 — $(head -1 "$D/err" 2>/dev/null)" >&2; exit 1; }

api_get "repos/$REPO/pulls?state=all&sort=updated&direction=desc&per_page=$SAMPLE" "$D/pulls.json" "$D/err" || fail "the PR list"
jq -e 'type == "array"' "$D/pulls.json" >/dev/null 2>&1 || { echo "x" > "$D/err"; fail "the PR list (not an array)"; }
# The login filter, defined once: the bot with or without the "[bot]" suffix Apps carry.
ISBOT='def norm: ascii_downcase | sub("\\[bot\\]$"; ""); def isbot: ((.user.login // "") | norm) == ($bot | norm);'
BOT_REQ=$(jq --arg bot "$BOT" "$ISBOT"'[.[] | select(.state == "open") | select([.requested_reviewers[]? | {user: .} | select(isbot)] | length > 0)] | length' "$D/pulls.json")
: > "$D/per-pr"
for n in $(jq -r '.[].number | select(type == "number")' "$D/pulls.json"); do
  api_get "repos/$REPO/pulls/$n/reviews?per_page=100" "$D/rev.json" "$D/err" || fail "reviews of #$n"
  api_get "repos/$REPO/issues/$n/comments?per_page=100" "$D/com.json" "$D/err" || fail "comments of #$n"
  jq -c -n --arg bot "$BOT" --slurpfile r "$D/rev.json" --slurpfile c "$D/com.json" "$ISBOT"'
    ([$r[0][]? | select(isbot)]) as $br
    | {reviews: ($br | length),
       comments: ([$c[0][]? | select(isbot)] | length),
       last: ([$br[] | .submitted_at // empty] | max)}' >> "$D/per-pr" \
    || { echo "unparseable reviews/comments" > "$D/err"; fail "#$n"; }
done

REQ=unknown
if api_get "repos/$REPO" "$D/repo.json" "$D/err"; then
  BR=$(jq -r '.default_branch // empty' "$D/repo.json")
  case "$BR" in
    ''|*[!A-Za-z0-9_./-]*) ;;
    *) if api_get "repos/$REPO/rules/branches/$BR" "$D/rules.json" "$D/err"; then
         REQ=$(jq -r '[.[]? | select(.type == "pull_request") | .parameters.required_approving_review_count // empty]
                      | if length == 0 then "unknown" else max | tostring end' "$D/rules.json" 2>/dev/null || echo unknown)
       fi;;
  esac
fi

jq -r -s --arg bot "$BOT" --argjson req "$(printf '%s' "$REQ" | jq -R 'tonumber? // .')" --argjson breq "$BOT_REQ" '
  length as $n
  | ([.[] | select(.reviews > 0 or .comments > 0)] | length) as $k
  | ([.[] | .reviews] | add // 0) as $m
  | ([.[] | .last // empty] | max // "none") as $last
  | (if $n == 0 then "unknown" elif $k > 0 or $breq > 0 then "automata" else "collaborators" end) as $sug
  | "BOT=\($bot)", "PRS_SAMPLED=\($n)", "BOT_PRS=\($k)", "BOT_REVIEWS=\($m)",
    "LAST_BOT_REVIEW=\($last)", "BOT_REQUESTED_OPEN=\($breq)", "REQUIRED_APPROVALS=\($req)",
    "SUGGEST=\($sug)",
    "EVIDENCE=" + (((if $n == 0 then "no PRs yet to learn from"
       else "\($bot) reviewed or commented on \($k) of the last \($n) PRs (last review: \($last))" end)
       + "; \($breq) open PR(s) request \($bot); required approvals: \($req)") | @sh)' "$D/per-pr"
