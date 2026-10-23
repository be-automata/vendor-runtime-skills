#!/usr/bin/env bash
# Lay the harness's target-repo settings down in the CURRENT repository, safely.
#
# A plugin cannot ship permissions, so a fully-harnessed repo must commit its
# own .claude/settings.json (the secret deny list + the Bash sandbox block that
# makes that deny OS-enforced; see docs/TARGET-REPO-SETUP.md). This script
# writes that file for the repo instead of a person pasting it by hand.
#
# It MERGES rather than overwrites: base = the shipped template, overlay = the
# repo's existing settings, so values the repo already tuned (extra allow rules,
# a wider allowedDomains) are kept and only the missing template keys are added.
# Re-running is a no-op once the repo already carries the template.
#
# Between those two it lays down the DETECTED network policy. A repo's own
# dependencies decide which hosts it must reach and which CLIs cannot be made
# proxy-aware, so skills/harness-init/scripts/detect_network.py reads the repo
# and emits a settings_fragment that is merged ON TOP of the template and UNDER
# the repo's own settings. Final precedence: template, then fragment, then repo.
# The detector is best effort by design — absent, no uv, a non-zero exit, no
# JSON, or a settings_fragment that is not a non-empty object all leave the
# outcome byte-identical to what this script produced before it existed.
#
# It never commits: the merged file is left in the working tree for a person to
# review and commit by PR, matching the harness policy for .claude/ edits.
#
# Requires: python3, and a clean .claude/settings.json (refuses to write over
# uncommitted changes to it, so it can never bury a developer's edit).

set -euo pipefail

log() { printf "[harness] %s\n" "$*"; }

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEMPLATE="${SOMNIO_SETTINGS_TEMPLATE:-$SCRIPT_DIR/../templates/target-settings.json}"

# The network detector, as installed (the plugin's skills/ holds symlinks) or in
# a repo checkout. Empty when neither path resolves; the run then behaves
# exactly as it did before the detector existed.
DETECTOR=""
for _d in "${SOMNIO_NETWORK_DETECTOR:-}" \
          "$SCRIPT_DIR/../skills/harness-init/scripts/detect_network.py" \
          "$SCRIPT_DIR/../../../skills/harness-init/scripts/detect_network.py"; do
  if [ -n "$_d" ] && [ -f "$_d" ]; then DETECTOR="$_d"; break; fi
done

command -v python3 >/dev/null 2>&1 || {
  log "ERROR: python3 not found — needed to merge the settings template."; exit 1; }
[ -f "$TEMPLATE" ] || {
  log "ERROR: settings template not found at $TEMPLATE"; exit 1; }

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  log "ERROR: not inside a git repository."; exit 1; }
cd "$REPO_ROOT"

TARGET=".claude/settings.json"

# Refuse to run over uncommitted changes to the target: the merge rewrites it,
# and we must never overwrite work a person has not committed.
if [ -f "$TARGET" ] && ! git diff --quiet -- "$TARGET" 2>/dev/null; then
  log "ERROR: $TARGET has uncommitted changes. Commit or discard them first."
  exit 1
fi

mkdir -p .claude
BEFORE=""
[ -f "$TARGET" ] && BEFORE="$(cat "$TARGET")"

# base = template, overlay = existing repo settings (or {} when absent).
# The template comment is a scaffolding note, not repo config — drop it.
TMP="$(mktemp .claude/.settings-XXXXXX.tmp)"
FRAG=""
NETJSON=""
trap 'rm -f "$TMP" ${FRAG:+"$FRAG"} ${NETJSON:+"$NETJSON"}' EXIT
OVERLAY="/dev/null"
[ -f "$TARGET" ] && OVERLAY="$TARGET"

# --- detected network policy (best effort; never changes the exit code) --------
FRAGMENT="/dev/null"
if [ -n "$DETECTOR" ] && command -v uv >/dev/null 2>&1; then
  NETJSON="$(mktemp .claude/.netjson-XXXXXX.tmp)"
  if uv run --script "$DETECTOR" --root "$REPO_ROOT" > "$NETJSON" 2>/dev/null && [ -s "$NETJSON" ]; then
    FRAG="$(mktemp .claude/.netfrag-XXXXXX.tmp)"
    NET_REPORT="$(python3 - "$NETJSON" "$FRAG" <<'PYEOF' 2>/dev/null
import json, sys

try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if not isinstance(d, dict):
    sys.exit(1)
frag = d.get("settings_fragment")
if not isinstance(frag, dict) or not frag:
    sys.exit(1)
json.dump(frag, open(sys.argv[2], "w"), indent=2)


def entries(key, field):
    out = []
    for e in d.get(key) or []:
        if isinstance(e, dict):
            ident = e.get("id")
            if ident:
                out.append((str(ident), str(e.get(field) or "")))
        elif e:
            out.append((str(e), ""))
    return out


vendors = entries("vendors", "confidence")
if vendors:
    print("VENDORS " + ", ".join(
        "%s (%s)" % (i, c) if c else i for i, c in vendors))
for ident, sev in entries("gaps", "severity"):
    print("GAP " + ("%s [%s]" % (ident, sev) if sev else ident))
PYEOF
)" || NET_REPORT=""
    if [ -s "$FRAG" ]; then
      FRAGMENT="$FRAG"
      while IFS= read -r _line; do
        case "$_line" in
          "VENDORS "*) log "network policy seeded from this repo's dependencies: ${_line#VENDORS }" ;;
          "GAP "*)     log "network gap this settings file cannot close: ${_line#GAP }" ;;
        esac
      done <<< "$NET_REPORT"
    fi
  fi
fi

python3 "$SCRIPT_DIR/merge-settings.py" "$TEMPLATE" "$FRAGMENT" \
  | python3 "$SCRIPT_DIR/merge-settings.py" - "$OVERLAY" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); d.pop("_comment", None); json.dump(d, sys.stdout, indent=2); sys.stdout.write("\n")' \
  > "$TMP"

if [ "$BEFORE" = "$(cat "$TMP")" ]; then
  log "$TARGET already carries the harness settings — nothing to do."
  exit 0
fi

mv "$TMP" "$TARGET"
rm -f ${FRAG:+"$FRAG"} ${NETJSON:+"$NETJSON"}
trap - EXIT
if [ -z "$BEFORE" ]; then
  log "wrote $TARGET from the harness template."
else
  log "merged the harness template into the existing $TARGET (existing values kept)."
fi
log "Review it and commit by PR — this script does not commit."
if command -v git >/dev/null 2>&1; then
  git --no-pager diff -- "$TARGET" 2>/dev/null | sed -n '1,40p' || true
fi
