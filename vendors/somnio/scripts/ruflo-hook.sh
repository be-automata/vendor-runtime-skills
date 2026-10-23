#!/bin/sh
# Fire-and-forget bridge from Claude Code hook events to ruflo's learning
# hooks. One wrapper for every event so the guard pattern lives in one place.
# Advisory-only by design: the ruflo call is backgrounded and detached, its
# output and exit code are discarded, and this wrapper always exits 0 — it can
# never block a tool call. Its only synchronous cost is one short python3 run
# that reads the payload (tens of ms). (It feeds ruflo's learning and
# session-state persistence; it is not a gate.)
# Usage: ruflo-hook.sh <pre-command|post-edit|session-end>   (payload on stdin)
#
# The payload is Claude Code's hook JSON. `pre-command` REQUIRES -c <command>
# and `post-edit` wants -f <file>; called bare, pre-command failed with
# "Required option missing" and post-edit recorded every edit as "unknown", so
# the learning loop (ENG-HARNESS-03) recorded next to nothing. A command is
# passed as a routing SUMMARY (`git commit`, `npm run`), never its text: see
# ruflo-hook-arg.py. Without python3 to read the payload, the event is skipped
# rather than sent empty.
command -v ruflo >/dev/null 2>&1 || exit 0
EVENT="${1:-}"
case "$EVENT" in
  pre-command|post-edit)
    command -v python3 >/dev/null 2>&1 || exit 0
    # A summary of tool_input.command for Bash; tool_input.file_path for edits.
    ARG="$(PYTHONDONTWRITEBYTECODE=1 python3 "$(dirname "$0")/ruflo-hook-arg.py" \
      "$( [ "$EVENT" = pre-command ] && echo command || echo file_path )" 2>/dev/null)"
    [ -n "$ARG" ] || exit 0
    if [ "$EVENT" = pre-command ]; then
      ruflo hooks pre-command -c "$ARG" >/dev/null 2>&1 &
    else
      ruflo hooks post-edit -f "$ARG" -s true >/dev/null 2>&1 &
    fi
    ;;
  *)
    ruflo hooks "$EVENT" >/dev/null 2>&1 &
    ;;
esac
exit 0
