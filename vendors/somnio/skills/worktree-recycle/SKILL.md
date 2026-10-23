---
name: worktree-recycle
description: >
  Finds the linked worktrees under .claude/worktrees whose PR is merged or closed and whose tree
  holds nothing unpushed, and removes the ones a person picks. Use when asked to clean up,
  recycle, prune or list leftover worktrees, after a PR merges, or when a worktree blocks checking
  out its branch. Never removes a dirty, locked, in-use or open-PR worktree, and never uses
  --force. Not for creating worktrees (dev-execution, loop-mode do that).
---

# Worktree Recycle

Lists leftover worktrees with a verdict each, and removes the ones a person picks.

## Hard rules

- **Removal is the person's pick, every time.** Show the table, ask, remove only the rows chosen.
- **Never `--force`, never `rm -rf`.** The script runs a plain `git worktree remove`; git's
  refusal on a dirty worktree is the second guard, and it stands.
- **Anything not proven safe is `KEEP`.** No token, an API error, or a non-GitHub origin reads
  `KEEP unknown`, never `RECYCLE`.
- **Scope is `<main>/.claude/worktrees/` only.** Anything else reads `KEEP foreign`. Example:
  `~/.superset/worktrees/.../friendly-shingle` is never touched.
- **The branch survives by default.** `--delete-branch` is a second, separate yes, and applies to
  a merged PR only.

## Step 1 — Classify

```bash
SKILL_DIR="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)/plugins/somnio-engineering-ai}/skills/worktree-recycle"
sh "$SKILL_DIR/scripts/worktree-recycle.sh"
```

Run it from any checkout of the repository. One line per linked worktree:

| Verdict | Reason | Meaning |
|:--|:--|:--|
| `RECYCLE` | `merged`, `closed` | PR merged or closed, tree clean, HEAD at or behind the PR's head. |
| `RECYCLE` | `in-base` | No PR or detached, and HEAD is already in the base branch. |
| `PRUNE` | `prunable` | Directory gone; `git worktree prune` drops the metadata. |
| `KEEP` | `dirty` | Uncommitted or untracked files. Example: `0 modified, 1 untracked`. |
| `KEEP` | `unpushed` | Commits after the PR's head. |
| `KEEP` | `open-pr` | The newest PR for the branch is open. |
| `KEEP` | `locked`, `in-use`, `current`, `foreign`, `no-pr`, `unknown` | Locked, a process or a recent HEAD move inside it (default 2 h, `--min-idle`), the caller's own worktree, outside the pool, no PR, or GitHub could not answer. |

Each row shows `ignored=<n>`: ignored files (`.env`, `node_modules`) that go with the directory.

## Step 2 — Ask

Nothing `RECYCLE` → report the table and stop. Otherwise put one `AskUserQuestion` with a
multi-select of the `RECYCLE` rows. Each option names the path, the PR and `ignored=<n>`. Say
plainly that ignored files are lost. Example option: `pr-review-mode — #82 merged, 14 ignored files`.
Ask about branch deletion only when a chosen row is `merged`, as a separate question.

## Step 3 — Remove

```bash
sh "$SKILL_DIR/scripts/worktree-recycle.sh" --remove <path>... [--delete-branch]
```

It re-classifies each path first and refuses any that is no longer `RECYCLE`. Example: the PR
was reopened since Step 1 → `REFUSE KEEP open-pr …`. Exit 0 = all removed, 3 = at least one
refused and left exactly as it was. Show every line verbatim.

## Triggers

- On demand: this skill.
- `/doctor` (full report) prints the summary. Example:
  `info  worktrees: 1 recyclable, 0 prunable, 3 kept (…)`.
- `/pr-sync` and `loop-mode` offer this skill when the watch reports `PR-<n> MERGED`;
  `dev-execution` names it in its merge handoff.

## Process

Serves `ENG-EXEC-07` in `docs/PROCESS_MAP.md`. Pattern: decision support for the classification,
never autonomous for the removal.

## Network

One `curl` read per branch to `api.github.com` (`api.github.com` in the target's
`allowedDomains`), through `gh-rest.sh`. `gh` is used for `gh auth token` only, so it is not an
`excludedCommands` matter.

## Scripts

| Script | Purpose |
|:--|:--|
| `scripts/worktree-recycle.sh` | Classify, summarize, remove. POSIX sh + curl + jq; the GitHub read goes through `../pr-sync/scripts/gh-rest.sh`. |

## Run evidence

```bash
sh "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)/plugins/somnio-engineering-ai}/scripts/ship-marker.sh" worktree-recycle 2>/dev/null || true
```
