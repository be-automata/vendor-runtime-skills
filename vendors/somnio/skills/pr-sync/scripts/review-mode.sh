#!/bin/sh
# Which reviewers this repository's PRs wait for — the arming flags for pr-watch.sh.
# Reads the `pr_review` block of <root>/.claude/harness.json (written by harness-init's
# record_pr_review.py). The one reader for pr-sync Step 2, dev-execution Step 5 and
# loop-mode Step 10, so the three callers never disagree about the mode.
#
#   review-mode.sh [<root>]      <root> defaults to the cwd's git toplevel
#
# Prints shell-sourceable KEY=VALUE lines:
#   REVIEWER=automata|automata_only|collaborators|none|unrecorded
#   BOT=<login>|none             pass as --bot
#   MIN_APPROVALS=<n>            pass as --min-approvals (only when BOT=none); 0 for REVIEWER=none
#   SOURCE=harness.json|unrecorded
#   WATCH=on|off                 off only for REVIEWER=none: arm no watch, report CI once
#   WATCH_FLAGS='<flags>'        the exact pr-watch.sh flags, ready to splice; empty when WATCH=off
# automata_only is automata with `--collaborators off`: the bot's approval at the head is
# still the verdict, and a collaborator's COMMENTED review is no longer worked. Their
# CHANGES_REQUESTED is reported in both modes — GitHub blocks on it either way.
# Unrecorded (no manifest, or no block) prints today's default — Automata — with
# SOURCE=unrecorded: the caller must ask the person (pr-sync Step 1b), never assume.
# Exit: 0 printed · 3 the record is malformed, or it was written by a NEWER plugin that
# knows a reviewer value this one does not (nothing printed; the message names it).
# Same rules as _common.classify_pr_review in skills/harness-init/scripts; both are
# tested against the same shapes.
set -u
command -v jq >/dev/null 2>&1 || { echo "review-mode: jq is required" >&2; exit 2; }
ROOT=${1:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
MAN="$ROOT/.claude/harness.json"
DEFAULT_BOT=automata-ai-bot

unrecorded() {
  printf 'REVIEWER=unrecorded\nBOT=%s\nMIN_APPROVALS=1\nSOURCE=unrecorded\nWATCH=on\n' "$DEFAULT_BOT"
  printf "WATCH_FLAGS='--bot %s'\n" "$DEFAULT_BOT"
  exit 0
}
[ -f "$MAN" ] || unrecorded
OUT=$(jq -r --arg def "$DEFAULT_BOT" '
  (.pr_review // null) as $b
  | if ($b | type) != "object" or ($b | length) == 0 then "UNRECORDED"
    elif ($b.reviewer | IN("automata", "automata_only", "collaborators", "none") | not)
      then "ERR reviewer is \($b.reviewer | tojson) (or this record was written by a newer plugin)"
    elif (($b.who // "") | tostring | test("\\S") | not) then "ERR the record names no who"
    elif $b.reviewer == "none" and ($b | has("bot_login") or has("min_approvals"))
      then "ERR a none record names a reviewer rule"
    elif $b.reviewer == "none" then "OK none none 0"
    elif ((($b.min_approvals // 1) | type) != "number") or (($b.min_approvals // 1) | (. != floor or . < 1 or . > 99))
      then "ERR min_approvals is \($b.min_approvals | tojson)"
    elif $b.reviewer == "collaborators" and ($b | has("bot_login")) then "ERR a collaborators record names a bot_login"
    elif ($b.reviewer | IN("automata", "automata_only")) and ((($b.bot_login // $def) | type) != "string"
         or (($b.bot_login // $def) | test("^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\\[bot\\])?$") | not))
      then "ERR bot_login is \($b.bot_login | tojson)"
    elif $b.reviewer | IN("automata", "automata_only")
      then "OK \($b.reviewer) \($b.bot_login // $def) \($b.min_approvals // 1)"
    else "OK collaborators none \($b.min_approvals // 1)" end' "$MAN" 2>/dev/null) \
  || { echo "review-mode: $MAN is not readable JSON" >&2; exit 3; }
case "$OUT" in
  UNRECORDED) unrecorded;;
  ERR*) echo "review-mode: malformed pr_review in $MAN — ${OUT#ERR } (fix: record_pr_review.py --record)" >&2; exit 3;;
esac
set -- $OUT   # OK <reviewer> <bot> <min>
printf 'REVIEWER=%s\nBOT=%s\nMIN_APPROVALS=%s\nSOURCE=harness.json\n' "$2" "$3" "$4"
if [ "$2" = none ]; then
  printf "WATCH=off\nWATCH_FLAGS=''\n"
elif [ "$3" = none ]; then
  printf 'WATCH=on\n'
  printf "WATCH_FLAGS='--bot none --min-approvals %s'\n" "$4"
elif [ "$2" = automata_only ]; then
  printf "WATCH=on\nWATCH_FLAGS='--bot %s --collaborators off'\n" "$3"
else
  printf "WATCH=on\nWATCH_FLAGS='--bot %s'\n" "$3"
fi
