# Sourced by install.sh and doctor.sh — defines functions only, performs no action.
#
# `ruflo` is a thin wrapper: bin/ruflo.js walks up from its own package for
# node_modules/@claude-flow/cli and delegates everything to it, and it depends on
# `@claude-flow/cli ^3.33.0`. So `ruflo --version` reports the WRAPPER, while the
# code that runs is whatever cli npm resolved at install time: a clean
# `npx ruflo@3.38.20` ran cli 3.42.5 (2026-09-22). The engine is the pin that
# matters (business rule 15); this resolves it the way the wrapper does.

# ruflo_wrapper_dir -> the installed `ruflo` package dir (<pkg>/bin/ruflo.js ->
# <pkg>), symlinks resolved. Prints nothing (rc 1) when it cannot resolve: an
# unresolved npm bin link would name the npm PREFIX, never the package.
ruflo_wrapper_dir() {
  local bin real
  bin="$(command -v ruflo 2>/dev/null)" || return 1
  real="$(readlink -f "$bin" 2>/dev/null)" \
    || real="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$bin" 2>/dev/null)" \
    || return 1
  [ -n "$real" ] || return 1
  # Only the npm wrapper itself: <pkg>/bin/ruflo.js with package name "ruflo".
  # A pnpm/asdf/user shim resolves elsewhere, and its grandparent (~/Library,
  # $HOME) must never be mistaken for a package — callers act on this path.
  case "$real" in */bin/ruflo.js) ;; *) return 1 ;; esac
  local pkg; pkg="$(dirname "$(dirname "$real")")"
  grep -q '"name"[[:space:]]*:[[:space:]]*"ruflo"' "$pkg/package.json" 2>/dev/null || return 1
  printf '%s\n' "$pkg"
}

# ruflo_engine_dir -> the @claude-flow/cli package dir the installed wrapper runs.
# Prints nothing (rc 1) when ruflo is absent or the engine cannot be found.
ruflo_engine_dir() {
  local dir i
  dir="$(ruflo_wrapper_dir)" || return 1
  # The wrapper's own walk, 10 levels max.
  i=0
  while [ "$i" -lt 10 ]; do
    if [ -f "$dir/node_modules/@claude-flow/cli/package.json" ]; then
      printf '%s\n' "$dir/node_modules/@claude-flow/cli"
      return 0
    fi
    [ "$dir" = "/" ] && break
    dir="$(dirname "$dir")"
    i=$((i + 1))
  done
  return 1
}

# ruflo_engine_version -> the version in that package.json, or nothing (rc 1).
ruflo_engine_version() {
  local d
  d="$(ruflo_engine_dir)" || return 1
  sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$d/package.json" | head -1
}
