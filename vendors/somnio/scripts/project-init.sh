#!/usr/bin/env bash
# Initialize project-scoped ruflo runtime (memory, sessions, intelligence) for
# the CURRENT repository — safely.
#
# Why this script exists: a bare 'ruflo init' overwrites repo-owned files
# (CLAUDE.md, .claude/settings.json) and drops generated content (.agents/,
# .codex/, AGENTS.md, .claude/agents|commands|skills|helpers) into the working
# tree. This wrapper lets init create ONLY what we want — the gitignored
# runtime (.claude-flow/, .swarm/, memory/, .hive-mind/) — and reverts
# everything else using git.
#
# Requires: a clean git status for repo-owned paths (refuses otherwise, so it
# can never revert a developer's uncommitted work).

set -euo pipefail

log() { printf "[harness] %s\n" "$*"; }

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  log "ERROR: not inside a git repository."; exit 1; }
cd "$REPO_ROOT"

command -v ruflo >/dev/null 2>&1 || {
  log "ERROR: ruflo CLI not installed. Run /somnio-engineering-ai:install first."; exit 1; }
command -v python3 >/dev/null 2>&1 || {
  log "ERROR: python3 not found — needed to merge plugin wiring back into .claude/settings.json."; exit 1; }

if [ -f .swarm/memory.db ] || [ -d .claude-flow/data ]; then
  log "project ruflo runtime already initialized (found .swarm/ or .claude-flow/data). Nothing to do."
  exit 0
fi

# Refuse to run over uncommitted changes in the paths init is known to touch.
DIRTY="$(git status --porcelain -- CLAUDE.md AGENTS.md .claude .gitignore 2>/dev/null || true)"
if [ -n "$DIRTY" ]; then
  log "ERROR: uncommitted changes in CLAUDE.md/AGENTS.md/.claude/.gitignore."
  log "Commit or discard them first — this script reverts init's edits to those paths with git."
  exit 1
fi

# Snapshot which cleanup targets exist BEFORE init: only files init itself
# created may be deleted afterwards. Other repos legitimately own .mcp.json,
# .agents/, .codex/, or AGENTS.md — deleting those would be data loss.
PRE_CLAUDEMD=$([ -e CLAUDE.md ] && echo 1 || echo 0)
PRE_AGENTS=$([ -e .agents ] && echo 1 || echo 0)
PRE_CODEX=$([ -e .codex ] && echo 1 || echo 0)
PRE_AGENTSMD=$([ -e AGENTS.md ] && echo 1 || echo 0)
PRE_MCPJSON=$([ -e .mcp.json ] && echo 1 || echo 0)

log "running 'ruflo init --minimal' (runtime only; repo files will be reverted)..."
RUFLO_NO_SKILLS_SH=1 ruflo init --minimal --no-signup --no-skills-sh --no-global >/dev/null 2>&1 || \
  log "WARN: ruflo init exited non-zero; continuing with cleanup and verification."

# Policy: ruflo's integration edits to .claude/ (settings, helpers, agents,
# commands, skills) and .gitignore are KEPT — review them and commit by PR.
# Three things are still cleaned up:
#   - CLAUDE.md is the area's brain; init's rewrite of it is reverted.
#   - Codex artifacts (.agents/, .codex/, AGENTS.md) are out of scope here.
#   - .mcp.json: the plugin owns MCP wiring; a project copy would duplicate it.
# Revert CLAUDE.md when the repo has one; if init CREATED it (repo had none,
# so there is nothing in HEAD to check out), remove the generated file.
if [ "$PRE_CLAUDEMD" = "1" ]; then
  git checkout -q -- CLAUDE.md 2>/dev/null || true
else
  rm -f CLAUDE.md
fi
# Delete only what init created; anything that pre-existed belongs to the repo.
[ "$PRE_AGENTS" = "0" ] && rm -rf .agents
[ "$PRE_CODEX" = "0" ] && rm -rf .codex
[ "$PRE_AGENTSMD" = "0" ] && rm -f AGENTS.md
if [ "$PRE_MCPJSON" = "0" ]; then
  rm -f .mcp.json
else
  log "NOTE: this repo has its own .mcp.json — left untouched; check it does not also declare a ruflo server (the plugin already provides one)."
fi

# ruflo init REPLACES .claude/settings.json rather than merging. One rule
# (scripts/merge-settings.py) restores what the repo committed without
# discarding init's edits: base = committed HEAD, overlay = init's output, so
# dicts recurse, lists union, scalars keep init's value, a type clash keeps the
# committed shape. This is what keeps the plugin wiring, the secret deny list,
# the sandbox block, and committed hooks alive across init.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -f .claude/settings.json ]; then
  TMP="$(mktemp .claude/.settings-XXXXXX.tmp)"
  if git show HEAD:.claude/settings.json 2>/dev/null \
       | python3 "$SCRIPT_DIR/merge-settings.py" - .claude/settings.json > "$TMP"; then
    mv "$TMP" .claude/settings.json
  else
    rm -f "$TMP"
    log "WARN: could not merge committed settings back over ruflo's output; left init's version in place."
  fi
fi

# Minimal init defaults the memory backend to in-memory, which does not
# persist across sessions — align the runtime with the team-canonical
# claude-flow.config.json (hybrid, persisted under .claude-flow/data).
if [ -f .claude-flow/config.yaml ]; then
  sed -i.bak 's/^\([[:space:]]*\)backend: memory$/\1backend: hybrid/' .claude-flow/config.yaml \
    && rm -f .claude-flow/config.yaml.bak
  # The sed matches ruflo's current generated format; if an upgrade changes
  # the key or value shape it would silently no-op — surface that instead.
  grep -q "backend: hybrid" .claude-flow/config.yaml \
    || log "WARN: could not set memory backend to hybrid in .claude-flow/config.yaml — ruflo's config format may have changed; project memory may not persist."
fi

CHANGES="$(git status --porcelain 2>/dev/null || true)"
if [ -n "$CHANGES" ]; then
  log "ruflo's project integration was kept — review and commit deliberately:"
  printf "%s\n" "$CHANGES"
else
  log "working tree unchanged; only gitignored runtime state was added."
fi

log "project runtime ready: $(ls -d .claude-flow .swarm memory .hive-mind 2>/dev/null | tr '\n' ' ')"
log "optional: bootstrap intelligence from this repository with 'ruflo hooks pretrain'."
