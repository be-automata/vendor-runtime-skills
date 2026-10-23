#!/bin/sh
# Records run evidence for the ship gate. Two modes:
#
#   ship-marker.sh run --label <name> -- <cmd> [args...]
#     RECEIPT mode (preferred): runs the real verification command with its
#     output flowing through untouched, then records the marker FROM that
#     execution — command, cmd_sha256, exit code, duration, and the wtree
#     content fingerprint of the tree it ran against.
#     TRANSPARENCY INVARIANT (load-bearing): the wrapper exit code is ALWAYS
#     the child exit code. Every marker-write failure is a stderr warning,
#     never a failure — this plumbing must never turn green tests red.
#     A marker whose recorded exit is nonzero is anti-evidence: the gate
#     rejects it.
#
#   ship-marker.sh <name>
#     CLAIM mode (transitional): records a bare claim marker with the current
#     wtree. Accepted by the gate only for requirements not flagged
#     "receipt": true in .claude/ship-gate.json.
#
# Markers live under $GIT_DIR/somnio-ship-markers/ — machine-local by
# construction, never committable, no .gitignore entry needed.
set -u

SCRIPTS_DIR=$(cd "$(dirname "$0")" && pwd)

valid_name() {
  case "$1" in
    "") return 1 ;;
    *[!A-Za-z0-9._-]*) return 1 ;;
    *) return 0 ;;
  esac
}

write_marker() { # write_marker <name> <mode> <exit> <duration> <cmd...>
  M_NAME="$1"; M_MODE="$2"; M_EXIT="$3"; M_DUR="$4"; shift 4
  # COMMON git dir so markers recorded in any linked worktree are visible
  # to pushes from every other one; the wtree fingerprint keeps sharing
  # safe (content mismatch grades STALE). --path-format=absolute needs
  # git >= 2.31; fall back to cd/pwd resolution.
  M_GIT_DIR=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    || M_GIT_DIR=$(CDPATH= cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd) || {
    echo "ship-marker: warning: not inside a git repository — marker not recorded" >&2
    return 1
  }
  # Strip JSON-hostile characters: git ref names may legally contain a
  # double quote, and the no-python3 printf fallback interpolates verbatim.
  # `git rev-parse` prints a value on STDOUT even when it fails: with no
  # resolvable HEAD it prints the literal string "HEAD" and exits nonzero. So
  # `$(git rev-parse HEAD || echo none)` CONCATENATES both and records
  # "HEAD\nnone" — assign first, substitute the fallback on a nonzero exit.
  # The literal `none` is load-bearing: ship-gate.sh's head_sensitive check
  # reads it as "no commit recorded". Changing it means changing that check.
  M_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || M_BRANCH=HEAD
  M_BRANCH=$(printf '%s' "$M_BRANCH" | tr -d '\042\134\n')
  M_HEAD=$(git rev-parse HEAD 2>/dev/null) || M_HEAD=none
  M_WTREE=$(sh "$SCRIPTS_DIR/ship-wtree.sh" 2>/dev/null || echo "")
  mkdir -p "$M_GIT_DIR/somnio-ship-markers" 2>/dev/null || {
    echo "ship-marker: warning: cannot create marker directory — marker not recorded" >&2
    return 1
  }
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, os, sys, time
name, mode, exit_code, dur, branch, head, wtree, out = sys.argv[1:9]
cmd_argv = sys.argv[9:]
rec = {"name": name, "ts": int(time.time()), "branch": branch,
       "head": head, "wtree": wtree, "mode": mode}
if mode == "run":
    import hashlib
    command = " ".join(cmd_argv)
    rec["command"] = command
    rec["cmd_sha256"] = hashlib.sha256(command.encode()).hexdigest()
    rec["exit"] = int(exit_code)
    rec["duration_s"] = int(dur)
# Atomic write: a gate evaluation racing this rewrite must never read a
# truncated marker. The temp name carries the pid because markers live in the
# COMMON git dir — two worktrees recording the same label concurrently would
# otherwise share one temp path and truncate or rename each other.
tmp = out + ".tmp." + str(os.getpid())
with open(tmp, "w") as f:
    json.dump(rec, f)
    f.write("\n")
os.replace(tmp, out)
' "$M_NAME" "$M_MODE" "$M_EXIT" "$M_DUR" "$M_BRANCH" "$M_HEAD" "$M_WTREE" \
      "$M_GIT_DIR/somnio-ship-markers/$M_NAME.json" "$@" 2>/dev/null || {
      echo "ship-marker: warning: marker write failed — marker not recorded" >&2
      return 1
    }
  elif [ "$M_MODE" = "claim" ]; then
    # Claim-mode values are shell-safe (shas, validated name, branch); run
    # mode carries arbitrary command strings and needs real JSON encoding.
    printf '{"name":"%s","ts":%s,"branch":"%s","head":"%s","wtree":"%s","mode":"claim"}\n' \
      "$M_NAME" "$(date +%s)" "$M_BRANCH" "$M_HEAD" "$M_WTREE" \
      > "$M_GIT_DIR/somnio-ship-markers/$M_NAME.json.tmp.$$" 2>/dev/null \
      && mv "$M_GIT_DIR/somnio-ship-markers/$M_NAME.json.tmp.$$" \
            "$M_GIT_DIR/somnio-ship-markers/$M_NAME.json" 2>/dev/null || {
      echo "ship-marker: warning: marker write failed — marker not recorded" >&2
      return 1
    }
  else
    echo "ship-marker: warning: python3 unavailable — run-mode marker not recorded" >&2
    return 1
  fi
  SHORT_WTREE=$(printf '%.12s' "$M_WTREE")
  echo "ship-marker: recorded '$M_NAME' ($M_MODE, branch $M_BRANCH, wtree ${SHORT_WTREE:-none})"
  return 0
}

if [ "${1:-}" = "run" ]; then
  shift
  NAME=""
  [ "${1:-}" = "--label" ] && { NAME="${2:-}"; shift 2 2>/dev/null || true; }
  if ! valid_name "${NAME:-}" || [ "${1:-}" != "--" ]; then
    echo "usage: ship-marker.sh run --label <marker-name> -- <cmd> [args...]" >&2
    echo "       (marker name charset: A-Za-z0-9._-)" >&2
    exit 1
  fi
  shift
  [ $# -ge 1 ] || { echo "usage: ship-marker.sh run --label <name> -- <cmd> [args...]" >&2; exit 1; }
  START=$(date +%s)
  "$@"
  STATUS=$?
  END=$(date +%s)
  write_marker "$NAME" run "$STATUS" $((END - START)) "$@" || true
  exit "$STATUS"
fi

NAME="${1:-}"
if ! valid_name "$NAME"; then
  echo "usage: ship-marker.sh <marker-name>  |  ship-marker.sh run --label <name> -- <cmd>..." >&2
  echo "       (marker name charset: A-Za-z0-9._-)" >&2
  exit 1
fi
write_marker "$NAME" claim 0 0
