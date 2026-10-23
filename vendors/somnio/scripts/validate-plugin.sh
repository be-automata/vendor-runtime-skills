#!/bin/sh
# `claude plugin validate` reads component directories WITHOUT following symlinks:
#   "This directory is a symlink and nothing in it was read … A session loading this
#    plugin does follow it, so validate the real directory separately."
# The repo mandates symlinks (a copy is a validation error), and a session follows them,
# so the plugin is correct as shipped. To validate the real content strictly we
# materialise a dereferenced copy in a temp dir and validate that.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# -L dereferences symlinks so the component dirs become real in the staged copy.
cp -RL "$ROOT/plugins/somnio-engineering-ai/." "$STAGE/"

echo "== manifest + symlinked layout (as shipped) =="
# NOT piped into tail: `set -e` sees the pipeline's status, which is tail's, so a
# failure here used to be invisible. Capture, then report.
if OUT=$(claude plugin validate "$ROOT/plugins/somnio-engineering-ai" 2>&1); then
  printf '%s\n' "$OUT" | tail -3
else
  printf '%s\n' "$OUT"
  echo "non-strict validation of the shipped layout FAILED" >&2
  exit 1
fi

echo "== real component content (dereferenced), --strict =="
claude plugin validate "$STAGE" --strict
