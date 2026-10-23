#!/bin/sh
# GitHub REST helpers shared by pr-watch.sh and resolve-pr.sh. Sourced, never run:
#   . "$(dirname "$0")/gh-rest.sh"
# Every network call goes through curl (proxy-aware, sandbox-friendly) to
# https://api.github.com — the caller runs inside the OS sandbox and needs
# `api.github.com` in its `allowedDomains`. `gh` is consulted for ONE thing only,
# the token (`gh auth token`, no network), and only when neither GH_TOKEN nor
# GITHUB_TOKEN is set. If `gh auth token` cannot run under the sandbox, export
# GH_TOKEN in the shell that launches Claude Code.
#
# Contract (POSIX sh, needs curl + jq):
#   GH_REST_DIR   set by the caller before the first call: a directory it owns with
#                 mode 0700 semantics (umask 077). The token is written there ONCE,
#                 to a 0600 header file that curl reads with `-H @file`, so the
#                 token never appears in `ps`.
#   gh_token                   -> stdout: the token; 1 when none can be resolved
#   api_get <path> <body> <err>   GET https://api.github.com/<path>; body to <body>.
#                 2xx -> 0. Otherwise <err> holds one line, `HTTP <code>: <message>`
#                 (<message> = `.message` of the JSON body, else curl's first stderr
#                 line, else "unexpected response"; a transport failure is code 000)
#                 and the return is 1. No token -> `HTTP 401: no GitHub token (set
#                 GH_TOKEN or run gh auth login)` without any network call.
#   api_post <path> <json> <body> <err>   POST the file <json> to https://api.github.com/<path>;
#                 same return and <err> contract as api_get. NOT retried: a POST is not
#                 idempotent. The only caller is resolve-threads.sh (ADR-0008 D-3b).
#   api_get_all <path> <err>   pages <path> with per_page=100 over ARRAY bodies,
#                 page=1.. until a page holds fewer than 100 items (or PAGE_CAP pages),
#                 and prints one concatenated JSON array (`jq -s 'add // []'`) on
#                 stdout. Any failed page -> <err> as above, return 1, nothing printed.

GH_REST_API=${GH_REST_API:-https://api.github.com}
GH_REST_TIMEOUT=20
GH_REST_PAGE_CAP=50       # 50 x 100 items: a bound, not a target — no PR carries more
GH_REST_HDR=""            # the header file, once resolved

gh_token() {
  if [ -n "${GH_TOKEN:-}" ]; then printf '%s' "$GH_TOKEN"; return 0; fi
  if [ -n "${GITHUB_TOKEN:-}" ]; then printf '%s' "$GITHUB_TOKEN"; return 0; fi
  _t=$(gh auth token 2>/dev/null) && [ -n "$_t" ] && { printf '%s' "$_t"; return 0; }
  return 1
}

# Resolve the token once per process and pin it in the 0600 header file. The file is
# named by the top-level pid ($$ is inherited by `$(...)` subshells, where the callers
# run api_get), so the cache is the file's presence, not a shell variable; header files
# left by processes that no longer exist are swept on the first write. Callers remove
# their own file on exit: `trap 'gh_rest_cleanup' EXIT`.
gh_rest_headers() {  # gh_rest_headers [--fresh]: --fresh always rewrites (call once at startup)
  _f="${GH_REST_DIR:-}/gh-headers.$$"
  if [ "${1:-}" != "--fresh" ]; then
    [ -n "${GH_REST_DIR:-}" ] && [ -f "$_f" ] && [ ! -L "$_f" ] && { GH_REST_HDR=$_f; return 0; }
  fi
  [ -n "${GH_REST_DIR:-}" ] && [ -d "$GH_REST_DIR" ] || { echo "gh-rest: GH_REST_DIR is not set to a directory" >&2; return 1; }
  _tok=$(gh_token) || return 1
  for _old in "$GH_REST_DIR"/gh-headers.*; do
    [ -e "$_old" ] || continue
    _pid=${_old##*.}; case "$_pid" in ''|*[!0-9]*) continue;; esac
    kill -0 "$_pid" 2>/dev/null || rm -f "$_old"
  done
  rm -f "$_f"; [ -e "$_f" ] && return 1     # never write through something that survived rm
  ( umask 077; set -C; printf 'Authorization: Bearer %s\nAccept: application/vnd.github+json\nX-GitHub-Api-Version: 2022-11-28\n' "$_tok" > "$_f" ) || return 1
  GH_REST_HDR=$_f
}
gh_rest_cleanup() { [ -n "${GH_REST_DIR:-}" ] && rm -f "$GH_REST_DIR/gh-headers.$$"; }

api_get() {  # api_get <path> <bodyfile> <errfile>
  _path=$1; _body=$2; _err=$3
  if ! gh_rest_headers; then
    echo "HTTP 401: no GitHub token (set GH_TOKEN or run gh auth login)" > "$_err"; return 1
  fi
  # --http1.1: one request per process, so h2 buys nothing and the CONNECT-proxy path has shown
  # CURLE_HTTP2 framing flakes; GETs are idempotent, so a short retry is safe.
  _code=$(curl -sS --http1.1 --retry 2 --retry-all-errors --retry-delay 1 --max-time "$GH_REST_TIMEOUT" -H "@$GH_REST_HDR" \
            -o "$_body" -w '%{http_code}' "$GH_REST_API/$_path" 2>"$_err.curl")
  _code=$(printf '%s' "$_code" | tr -cd '0-9'); [ -n "$_code" ] || _code=000
  case "$_code" in
    2[0-9][0-9]) rm -f "$_err.curl"; return 0;;
  esac
  _msg=""
  [ "$_code" != 000 ] && _msg=$(jq -r 'if type == "object" then (.message // empty) else empty end' "$_body" 2>/dev/null | head -1)
  [ -n "$_msg" ] || _msg=$(head -1 "$_err.curl" 2>/dev/null | sed 's/^curl: ([0-9]*) //')
  [ -n "$_msg" ] || _msg=$(head -c 160 "$_body" 2>/dev/null | sed 's/<[^>]*>//g' | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')
  [ -n "$_msg" ] || _msg="unexpected response"
  printf 'HTTP %s: %s\n' "$_code" "$_msg" > "$_err"
  rm -f "$_err.curl"
  return 1
}

api_post() {  # api_post <path> <jsonfile> <bodyfile> <errfile>
  _path=$1; _json=$2; _body=$3; _err=$4
  if ! gh_rest_headers; then
    echo "HTTP 401: no GitHub token (set GH_TOKEN or run gh auth login)" > "$_err"; return 1
  fi
  _code=$(curl -sS --http1.1 --max-time "$GH_REST_TIMEOUT" -H "@$GH_REST_HDR" -H 'Content-Type: application/json' \
            -X POST --data-binary "@$_json" -o "$_body" -w '%{http_code}' "$GH_REST_API/$_path" 2>"$_err.curl")
  _code=$(printf '%s' "$_code" | tr -cd '0-9'); [ -n "$_code" ] || _code=000
  case "$_code" in 2[0-9][0-9]) rm -f "$_err.curl"; return 0;; esac
  _msg=""
  [ "$_code" != 000 ] && _msg=$(jq -r 'if type == "object" then (.message // empty) else empty end' "$_body" 2>/dev/null | head -1)
  [ -n "$_msg" ] || _msg=$(head -1 "$_err.curl" 2>/dev/null | sed 's/^curl: ([0-9]*) //')
  [ -n "$_msg" ] || _msg="unexpected response"
  printf 'HTTP %s: %s\n' "$_code" "$_msg" > "$_err"
  rm -f "$_err.curl"
  return 1
}

api_get_all() {  # api_get_all <path> <errfile>  -> stdout: one JSON array
  _apath=$1; _aerr=$2
  case "$_apath" in *\?*) _sep='&';; *) _sep='?';; esac
  [ -n "${GH_REST_DIR:-}" ] && [ -d "$GH_REST_DIR" ] || { echo "gh-rest: GH_REST_DIR is not set to a directory" > "$_aerr"; return 1; }
  _pages="$GH_REST_DIR/gh-pages.$$"; _page="$GH_REST_DIR/gh-page.$$"
  : > "$_pages" || return 1
  _n=1
  while [ "$_n" -le "$GH_REST_PAGE_CAP" ]; do
    api_get "$_apath${_sep}page=$_n&per_page=100" "$_page" "$_aerr" || { rm -f "$_pages" "$_page"; return 1; }
    _len=$(jq -e 'if type == "array" then length else empty end' "$_page" 2>/dev/null) \
      || { echo "HTTP 200: page $_n is not a JSON array" > "$_aerr"; rm -f "$_pages" "$_page"; return 1; }
    cat "$_page" >> "$_pages"; printf '\n' >> "$_pages"
    [ "$_len" -lt 100 ] && break
    _n=$((_n + 1))
  done
  if [ "$_len" -ge 100 ]; then  # the cap was hit with a full page: never return a partial list as success
    echo "HTTP 200: more than $GH_REST_PAGE_CAP pages" > "$_aerr"; rm -f "$_pages" "$_page"; return 1
  fi
  jq -s 'add // []' "$_pages"; _rc=$?
  rm -f "$_pages" "$_page"
  return $_rc
}
