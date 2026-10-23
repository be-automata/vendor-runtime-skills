# Detection signals

This document *is* `detect_repo_state.py` and `detect_stack.py`, written in prose: every
signal either script reads, every branch it can take, and the six fixtures the test suite
grades them against. Nothing here is aspirational — a claim below that stops matching the
scripts is a bug in this document, not a design decision to defer.

## Contents

- [Signal table](#signal-table)
- [Repo-state branches](#repo-state-branches)
- [The subdirectory rule](#the-subdirectory-rule)
- [Ambiguity procedure](#ambiguity-procedure)
- [Stack signals](#stack-signals)
- [Fixtures](#fixtures)

## Signal table

`detect_repo_state.py` walks the tree from `<root>` once, pruning `EXCLUDED_DIRS`, and
reads these manifest, extension and harness signals along the way:

| Signal | Source | Feeds |
|:--|:--|:--|
| Source file extensions | `SOURCE_EXTENSIONS` (`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.dart`, `.go`, `.rs`, `.java`, `.kt`, `.swift`, `.rb`, `.php`, `.cs`, `.vue`, `.svelte`, `.scala`) | `source_files.total`, `source_files.by_extension` |
| Manifest filenames | `MANIFEST_NAMES` (`pyproject.toml`, `setup.py`, `package.json`, `pubspec.yaml`, `go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle`, `build.gradle.kts`, `firebase.json`), plus the glob `requirements*.txt`, at depth ≤ 2 | `manifests`, branch 2 of the verdict table |
| CI config | `.github/workflows/*.yml`, `*.yaml`, `.gitlab-ci.yml` | `ci_workflows` (corroborating only — see below) |
| Test directories | dirs named `tests`, `test`, `__tests__`, `spec`, `e2e`, at depth ≤ 2 | `test_dirs` |
| Docs | `*.md` at root, `*.md` directly under `docs/` | `docs` |
| Harness files | `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, `.claude/rules/*.md`, `.claude/agents/*.md`, `.claude/commands/*.md`, `.claude/skills/*/SKILL.md`, `.claude/ship-gate.json`, `.claude/harness.json`, `.loop/config.yaml` | `harness.*` |
| Git history | `git -C <root>` `rev-parse`, `rev-list`, `log`, `ls-files`, `symbolic-ref` | `git.*` |

**Exclusion list**, pruned from every walk so neither script pays for or reasons about
generated or vendored trees:

| Excluded | Reason |
|:--|:--|
| `.git` | version-control internals, not repository content |
| `node_modules` | installed npm/yarn/pnpm packages, not authored code |
| `build`, `dist`, `.next`, `target` | compiled/bundled output, regenerated on every build |
| `vendor` | vendored third-party source, not this repository's own conventions |
| `.dart_tool`, `.venv` | language-tool caches and virtualenvs, not source |
| `__pycache__` | compiled Python bytecode |
| `Pods` | CocoaPods-installed iOS dependencies |
| `reports` | generated test/coverage output |

## Repo-state branches

`detect_repo_state.py` applies these five branches in order against a single evidence
payload; the first match wins and there is no else-branch — branch 5 (`ambiguous`) is what
"none of the above" means:

1. **No resolvable `HEAD`** (zero commits) → `greenfield`. A repository that has never
   committed cannot have accreted conventions, regardless of what is sitting in the working
   tree.
2. **A stack manifest is present and `source_files.total >= 25`** → `brownfield`. This is
   the only branch the 25-file floor gates, and it is deliberate: 25 is the point below
   which a repository cannot plausibly have accreted conventions worth reading — a scaffold
   plus a handful of modules — and above which rewriting the harness from a template would
   overwrite decisions someone already made. It is a constant, not a tuned parameter,
   changeable by PR like any other threshold in this document, and CI presence never
   substitutes for it: a repository can run a green pipeline over four files.
3. **`source_files.total == 0`** → `greenfield`. A manifest with no source yet (a freshly
   scaffolded project) reads the same as no manifest at all.
4. **No stack manifest and `commit_count <= 1`** → `greenfield`. One commit and no manifest
   is still a scaffold, even with a handful of loose source files sitting in it.
5. **Otherwise** → `ambiguous`. Everything that survives branches 1–4 without a manifest-plus-25
   majority and without a one-commit floor lands here — including a repository with CI and
   four source files, which is `ambiguous`, not `brownfield` (CI is corroborating evidence
   only: it is recorded in `ci_workflows` and may be cited in `reason`, but no branch tests
   it directly).

Each branch reads a fixed subset of the payload: branch 1 reads `git.commit_count`; branch 2
reads `manifests` and `source_files.total`; branch 3 reads `source_files.total`; branch 4
reads `manifests` and `git.commit_count`; branch 5 needs nothing further — it is what is left.

## The subdirectory rule

`detect_repo_state.py` refuses to run from inside a package directory of a monorepo: if
`git -C <root> rev-parse --show-toplevel` resolves to a path that differs from `<root>`
itself, the script exits 2 with `error: root is a subdirectory of a git repository; pass
the repository root` rather than silently re-rooting to the toplevel.

The reason is that harness files are repo-scoped. `.claude/`, `CLAUDE.md`, `.loop/` and
every other artifact `/harness-init` writes belong at the repository root; running the
detector from `apps/api` inside a monorepo and letting it quietly operate on the toplevel
would write the harness into a single package while leaving the actual repository root
unharnessed — a mistake indistinguishable from success until someone looks for the harness
in the wrong place.

Both sides of the comparison are passed through `realpath` before comparing, which is
load-bearing on macOS: `mktemp -d` returns a path under `/var/folders/...`, while
`git rev-parse --show-toplevel` resolves the same directory to
`/private/var/folders/...` — `/var` is itself a symlink to `/private/var`. Comparing the raw
strings would report every temp-directory fixture as a subdirectory mismatch even when the
two paths name the same place on disk.

## Ambiguity procedure

An `ambiguous` verdict is not a dead end for `/harness-init` — it is a single, bounded
question. On `ambiguous`:

1. **Ask once.** Present the counts that produced the verdict: `source_files.total`, the
   manifests found, `git.commit_count`, and whether any `ci_workflows` were seen.
2. **State the recommended side**, derived from those same counts: nearer the 25-file floor
   *and* more than one commit recommends **brownfield**; otherwise recommends
   **greenfield**. The recommendation is a reading of the evidence already in the payload,
   never a new signal.
3. **Take the brownfield path on any answer other than an explicit greenfield.** Brownfield
   is the conservative choice — it reads before it writes — so silence, "not sure", or any
   answer that is not a clear greenfield selection defaults to it.
4. **Never ask twice in one run.** The question is asked once per `/harness-init`
   invocation; a second `ambiguous` verdict inside the same run (there is only one target
   repository per run, so this should not occur) is not re-prompted.

## The greenfield question

A `greenfield` verdict is not a smaller install. The repository has no language to read, so the
one thing detection cannot answer is asked, once:

1. **Offer the real list, never a guess.** `detect_stack.py --list-served` prints the ten
   stacks the pinned corpus carries rules for and, per stack, the CLI catalog skill pair that
   exists for it. Those `served[]` rows are the option list, verbatim — a stack that is not in
   them has no rules to install and must not be offered.
2. **Multi-select.** A greenfield repository is often more than one stack (a python API and a
   react front end); one question with several selectable options, not a sequence of yes/no
   questions.
3. **Mark which options also bring skills.** An option whose `skills` list is empty
   (`django`, `fastapi`, `flask`, `typescript`, `dart`, `functions`) installs rules only. Say so on the
   option, so the choice is informed.
4. **Recommend a default.** The stack the repository's own files imply if anything at all does
   — a lone `pyproject.toml` under a `source_files.total` of zero still recommends `python` —
   else `python`. The recommendation is a reading of the payload, never a new signal.
   **Except when every detected stack is `served: false`** (`node`, `go`, `rust`, `jvm`): the card says
   so plainly — the language was read, the pin carries no rules for it — and the question is
   asked with the served list and **no** recommended default. The only honest reading of that
   evidence is "none of these", and recommending an unrelated stack would be a new signal.
5. **Then take the same pipeline.** The answer becomes `STACKS[]` and the run continues exactly
   as a brownfield run does: same rules source, same write plan, same C4, same verification.
   There is no partial greenfield outcome and no blueprint prerequisite.

**A detected stack is never re-asked.** On brownfield, `detect_stack.py` has already answered
and the Repo Reading card shows the answer with its evidence; confirming the card is the
confirmation. Asking "which stack?" on a repository that declared its own stack is noise.

## Stack signals

`detect_stack.py` reports base languages **always** when their manifest is present, and
frameworks **in addition**, never instead — a repository with `@nestjs/core` in
`package.json` reports both `nestjs` and `node`, never `nestjs` alone.

| Signal (relative to `<root>`) | Emits | Confidence | Kind |
|:--|:--|:--|:--|
| `pyproject.toml`, `requirements*.txt`, `setup.py` | `python` | high | base |
| `fastapi` in declared deps | `fastapi` (+ `python`) | high | framework |
| `django` in declared deps | `django` (+ `python`) | high | framework |
| `flask` in declared deps | `flask` (+ `python`) | high | framework |
| `manage.py` at root, no `django` dep | `django` (+ `python`) | medium | framework |
| `package.json` | `node` (`served: false`) | medium | base |
| `@nestjs/core` in `package.json` deps | `nestjs` (+ `node`) | high | framework |
| `react` or `next` in `package.json` deps | `react` (+ `node`) | high | framework |
| `tsconfig.json` at the root | `typescript` | high | base |
| `tsconfig.json` nested at depth 1–2 (`tools/`, `scripts/`, `packages/*`), none at the root | `typescript` | low — lifted to medium when `nestjs` or `react` is also detected at high | base |
| `pubspec.yaml` naming Flutter (`sdk: flutter`, a `flutter:` key), or unreadable | `flutter` | high | base |
| `pubspec.yaml` naming no Flutter | `dart` | high | base |
| `firebase.json` with a `functions/` directory or a `functions` key, or `functions/package.json` | `functions` | medium | base |
| `firebase.json` alone (hosting, rules, emulators) | `functions` | low | base |
| `go.mod` | `go` (`served: false`) | high | base |
| `Cargo.toml` | `rust` (`served: false`) | high | base |
| `pom.xml`, `build.gradle`, `build.gradle.kts` | `jvm` (`served: false`) | high | base |

**Confidence tiers and the `default` flag:** every entry carries `default`, set to
`confidence != "low"`. `STACKS[]` is lifted from that flag, never re-read from the card: a
`low` stack is shown on the Repo Reading card with its evidence but installs only when the
person adds it (real-repo UAT of 2026-09-04, finding F-3 — a Flutter app got a root-wide
`typescript` rule from `tools/firestore_observer/tsconfig.json`). Root and nested
`tsconfig.json` together report the root file as evidence at `high`.

**Base-language-always rule:** a manifest signal always emits its base language, whether or
not a framework is also detected — `pyproject.toml` alone still emits `python`.

**`STACK_ORDER` determinism rule:** the detected set is sorted by index in a fixed list
(`python`, `django`, `fastapi`, `flask`, `node`, `typescript`, `nestjs`, `react`, `dart`,
`flutter`, `functions`, `go`, `rust`, `jvm`), so `stacks[0].name == "python"` on a `pyproject.toml`
repository and `stacks[0].name == "node"` on a `package.json`-only directory are true by
construction, not by accident of filesystem walk order.

**The `served` caveat:** `served` is present on every entry and is emitted from one local
constant — `SERVED_STACKS`, the same list `--list-served` prints. A stack outside it
(`node`, `go`, `rust`, `jvm`) is `served: false`: the language is read, the pin carries no
rules for it. The two answers cannot disagree, which is the point — a scan that called
`node` served while the greenfield question omitted it (the shape HI-17 fixed) offers a
person a stack nothing can install. The script still never reads the corpus: the
authoritative served set at the actual pinned revision is resolved downstream by
`resolve_rules_source.py`.

## Fixtures

The six fixtures built by `tests/harness-init/fixtures/build-fixtures.sh` and exercised by
`tests/harness-init/detect-test.sh`:

| Fixture | Expected verdict / exit code |
|:--|:--|
| `empty-repo` | exit 0, `greenfield` (branch 1 — no commits) |
| `pyproject-40py-ci` | exit 0, `brownfield` (branch 2 — manifest + 40 source files) |
| `pyproject-3py` | exit 0, `ambiguous` (branch 5 — manifest but under the 25-file floor) |
| `already-harnessed` | exit 0, `brownfield`, with `harness.harness_json == true` and `harness.rules_count == 1` |
| `not-a-repo` | exit 2, empty stdout (no git repository) |
| `subdir-of-repo` | exit 2, empty stdout (the subdirectory rule — this is `pyproject-40py-ci/docs`, not a separately built fixture) |
