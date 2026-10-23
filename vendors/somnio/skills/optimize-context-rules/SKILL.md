---
name: optimize-context-rules
description: |
  Audits and optimizes a repository's Claude Code configuration so path-scoped
  rules load lazily and CLAUDE.md stays a lightweight always-loaded index.
  Use when asked to optimize, restructure, or lazy-load agent context rules,
  or when .claude/rules frontmatter, CLAUDE.md bloat, or the read-not-create
  hook need fixing. Validates every path glob against the real project tree
  instead of guessing. Three areas: (1) migrate .claude/rules/**/*.md
  frontmatter to the native lazy-load `paths:` YAML-array form (away from
  eager `globs:` or red-flagged CSV `paths:`); (2) slim CLAUDE.md files to an
  index + rule map, deleting redundant @import-only shells; (3) audit/install
  the PreToolUse(Write) hook that works around the read-not-create caveat.
  Audits first, reports, asks to confirm, then applies (--audit-only skips
  applying). Not for unrelated settings.json edits (permissions, env vars,
  non-rule hooks) or CLAUDE.md prose unrelated to rule-loading -- use a
  general Claude Code config skill instead.
argument-hint: "[--audit-only] [path]"
allowed-tools: Bash, Read, Edit, Write
---

# optimize-context-rules

Optimize the `.claude/` config of a repo so rules load *only when relevant* and
CLAUDE.md stays small. Default mode audits, reports, asks to confirm, then
applies. `--audit-only` stops after the report.

## Why this matters (canonical facts)

Verified empirically in Claude Code **2.1.145**:

- **`paths:` as a YAML array** lazy-loads a rule (it enters context only when a
  matching file is read) AND passes the IDE extension's schema validation. This
  is the target format.
- **CSV single-line `paths: a, b`** lazy-loads at runtime but the IDE flags it
  red (*"paths must be an array of glob patterns"*). Migrate to the array form.
  YAML-array support landed in CC 2.1.84.
- **`globs: "..."`** (Cursor's field) loads **eager at session start, ignoring
  scope**. This is the main thing to fix — convert to `paths:`.
- **`alwaysApply:`** is a Cursor field, ignored by Claude Code — harmless noise.
  Leave it; do not churn files just to remove it.
- A rule with **no `paths:` and no `globs:`** is effectively always-loaded. Flag
  it: either it genuinely belongs in CLAUDE.md, or it needs a `paths:` scope.
- **Read-not-create caveat (bug #23478):** path-scoped rules trigger on *read*,
  not *create*. Creating a brand-new file that matches a rule, without first
  reading a neighbor, does NOT load that rule. The hook in area 3 backstops this.
- **CLAUDE.md** should be a lightweight, always-loaded **index**: project
  overview + a rule map (pointers into `.claude/rules/*`) + a short "Critical
  Rules" digest — NOT full rule bodies inlined. Per-directory CLAUDE.md files
  that are pure `@import` shells are redundant once rules self-load via `paths:`.

## Step 0 — Parse arguments

- `--audit-only` present → report only, never edit or write.
- A trailing path arg → treat it as the repo root to scan; otherwise use the
  current working directory. Resolve `.claude/` under that root.
- If `<root>/.claude` does not exist, stop and tell the user there is nothing to
  optimize here.

## Step 1 — Discover

Collect, under `<root>`:

```bash
find .claude/rules -type f -name '*.md' 2>/dev/null
find . -name 'CLAUDE.md' -not -path '*/node_modules/*' 2>/dev/null
ls .claude/settings.json .claude/settings.local.json 2>/dev/null
ls .claude/hooks/inject-rules.py 2>/dev/null
```

Read each rule file's frontmatter, each CLAUDE.md, and the settings file(s).

## Step 1b — Analyze project structure (derive paths from reality)

Do NOT guess path globs or trust existing ones blindly. Build a factual map of
the repo first, then derive/validate every `paths:` against it.

Enumerate the real tree (tracked files only, so build output and deps are
excluded):

```bash
git ls-files 2>/dev/null || find . -type f -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/build/*'
```

From that file list, compute:

1. **Top-level domains** — the first path segment of each file (e.g. `apps/`,
   `functions/`, `packages/`). These are the coarse areas rules will scope to.
2. **Monorepo package roots** — directories containing a manifest
   (`pubspec.yaml`, `package.json`, `Cargo.toml`, `go.mod`, `pyproject.toml`).
   Nested packages (e.g. `apps/mobile/packages/*/`) change the right glob depth:
   prefer `apps/mobile/packages/*/lib/**/*.dart` over a flat `**/*.dart`.
3. **Extensions per domain** — which file types live where (e.g. `.dart` under
   `apps/`, `.ts` under `functions/`). This tells you the file-type tail of each
   glob (`**/*.dart` vs `**/*.ts`).
4. **Generated / vendored / frozen areas** — directories a rule should NOT match
   (`*.g.dart`, `*.freezed.dart`, `build/`, `node_modules/`, `lib/` build output,
   and anything the repo's CLAUDE.md marks frozen/deprecated). Exclude these from
   proposed globs.

Use a quick aggregation to see the shape, e.g.:

```bash
# extensions by top-level domain
git ls-files | awk -F/ '{d=$1; n=split($NF,a,"."); print d"  *."a[n]}' | sort | uniq -c | sort -rn | head -40
# package roots
git ls-files | grep -E '/(pubspec\.yaml|package\.json|Cargo\.toml|go\.mod|pyproject\.toml)$'
```

Hold this map; Step 2 uses it to validate and derive globs.

## Step 2 — Audit rules frontmatter

For every `.claude/rules/**/*.md`, classify its frontmatter:

| Finding | Condition | Proposed action |
|---|---|---|
| ✅ OK | `paths:` is a YAML array (one `- pattern` per line) | none |
| 🔴 eager `globs:` | has `globs:` (any form) | rename key to `paths:`, convert value to a YAML array (one item per line) |
| 🟡 CSV `paths:` | `paths:` value is inline / comma-separated on one line | reformat to a YAML array |
| 🟡 unscoped | no `paths:` and no `globs:` | derive a candidate scope from Step 1b and the rule's own domain, then propose it for the user to confirm. Do NOT auto-apply a scope. |
| 🟠 stale glob | a `paths:`/`globs:` pattern matches **zero** tracked files | flag it — the directory was renamed/removed, or the depth/extension is wrong. Propose the corrected pattern from the real tree. |
| 🟠 over-broad | a pattern matches generated/vendored/frozen files (Step 1b list) | tighten it (add the right package depth, or narrow the extension) so the rule only loads for hand-written source. |

**Validate every pattern against the real tree.** For each `paths:`/`globs:`
entry, check it actually matches files (reuse the glob→regex logic from the hook,
or `git ls-files | grep` with the glob translated). A pattern matching zero files
is the most common silent failure — the rule looks scoped but never loads.

**Deriving a glob (for unscoped or stale rules).** Use the Step 1b map:
- Identify the rule's domain from its `description:`/body (e.g. "Flutter
  client", "Cloud Functions", "TypeScript") and map it to the real top-level
  domain(s) and extension(s) you found.
- Respect monorepo package depth — emit `apps/mobile/packages/*/lib/**/*.dart`
  when packages exist, not a flat `**/*.dart`.
- Exclude generated/frozen areas; never scope a rule to `build/`,
  `node_modules/`, `*.g.dart`, or a deprecated/frozen dir.
- Present each derived glob with the **count of files it matches** so the user
  can sanity-check (e.g. `apps/mobile/lib/**/*.dart → 142 files`).

Conversion rules:
- When a pattern is already correct, preserve it verbatim — only change the key
  name and the YAML shape. Strip surrounding quotes the CSV form may have.
- Only rewrite a pattern's *value* when Step 1b proves it stale or over-broad,
  and show the before/after in the report.
- Keep `description:` and any `alwaysApply:` line unchanged.
- Idempotent: a file already in the ✅ state with patterns that match real files
  gets no edit.

Target shape:

```yaml
---
description: <unchanged>
alwaysApply: false        # only if it was already there
paths:
  - apps/mobile/lib/**/*.dart
  - functions/**/*.ts
---
```

## Step 3 — Audit CLAUDE.md slimming

For each `CLAUDE.md` found:

1. **Pure `@import` shell** — file body (after stripping blank lines/comments) is
   nothing but `@import`/`@path` lines → **redundant**, because rules now
   self-load via `paths:`. Propose deletion. Exception: keep it if a parent
   `CLAUDE.md` relies on importing it AND it carries unique prose.
2. **Bloated index** — a top-level CLAUDE.md that inlines full rule bodies that
   already exist under `.claude/rules/`. Propose replacing the duplicated section
   with a one-line pointer (`(→ .claude/rules/<area>/<file>.md)`), keeping only a
   short digest. Do NOT delete unique content — only de-duplicate what is
   verbatim-present in a rule file.
3. **Healthy index** — overview + rule map + short Critical Rules digest →
   leave it.

Be conservative: when unsure whether content is unique, flag it for the user
rather than deleting. Never delete a CLAUDE.md that has hand-written guidance not
covered by a rule.

## Step 4 — Audit the read-not-create hook

Check three things:

1. `.claude/settings.json` registers a `PreToolUse` hook with matcher `Write`
   pointing at `.claude/hooks/inject-rules.py`.
2. `.claude/hooks/inject-rules.py` exists.
3. The registered command carries BOTH guards of the snippet below — the
   `${CLAUDE_PROJECT_DIR:-$PWD}` fallback and the trailing `|| true`. A
   command missing either one (typically the older bare
   `"$CLAUDE_PROJECT_DIR/..."` form) is reported as `stale snippet → rewrite`.

If 1 or 2 is missing, propose installing it; if 3 fails, propose rewriting the
command. The canonical hook script ships
with this skill as `assets/inject-rules.py` — **this skill is self-contained**.
Always deploy it verbatim: `Read` `assets/inject-rules.py` and `Write` its
exact content to `.claude/hooks/inject-rules.py`. NEVER copy `inject-rules.py`
(or any config) from another repository, a sibling project, or a path on the
host machine — there is no "reference repo". The skill must behave identically
for anyone who runs it, including a colleague who only has this skill
installed.

**`.claude/settings.json`** (merge into existing `hooks` block; don't clobber
other hooks):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PROJECT_DIR:-$PWD}/.claude/hooks/inject-rules.py\" || true"
          }
        ]
      }
    ]
  }
}
```

The command has two guards, and both are load-bearing:

- `${CLAUDE_PROJECT_DIR:-$PWD}` — when the variable is unset (a headless run,
  a subagent, a shell that did not export it) the path would otherwise resolve
  to `/.claude/hooks/inject-rules.py`; the fallback keeps it under the
  session's working directory.
- `|| true` — a missing hook file, a host without `python3`, or any other
  launch failure exits 0. PreToolUse hooks are advisory here: they must never
  block a Write. The hook itself also traps every failure and exits 0, but the
  `|| true` covers the cases where the hook never starts. stderr is not
  redirected, so a failure stays visible in the hook log for diagnosis.

Pattern support in the hook: quoted block-list items, the flow form
`paths: ["a", "b"]`, `**`, `*`, `?`, bracket classes `[a-c]` / `[!a-c]`, and
one level of brace groups `{ts,tsx}`. Note that `harness-init`'s writer
(`install_assets.py`) keeps brace globs whole since HN-01b; the hook expands them, so a
`{ts,tsx}` pattern itself — follow-up HN-01b; until then the hook's brace
support matters for hand-written rules only.

**`.claude/hooks/inject-rules.py`** — when absent, `Read` `assets/inject-rules.py`
(bundled with this skill) and `Write` its exact content to
`.claude/hooks/inject-rules.py`. Do not paraphrase, reformat, or fetch it from
elsewhere — the deployed file must be byte-identical to the asset.

If `.claude/hooks/inject-rules.py` exists but differs from
`assets/inject-rules.py`, report the diff and ask before overwriting — the user
may have customized it. When the user confirms a sync, `Read` `assets/inject-rules.py`
and `Write` it verbatim; do not pull the file from any other location.

## Step 5 — Report

Print one consolidated table grouped by area (Rules / CLAUDE.md / Hook), each row
being `path · finding · proposed action`. For any glob that changes or is
derived, show before → after **with the file-match count** so the scope is
verifiable at a glance (e.g. `globs:"lib/**" → paths: apps/mobile/lib/**/*.dart
(142 files)`). End with a count summary, e.g. `3 rules to migrate · 1 stale glob
fixed · 1 CLAUDE.md to delete · hook already installed`.

If nothing needs changing, say so plainly and stop — the config is already
optimal.

## Step 6 — Confirm & apply

- If `--audit-only`: stop after the report.
- Otherwise ask the user to confirm (`y` to apply all, or let them deselect
  specific rows). Then:
  - Edit rule frontmatter with `Edit` (key rename + YAML-array reshape).
  - Delete redundant shells with `rm`; de-duplicate bloated CLAUDE.md with `Edit`.
  - Install the hook (`Write` the script, `Edit`/`Write` settings.json) only if
    that row was confirmed.
- Never touch `unscoped` rules or ambiguous CLAUDE.md content automatically —
  those are advisory flags for the user to resolve by hand.

## Step 7 — Verify

After applying, tell the user how to confirm lazy-loading works:
- Run `/memory` (or read a file matching a migrated rule's `paths:`) and look for
  the `⎿ Loaded …` InstructionsLoaded message — the rule should appear only when
  a matching file is in context, not at session start.
- The hook fires only on creating a *new* matching file; the additionalContext
  arrives after the first write, so the model self-corrects with a second write
  (create-then-fix). Net result: the new file ends up compliant.
- The hook never blocks a Write: any failure — missing file, no python3,
  malformed rule — is silent by design (exit 0). If a rule is not being
  injected, run `sh tests/optimize-context-rules/hook-test.sh` from this
  repository or feed the hook its JSON by hand; do not expect an error.

Do not run `git commit` unless the user asks.

## Process

Serves `ENG-HARNESS-06` in `docs/PROCESS_MAP.md`.

## Run evidence

After delivering the output, record run evidence for the ship gate
(best-effort; never block on it):

```bash
sh "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)/plugins/somnio-engineering-ai}/scripts/ship-marker.sh" optimize-context-rules 2>/dev/null || true
```

In repositories whose `.claude/ship-gate.json` requires this marker,
the ship gate blocks `git push` / `gh pr create` until it is fresh.
