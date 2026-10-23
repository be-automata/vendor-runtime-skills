# Asset map — harness-init

What `harness-init` writes, where it comes from, and what happens when the target already has
something in that spot. This is the document `harness-init`'s `SKILL.md` (HI-13) points at
instead of restating the rules inline.

## Contents

- Stack → rules
- Source tiers and resolution order
- Why we never run `somnio rules install`
- Agent adapters
- The skill set a run installs
- The CLI invocation contract
- The binary pin and drift policy
- Target layout
- Overwrite matrix
- What this document does not decide

## Stack → rules

Counts are pinned to `somnio-ai-tools` rev `aa91cef` (CLI 3.0.0) and re-derived, never estimated. A pin
bump re-counts them with the command below.

| Stack | Rule files at `aa91cef` |
|:--|--:|
| nestjs | 9 |
| python | 8 |
| django | 7 |
| flask | 7 |
| flutter | 7 |
| dart | 5 |
| fastapi | 6 |
| react | 6 |
| functions | 2 |
| typescript | 1 |
| **Total (10 stacks)** | **58** |

Counting command, reproducible against any working copy of `somnio-ai-tools`:

```bash
git -C <somnio-ai-tools> ls-tree -r --name-only aa91cef \
  | grep '^agent-rules/rules/.*\.md$' | wc -l
```

## Source tiers and resolution order

Four tiers, tried in this order. First hit wins.

| # | Tier | `rules_source.tier` | Behaviour |
|:--|:--|:--|:--|
| 1 | `$SOMNIO_AI_TOOLS_ROOT` | `env` | An explicit operator override. Used as-is; the run reports the path and the rev it resolves to. |
| 2 | Owned clone `$HOME/.somnio/somnio-ai-tools-<rev>` | `owned-clone` | The normal path: detached at the exact SHA, immune to pub-cache GC. |
| 3 | pub-cache at the rev | `pub-cache` | Accepted only when the cached checkout is at the pinned rev. Reported in the run output — it is a fallback, not the intended home. |
| 4 | Sibling working copy | `working-copy` | Requires explicit confirmation from a person, because a working copy is mutable and may be dirty or on another rev. Before asking, the run prints the stacks that working copy actually serves, so the confirmation is informed rather than blind. |
| — | none resolved | — | **Hard gate: stop.** This gates `harness-init` only; it is never an `install.sh` failure or a doctor failure for the rest of the harness. |

The pin literal (`aa91cef`) lives in `plugins/somnio-engineering-ai/scripts/install.sh`,
beside `GSTACK_COMMIT` — one definition, read by everything else.

## Why we never run `somnio rules install`

At `aa91cef`, the adapter fragment `agent-rules/adapters/claude/*/CLAUDE.md` is a generated
output that is gitignored (under the comment "Agent rules generated outputs (regenerate with:
cd agent-rules && npm run generate)") and therefore absent from the checkout.
`RulesInstaller._installClaudeModular` (`cli/lib/src/installers/rules_installer.dart`) reads
that fragment first and throws `Adapter fragment not found: ...` before copying any rule and
before writing the managed block. So the command does not partially install — it fails before
writing anything.

Regenerating the fragment is the write we refuse. Nothing in this repository ever writes into
that checkout; neither `somnio rules install` nor `agent-rules/scripts/generate.py` is ever
invoked — the source stays read-only, and `harness-init` copies rule files directly instead.

The CLI additionally writes a `.somnio-manifest` line-list inside each `.claude/rules/<stack>/`
directory. That is the CLI's own uninstall bookkeeping and is not `.claude/harness.json`. A
harness run **never writes it**, and reads it for one purpose only: a file it names **that is
on disk** is CLI-owned, so it is reported as `pre_existing` and never overwritten. A stale
entry — a name with no file behind it — is not trusted: the corpus rule installs and the plan
records `cli manifest: <path> is named by .somnio-manifest but missing; installed from the
corpus` in `not_in_scope`.

## Agent adapters

`--agents` names who will work in the target repo. `claude` never uses an adapter — its rules
are the copied `.claude/rules/` tree plus `install_assets.py`'s frontmatter normalizer. Every
other agent that has an adapter at the pin gets **one** rule file, written by one of two
mechanisms.

| Agent | Adapter at `aa91cef` | Target file | Mechanism |
|:--|:--|:--|:--|
| `claude` | — (deliberately not used) | `.claude/rules/<stack>/*.md` | copy + frontmatter rewrite |
| `codex` | `agent-rules/adapters/codex/<stack>/system-prompt.md` | `AGENTS.md` | managed block |
| `copilot` | `agent-rules/adapters/copilot/<stack>/copilot-instructions.md` | `.github/copilot-instructions.md` | managed block |
| `windsurf` | `agent-rules/adapters/windsurf/<stack>/.windsurfrules` — a dotfile, so a suffix-based listing hides it | `.windsurfrules` | managed block |
| `cursor` | `agent-rules/adapters/cursor/rules/<stack>/` — **absent at the pin**; only the adapter README ships | `.cursor/rules/somnio-<stack>.md` | copy |
| `antigravity` | README only at the pin | — | — |
| every other CLI agent id | no adapter | — | — |

**The codex adapter's target *is* the repository's `AGENTS.md`** — not a codex-private file:
that path is the cross-agent convention, so the managed block written there is read by every
other tool that loads `AGENTS.md`, and anything the repository already keeps in that file is
preserved outside the markers.

**Only the block is ours; the prose is not.** `install_assets.py` writes the managed block into
`AGENTS.md` and nothing else in that file. On acme-core (#403) the session model filled the rest
of it by transliterating `CLAUDE.md` with `claude` → `Codex`, applied to identifiers as well as
prose, which produced `.Codex/` paths that exist nowhere in three files. Agent-facing prose
**points at `CLAUDE.md`**; it never restates it. `verify_harness.py`'s `check-13` grades the
result against the tree.

**A vendored block can be wrong for the target, and that is named rather than fixed.** The
TypeScript adapter at the pin is written for a Firebase Functions repository and cites
`.claude/rules/functions/architecture.md`. Installed into a Fastify repository that file does
not exist. `check-13` reports it as a **warn** and the hand-over names it; the block stays
byte-identical to the pin, because rewriting it would break the one thing the provenance
manifest's `sha256` exists to prove (ENG-HARNESS-07) and would edit a corpus this area consumes
read-only (business rule 17, ADR-0003).

A managed block is the concatenation of the adapter file of each selected stack, in `STACK_ORDER`
(`detect_stack.py`'s canonical order, which `install_assets.py` imports rather than copies),
between the same `<!-- somnio:harness:begin -->` / `<!-- somnio:harness:end -->` markers
`CLAUDE.md` uses — same idempotency, same malformed-marker refusal, same strip-in-place on
`--uninstall`. A `cursor` copy follows the overwrite matrix below without a sidecar: a file the
harness did not write is reported as `pre_existing` and left alone.

Anything the row above marks absent or empty is a recorded gap, never an error and never an
upstream PR (ADR-0003 D-3): the plan carries
`rules: no <agent> adapter at <pin7>` or
`rules: no <agent> adapter content for <stack> at <pin7>`, and that agent still gets its skills
from the CLI step. Today that means **codex, copilot and windsurf have adapter content at the
pin and get a rule file**; cursor and antigravity do not, and resolve to a named gap until the
corpus ships their content.

## The skill set a run installs

Derived from the selected stacks, never chosen freehand. `--skills` overrides it; a person who
wants no skills at all passes `--no-cli-skills`.

| Group | Skill ids | When |
|:--|:--|:--|
| stack pair | `<stack>_plan` + `<stack>_health` | per selected stack in `flutter`, `nestjs`, `react`, `python`, `angular`, `angularjs` |
| core | `git_commit_format`, `git_branch_format`, `security_audit`, `harness_audit`, `optimize_claude_config` | always |
| flutter extra | `dart_model_from_json` | only when `flutter` is selected |

**Never installed by harness-init:** `soc2_audit`, `iso27001_audit`, `clockify_tracker`,
`dora_metrics`, `workflow_builder`. They are engagement- or account-scoped, not repository
setup.

The served stacks with **no** catalog skill — `django`, `fastapi`, `flask`, `typescript`,
`functions`, `dart` — get rules only, and the plan says so:
`cli skills: <stack> has no catalog skill (rules only)`.

## The CLI invocation contract

```
somnio skills install --agent <id> --project --skills <comma list>
```

- **One call per agent.** `--agent` is single-valued in the CLI (last wins), so a list would
  silently install for one agent only.
- **`--project`, always**, and the working directory is the target repo root — together they
  are what keeps every byte inside the target. `GIT_TERMINAL_PROMPT=0` and a 120 s timeout.
- **What it writes**, per agent: an install directory (`.claude/skills/<name>/` for claude,
  `.codex/skills/<name>.md` for codex, and for every other agent the per-agent project
  directory the CLI's own registry defines — discovered by scanning for `.somnio-skills.json`
  rather than by duplicating that table here, and no two agents share one) with a
  `.somnio-skills.json` manifest at its root, and for the agents that use them, a sibling
  `somnio_rules/` directory the manifest's `root: "rules"` entries point at.
- **The CLI's manifest stays the owner.** `harness-init` reads it back and records every path
  it lists under `.claude/harness.json`'s `cli_installed` (with the manifest's own path, so
  `verify_harness.py`'s check-9 can re-read it). `--uninstall` lists those paths, leaves them,
  and prints `somnio skills remove`.
- **`somnio rules install` is never run.** `skills install` is a different command: it copies
  catalog skills and regenerates no adapter, so the reasons in the section above still hold.
- **A failure is a gap, not a failed run.** A nonzero exit, a timeout or an absent binary is
  recorded as `cli skills (<agent>): <reason>` in `not_in_scope`; the exit code stays what the
  file writes made it (0, or 1 for rule conflicts). Adding a fourth exit meaning would break
  the 0 / 1 / 2 contract the whole pipeline gates on.

## The binary pin and drift policy

`SOMNIO_CLI_VERSION` lives in `plugins/somnio-engineering-ai/scripts/install.sh`, on the same
line it is copied into a working variable, with `SOMNIO_CLI_COMMIT` beneath it — each must
appear exactly once in that file, and `doctor.sh` and the skill both `grep` them from there
rather than carrying a literal.

The binary pin is a **separate rev from the content pin, by design**: `SOMNIO_AI_TOOLS_COMMIT`
(`aa91cef`, CLI 3.0.0) pins the rules corpus this run copies from; `SOMNIO_CLI_COMMIT`
(`aa91cef`, CLI 3.0.0) pins the binary it shells out to. Upstream tags neither, so both are
exact commits and both are bumped by PR — independently; sharing a rev is allowed.

| Situation | install.sh | doctor.sh | harness-init |
|:--|:--|:--|:--|
| binary at the pin | `already at <ver>` | `ok  somnio CLI <ver>` | runs the skills step |
| binary at another version | reports the install line, exit status unchanged | `warn somnio CLI <ver> (pin <pin>)` | **warns and continues**; the manifest records `cli_version` and `cli_pin` |
| binary absent | reports the install line, exit status unchanged | `MISSING  somnio CLI (harness-init skills step only)` | rules still install; the skills step is a named `not_in_scope` |

The installer never installs, activates or upgrades that binary: it is a separate product on its
own road (business rule 17). The fix line both `install.sh` and `doctor.sh` print pins the
activate to `SOMNIO_CLI_COMMIT`, because an unpinned activate would silently follow the default
branch:
`dart pub global activate -sgit https://github.com/somnio-software/somnio-ai-tools.git --git-path cli --git-ref $SOMNIO_CLI_COMMIT`.

## Target layout

Rules land at `.claude/rules/<stack>/<rule>.md`, matching the CLI's `rules_installer`
`claudeModular` shape so the two roads' outputs stay compatible. Filenames are copied
unprefixed — the CLI's `somnio-` prefix belongs to its directory format, not the Claude
modular one. The bytes are the source's, with one deliberate rewrite:
`install_assets.py` turns a rule's `globs:` frontmatter key (which every rule at `aa91cef`
carries) into a lazy `paths:` block list. Everything else in the frontmatter and the whole
body are verbatim, and the manifest records both hashes — `sha256` as written,
`sha256_pre_optimize` of the source file.

## Network policy

`scripts/detect_network.py` reads the target for the vendors it talks to (Cloudflare, Neon,
Supabase, …) and prints, on stdout, the vendors with their evidence, a `settings_fragment` that
lets a sandboxed session reach them (`permissions.allow` WebFetch rules, `sandbox.network
.allowedDomains`, `sandbox.excludedCommands` for proxy-unaware CLIs — ADR-0004), and the `gaps`
seeding cannot close. It is **advisory**: it never fails the run, and `install_assets.py` reads
its payload defensively — any key may be missing or malformed, and the answer is always no
fragment, no gaps, carry on, with the payload named as a skipped line.

**Gaps are never applied.** They are rendered in the plan, recorded in `.claude/harness.json`,
and named again at hand-over, because closing one is a person's decision (a Node runtime that
ignores `HTTPS_PROXY` is not fixed by adding a domain).

Also written:

| Path | What |
|:--|:--|
| `.claude/settings.json` | Merged, never replaced. Up to four documents deep — template, the network fragment, the declared-commands fragment, then the repo's own settings, which wins on anything it tuned. The template carries only rules true of every repository; `_comment*` keys document it and never travel. |
| `.claude/settings.json.bak` | The pre-harness original, written once and never overwritten; `--uninstall` restores from it and then removes it — the manifest's `settings_bak_created` says whether this harness wrote it, and a `.bak` it did not write is never deleted. Gitignored. |
| `CLAUDE.md` | Managed block only. |
| `.gitignore` | Managed block only. Carries `!` re-include lines for any planned path the target's own rules would ignore (issue #25); a parent-directory exclusion is a named gap instead. |
| `.claude/ship-gate.json` | A draft with `required: []`, written only when the file is absent or the harness wrote the file that is there. |
| `.claude/harness.json` | The committed manifest, validated against `templates/harness-manifest.schema.json`. A re-apply that changes nothing rewrites it byte for byte — `generated_at` is carried over when everything else is identical, so the timestamp means "when the harness last changed", not "when it last ran". It also records `objective` (when not the placeholder) and the normalized `commands` (when the flag was ever passed), which a flagless re-apply reads back so the `CLAUDE.md` commands table and the ship-gate `_candidates` survive a `--reconfigure`. The optional `network` key records the detected vendors (`id`, `confidence`), the `domains` and `excluded_commands` that were seeded, the `gaps` (`id`, `severity`) that were **not**, and `detected_at`; a flagless re-apply reads it back and keeps the timestamp as recorded, an explicit `--network` replaces the whole block, and `verify_harness.py`'s check-10 grades the recorded domains and exclusions against the settings file. A manifest written before HN-11b carries no such key and stays valid. The optional `allowlist` key records the runner, the file that named it, the `commands` seeded into `permissions.allow` and the ones `refused` with their reason; same three lanes as `network` (explicit `--toolchain` wins, a flagless re-apply carries it forward, neither leaves the allowlist at the template's repo-independent baseline). |
| `.claude/.harness/new/<path>` | A conflicting file's new content, until `--take`/`--keep` consumes it. |
| `.claude/.harness/init-<ts>.json` | Gitignored run verdict. |
| `AGENTS.md`, `.github/copilot-instructions.md`, `.windsurfrules` | Adapter managed blocks, one per chosen agent that has content at the pin. |
| `.cursor/rules/somnio-<stack>.md` | Adapter copies, one per stack, when cursor's adapter has content. |
| `.claude/skills/`, `.codex/skills/`, `.<agent>/skills/`, `<agent>/somnio_rules/` | Written by `somnio skills install`, owned by its `.somnio-skills.json`, recorded in `cli_installed`, never deleted by `--uninstall`. |

## Declared commands and the allowlist (#403)

`scripts/detect_toolchain.py` reads the verification commands the target **declares** — every
`package.json` `scripts` key at depth ≤ 3 (workspaces in, `node_modules` out) and every
`Makefile` target — invoked through the runner the lockfile names (`pnpm-lock.yaml` → `pnpm`,
and a `package.json` with no lockfile records `npm` as an assumption rather than a reading). It
prints a `commands.settings_fragment` of `Bash(...)` allow rules, and `install_assets.py` merges
it. The writer never derives an allow rule itself.

Two filters, and the second is the one worth knowing:

| filter | effect |
|:--|:--|
| verification family | only a name carrying `test`, `lint`, `build`, `typecheck`, `format`, `check`, `coverage`, `swagger`, `openapi`, … is offered at all. A repository's script map is not a list of safe commands. |
| side effect wins | a name carrying `deploy`, `publish`, `release`, `migrate`, `seed`, `reset`, `start`, `update`, `generate`, … is refused even when it also matched the family — `swagger:update` is not `swagger:check` — and the refusal is recorded with its reason so the filter is visible rather than silent. |

Capped at 24 rules; beyond that the overflow is a recorded gap, because an allowlist nobody
reads is not a safety control.

**Why this is not in `templates/target-settings.json`.** It used to be: seven
`Bash(pnpm run ...)` rules lifted from one backend monorepo. On acme-core five of them named
nothing that existed, two being near-misses of the real names (`test:cov` for `test:coverage`,
`openapi:check` for `swagger:check`). An allow rule for a script that does not exist is inert,
so nothing broke — the harness simply told the repository about itself and was wrong. A per-repo
fact belongs in a per-repo reading, and `verify_harness.py`'s `check-11` now grades the target's
allowlist against the repository's real script names, reading the repository rather than the
manifest so a target harnessed before this existed is graded too.

## Overwrite matrix

One row per situation, no else-branch.

| Situation | Action |
|:--|:--|
| Target file absent | **Write.** |
| Target present, SHA-256 matches the `installed[].sha256` in the target's `.claude/harness.json`, and either the corpus would write the same bytes or the corpus moved | **Overwrite** — the harness wrote those bytes; nobody has edited them. A no-op is counted in `unchanged[]`; a moved corpus (the entry's `source_rev` or `sha256_pre_optimize` no longer matches the current source) wins, and the optimize pass re-proposes. |
| Target present, SHA-256 matches the manifest, the corpus would write different bytes, and the corpus did **not** move (same `source_rev`, same verbatim source hash) | **Keep** — planned as `keep`, never written (HN-10b). This is a rewrite the harness accepted: `optimize-context-rules` narrowed the globs and `--refresh-hashes` re-recorded the file. The manifest entry is re-recorded from the bytes on disk; the plan's `kept[]` names it and the summary counts it. |
| Target present, no manifest entry at all, `--adopt-existing` | **Adopt as `pre_existing`** — the file is kept byte for byte, no sidecar is written, no question is asked, and the rest of the corpus installs beside it. This is the brownfield row of the applicability gate (a repo with its own `.claude/rules/` and no `.claude/harness.json`, decided 2026-09-04): a file nothing has ever claimed has nothing to reconcile. The same treatment covers the rest of that tree — a rule the corpus has no opinion about (`.claude/rules/python/house-style.md`) is adopted too, because half-adopting a repo's own rules would fail the gate on a file the run was forbidden to touch. `verify_harness.py`'s check-3 counts these and does not grade them — `optimize-context-rules` in Step 7 is what offers to make them lazy. |
| Target present, SHA differs (or no manifest entry, without `--adopt-existing`) | **Never overwrite.** Write the new content beside it at `.claude/.harness/new/<path>` (the target-relative path under the sidecar directory, `.claude/` stripped) and ask per group, not per file — one question for "the 9 nestjs rules", not nine. The answer is executed by re-running `--apply` with `--take` or `--keep`. |
| `.claude/settings.json` | **Merge, never replace** — `plugins/somnio-engineering-ai/scripts/merge-settings.py` `BASE OVERLAY`, base = the shipped template, overlay = the repo's existing settings, so tuned values survive. With `--network`, the same script runs twice: base = the template, overlay = the detector's `settings_fragment`, then base = that result, overlay = the repo's settings. Template → fragment → repo, so the repo still wins on anything it tuned and `allowedDomains` / `excludedCommands` / `permissions.allow` come out as the union of all three. No hand-rolled merge exists anywhere in this skill. |
| `CLAUDE.md` | **Managed block only** — rewrite strictly between `<!-- somnio:harness:begin -->` and `<!-- somnio:harness:end -->`; a `CLAUDE.md` with no markers is treated as human-owned and the block is appended, never interleaved. |
| A block-managed file (`CLAUDE.md`, `.gitignore`, `AGENTS.md`, …) the run **created**, on `--uninstall` | **Delete the file**, not just the block. The manifest entry carries `created: true`, and it is honoured only while the file's sha still matches what was written: prose added outside the block hands ownership back, and the block alone is stripped. A `.claude/rules/<stack>/` left empty by the removals is removed too — emptiness is the proof that nothing but this harness's rules lived there — as is `.claude/.harness/`, the blast radius the plan declared, and `.claude/` itself when nothing else is left in it. |
| Upstream pin moved since the manifest was written (`rules_source.rev` differs from the current pin) | **Report drift per file and ask** — file-by-file, naming which changed upstream and which the target edited locally, referencing ENG-HARNESS-07, the vendored-asset-currency process this feeds. |
| Any path outside the target repo root | **Never write.** Absolute paths, `..` traversal and symlinks pointing outside the root are refused; the run stops. |

## What this document does not decide

The `harness-init` pipeline itself — applicability gate, Repo Reading card, write plan,
verification gate — belongs to **HI-13**. Stack detection belongs to **HI-08**. The pin
literal and the doctor grading of it belong to **HI-05**. This document only fixes the source
tiers, the target layout and the overwrite matrix those three tickets consume.
