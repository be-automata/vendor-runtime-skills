#!/bin/sh
# Resolve the /pr-sync argument to an exact owner/repo + PR number, then confirm
# the PR exists. Never guesses: anything that is not one of the three accepted
# forms is refused with a usage line on stderr and exit 2.
#
#   resolve-pr.sh "#120"                         -> repo from the cwd git remote
#   resolve-pr.sh "120"                          -> repo from the cwd git remote
#   resolve-pr.sh "https://github.com/o/r/pull/230"  -> owner/repo from the URL
#
# Prints shell-sourceable KEY=VALUE lines on stdout:
#   REPO=owner/name  PR=120  STATE=OPEN|MERGED|CLOSED  DRAFT=true|false
#   HEAD=<sha>  URL=<html url>  TITLE=<title>
# Exit codes: 0 resolved, 1 the PR or repo could not be reached, 2 bad argument.
#
# The repo for the number forms comes from `git remote get-url origin` (github.com over
# ssh, ssh:// or https only); the PR is confirmed with one curl read of api.github.com
# through gh-rest.sh, so the script works sandboxed (`api.github.com` in allowedDomains).
# The token comes from GH_TOKEN, else GITHUB_TOKEN, else `gh auth token`.
set -u
umask 077
. "$(dirname "$0")/gh-rest.sh"

USAGE='usage: /pr-sync <#number | number | https://github.com/<owner>/<repo>/pull/<number>>'

refuse() { echo "$USAGE" >&2; [ -n "${1:-}" ] && echo "refused: $1" >&2; exit 2; }

[ $# -eq 1 ] || refuse "exactly one argument is expected, got $#"
ARG=$(printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
[ -n "$ARG" ] || refuse "empty argument"
case "$ARG" in *[[:space:]]*) refuse "'$ARG' contains whitespace — one PR per call";; esac

case "$ARG" in
  \#[1-9]*|[1-9]*)
    NUM=${ARG#\#}
    case "$NUM" in *[!0-9]*) refuse "'$ARG' is not a PR number";; esac
    no_clone() { echo "refused: the current directory is not inside a GitHub repository clone — pass the full PR URL" >&2; exit 1; }
    ORIGIN=$(git remote get-url origin 2>/dev/null) || no_clone
    case "$ORIGIN" in
      git@github.com:*)       REST=${ORIGIN#git@github.com:};;
      ssh://git@github.com/*) REST=${ORIGIN#ssh://git@github.com/};;
      https://github.com/*)   REST=${ORIGIN#https://github.com/};;
      *) no_clone;;
    esac
    REST=${REST%/}; REST=${REST%.git}; REST=${REST%/}
    case "$REST" in ""|*/*/*|/*|*/) no_clone;; */*) ;; *) no_clone;; esac
    OWNER=${REST%%/*}; NAME=${REST#*/}
    case "$OWNER" in .|..|-*|*[!A-Za-z0-9_.-]*) no_clone;; esac
    case "$NAME"  in .|..|-*|*[!A-Za-z0-9_.-]*) no_clone;; esac
    REPO="$OWNER/$NAME"
    ;;
  *github.com/*/pull/*)
    # scheme and "www." optional; trailing path, query or fragment tolerated
    REST=${ARG#*github.com/}
    OWNER=${REST%%/*}; REST=${REST#*/}
    NAME=${REST%%/*};  REST=${REST#*/}
    case "$REST" in pull/*) ;; *) refuse "'$ARG' is not a pull-request URL";; esac
    NUM=${REST#pull/}
    NUM=$(printf '%s' "$NUM" | sed 's/[/?#].*$//')
    case "$OWNER" in ""|.|..|-*|*[!A-Za-z0-9_.-]*) refuse "'$ARG' has an invalid owner";; esac
    case "$NAME"  in ""|.|..|-*|*[!A-Za-z0-9_.-]*) refuse "'$ARG' has an invalid repository";; esac
    case "$NUM"   in ""|*[!0-9]*|0*) refuse "'$ARG' has an invalid PR number";; esac
    case "$ARG" in
      https://github.com/*|http://github.com/*|https://www.github.com/*|http://www.github.com/*|github.com/*|www.github.com/*) ;;
      *) refuse "'$ARG' is not a github.com URL";;
    esac
    REPO="$OWNER/$NAME"
    ;;
  *) refuse "'$ARG' matches none of the accepted forms" ;;
esac

# Templated on purpose: on macOS a bare `mktemp -d` ignores $TMPDIR and uses
# confstr(_CS_DARWIN_USER_TEMP_DIR) -> /var/folders/..., which a sandboxed shell
# cannot write, so the resolver died here before doing anything. Every other
# mktemp in this repo already uses this form.
GH_REST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pr-sync.XXXXXX") || exit 1
trap 'rm -rf "$GH_REST_DIR"' EXIT
api_get "repos/$REPO/pulls/$NUM" "$GH_REST_DIR/pull.json" "$GH_REST_DIR/err" \
  || { echo "refused: cannot read $REPO#$NUM — $(head -1 "$GH_REST_DIR/err")" >&2; exit 1; }

printf 'REPO=%s\nPR=%s\n' "$REPO" "$NUM"
jq -r '
  "STATE=\(if .merged then "MERGED" elif .state == "open" then "OPEN" else "CLOSED" end)",
  "DRAFT=\(.draft // false)",
  "HEAD=\(.head.sha)",
  "URL=\(.html_url)",
  "TITLE=\((.title // "") | gsub("[\\n\\r]"; " ") | @sh)"' "$GH_REST_DIR/pull.json"
