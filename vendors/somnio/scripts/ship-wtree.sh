#!/bin/sh
# ship-wtree.sh — print a working-tree CONTENT fingerprint (a git tree hash).
# Single source of truth for the ship gate's freshness rule; used by both
# ship-marker.sh (stamps markers) and ship-gate.sh (grades them).
#
# Recipe (benchmarked from gstack's bin/gstack-wtree): stage the full working
# tree into a TEMP index (git add -A, so gitignored scratch stays out and
# untracked source is included) and print git write-tree of that index.
# Keystone properties, vs the HEAD tree:
#   - Committing identical content does NOT change the fingerprint, so
#     evidence recorded on a dirty tree stays valid after the exact same
#     content is committed.
#   - An untracked new source file DOES change it, so "verification ran"
#     cannot stay fresh after new code appears.
#   - Rebase/amend/squash that preserve content do not change it.
#
# The real repo index is never touched. Honest side effect: blobs of
# untracked, non-ignored files enter .git/objects until gc — the same
# property git stash -u has.
#
# Performance: the temp index is seeded by COPYING the real index (git writes
# it atomically via rename, so the copy is a consistent snapshot), which
# preserves the stat cache — git add -A then re-hashes only files whose stat
# changed. Fallback: read-tree HEAD when the copy is unavailable.
#
# Exit 1 outside a git repo or in a repo with no commits — callers treat
# that as "no fingerprint".
set -eu

TOP=$(git rev-parse --show-toplevel 2>/dev/null) || exit 1
# Resolve the REAL index path BEFORE exporting GIT_INDEX_FILE — with the var
# already set, --git-path returns the temp index and the stat-cache seed
# becomes a dead self-copy.
REAL_INDEX=$(git -C "$TOP" rev-parse --git-path index 2>/dev/null || true)
case "$REAL_INDEX" in
  /*) ;;
  ?*) REAL_INDEX="$TOP/$REAL_INDEX" ;;
esac
TMPIDX=$(mktemp "${TMPDIR:-/tmp}/somnio-wtree-XXXXXX")
trap 'rm -f "$TMPIDX"' EXIT
export GIT_INDEX_FILE="$TMPIDX"
if [ -n "$REAL_INDEX" ] && [ -f "$REAL_INDEX" ]; then
  cp "$REAL_INDEX" "$TMPIDX" 2>/dev/null || git -C "$TOP" read-tree HEAD 2>/dev/null || exit 1
else
  git -C "$TOP" read-tree HEAD 2>/dev/null || exit 1
fi
git -C "$TOP" add -A
git -C "$TOP" write-tree
