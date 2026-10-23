# Network vendors: how a repository decides its own sandbox policy

`scripts/detect_network.py` reads a target repository and answers one question:
which third-party services do this repository's own dependencies, terraform
providers, CI workflows and env-var names name? The answer becomes a
`.claude/settings.json` fragment a person reviews, plus a list of gaps a person
accepts. The script writes nothing, makes no network call, and never opens a
real `.env`.

It exists because `templates/target-settings.json` ships three allowed domains
— `registry.npmjs.org`, `github.com`, `api.github.com`. Every repository that
talks to anything else would otherwise need the sandbox block hand-edited per
repository, by hand, from memory. Detection turns that hand-edit into a review.

Related: `docs/adr/0004-network-egress-and-trusted-commands-in-target-repositories.md`
(the decisions this script obeys) and `references/detection-signals.md` (the
stack-detection table, same authoring conventions).

## Contents

- [What the sandbox actually does](#what-the-sandbox-actually-does)
- [The vendor table](#the-vendor-table)
- [Detection sources and their limits](#detection-sources-and-their-limits)
- [What the host scan refuses to read](#what-the-host-scan-refuses-to-read)
- [The gap catalogue](#the-gap-catalogue)
- [Adding a vendor by PR](#adding-a-vendor-by-pr)

## What the sandbox actually does

Measured on 2026-09-06 inside a real sandboxed target (`marketplace-monorepo`,
plugin 0.9.6), not inferred:

| Fact | Evidence |
|:--|:--|
| DNS is blocked at the kernel | `nslookup` → `bind: Operation not permitted`; `socket.gethostbyname` fails |
| All egress goes through an HTTP CONNECT proxy | injected as `HTTPS_PROXY` / `https_proxy` |
| `curl` honours it | `api.github.com` 200, `api.cloudflare.com` 400, `console.neon.tech` 401, `example.com` 200 |
| python `urllib` honours it | same four hosts, same results |
| Node's global `fetch` ignores it | `ENOTFOUND` — undici reads no proxy variable |
| Raw TCP cannot work at all | a `pg` client to a Neon endpoint on `:5432` has nothing to CONNECT through |

Two consequences follow, and both are baked into the script.

**`allowedDomains` is a boundary only with `strictAllowlist`.** Without it, a
host outside the list prompts a person, and a `--dangerously-skip-permissions`
lane auto-approves it (ADR-0004 D-5; that is why `example.com` answered 200).
The fragment this script emits is the *right* list, not a hard block; a repo
that wants the block sets `sandbox.network.strictAllowlist: true` itself.

**A CLI that cannot be made proxy-aware needs an exclusion, and exclusions are
never invented here.** The fragment's `sandbox.excludedCommands` is filtered
against the shipped template's own list: a vendor CLI reaches the fragment only
if it is evidenced in the repository *and* already appears in
`templates/target-settings.json`. A new escape from the sandbox is a template
PR and a TL decision, never a detection result (ADR-0004 D-2).

## The vendor table

`VENDORS` in `detect_network.py` is module-level data, kept alphabetical by id.
One row per service. Fields:

| Field | Meaning | Confidence it produces |
|:--|:--|:--|
| `id` | stable machine id; appears in evidence, gaps and the stderr summary | — |
| `name` | display name | — |
| `deps` | package names in `package.json`, `pubspec.yaml`, `requirements*.txt`, `pyproject.toml`. A signal ending in `/` or `-` is a namespace prefix (`@aws-sdk/` covers `@aws-sdk/client-s3`); anything else matches exactly | **high** |
| `files` | paths, relative to the root, whose existence names the vendor (`fly.toml`, `.github`, `wrangler.toml`) | **high** |
| `providers` | terraform provider names read from `*.tf` / `*.tf.json` | **high** |
| `actions` | `uses:` prefixes in `.github/workflows/*.yml` | **high** |
| `env` | env-var **names** from `.env.example` / `.env.sample` / `.env.template` | **medium** |
| `hosts` | hostname fragments matched in tracked source files | **medium** |
| `domains` | what the vendor needs in `sandbox.network.allowedDomains` | — |
| `clis` | the vendor's CLI binaries, filtered against the template before use | — |
| `transports` | `https` for anything a proxy brokers; a `*-tcp` transport says a raw socket is involved and cannot be | — |

The confidence rule in one sentence: **a dependency, provider, workflow action
or vendor file naming the vendor is `high`; an env-var name or a hostname in
source is `medium`.** The difference matters because a hostname in a README is
weaker evidence than a package the build installs, and a person reviewing the
fragment should see which kind they are looking at.

Every row must carry at least one real domain. A row without one cannot change
a repository's policy; it is a documentation entry, not a vendor.

## Detection sources and their limits

| Source | Read | Deliberately not read |
|:--|:--|:--|
| `package.json` | `dependencies` + `devDependencies`, every workspace package | `node_modules` (pruned via the shared `EXCLUDED_DIRS`), transitive deps |
| `pubspec.yaml` | `dependencies` + `dev_dependencies` names | transitive deps, `pubspec.lock` |
| `requirements*.txt`, `pyproject.toml` | requirement names at the root | nested Python packages |
| `*.tf`, `*.tf.json` | `provider "x"` and `source = ".../x"` | terraform state, module registries |
| `.env.example`, `.env.sample`, `.env.template` | **key names only**, split at the first `=` | every value, and a real `.env` — which is not in the file list, is denied by the template's `Read(**/.env)` rule, and is skipped again by name in the source scan |
| `.github/workflows/*.yml` | `uses:` action names | job scripts |
| tracked source files | hostname fragments, one file recorded per (vendor, host) pair | lockfiles, binaries, anything untracked or ignored, and everything in the next section |

The source scan uses `git ls-files` on purpose: it is the repository's own
answer to "what counts as source here", so generated output, vendored trees and
ignored files fall away for free. It is capped (1500 files, 256 KB each) —
this is advisory detection, not an audit.

## What the host scan refuses to read

The hostname scan — and **only** the hostname scan; dependency, provider,
env-key, workflow-action and file signals are unaffected — skips these paths:

| Path | Constant |
|:--|:--|
| `.claude/**`, `.codex/**`, `.cursor/**`, `.windsurf/**` | `HOST_SCAN_EXCLUDED_PREFIXES` |
| `.windsurfrules`, `.github/copilot-instructions.md`, `.github/dependabot.yaml` / `.yml` | `HOST_SCAN_EXCLUDED_PATHS` |
| `AGENTS.md`, `CLAUDE.md` | `HOST_SCAN_EXCLUDED_BASENAMES` |
| `cspell*.json`, `.cspell*`, `*dictionary*`, `*wordlist*`, `*.dic` | `HOST_SCAN_EXCLUDED_GLOBS` |

A hostname in agent configuration, in an instruction file, or in a spell-check
dictionary is not evidence that the product talks to that vendor. Two failure
modes were observed on real repositories before this was added:

- **Self-evidence.** `.claude/settings.json` already carries the allowlist this
  harness writes, so `api.github.com` and `registry.npmjs.org` inside it
  "detected" GitHub and npm. On a re-run that is circular: the fragment would
  keep re-justifying whatever a previous run put there.
- **Documentation.** `automata`'s `datadog` and `gcp` rows came only from
  `.claude/skills/*/SKILL.md` and `.codex/skills/*.md` prose — every agent's
  rules directory is the same class of file, which is why all four are listed — and `acme-frontend`'s GitHub host came
  from `.github/cspell.json`, a word list. Each seeded a domain into the
  allowlist for a vendor the product never calls.

The exclusion is about **where the string lives**, not about the string: the
same hostname in a real source file is still evidence, at medium confidence,
and `tests/harness-init/network-test.sh` asserts both halves. Extend the list
by PR, and only with paths that are documentation, agent configuration or a
word list — never a path where product code lives.

## The gap catalogue

A gap is **never** folded into `settings_fragment`. The fragment is what a
person can apply as written; a gap is a decision a person makes. Mixing them
would let detection widen a sandbox on its own, which ADR-0004 D-2 and D-8
forbid.

| id | Severity | Fires when | Fix it names |
|:--|:--|:--|:--|
| `node-fetch-proxy` | medium | at least one **detected vendor** was evidenced by a `package.json` dependency **and** the detected Node major is below 24 (or undetected — unknown is not proof of 24+). A Flutter repo whose only `package.json` holds build tooling with no vendor dependency reaches no vendor from Node and gets no gap | `NODE_OPTIONS=--use-env-proxy` exists only from Node 24; until the runtime is there, a harness script that needs the network uses `curl` or python, never Node `fetch` |
| `raw-tcp-db` | high | a `pg`/`postgres`/`mysql2`/`mongodb`/`redis`/`psql` client **and** a hosted DB vendor (Neon, Supabase, PlanetScale, MongoDB Atlas, Upstash) | run that lane outside the sandbox as a named per-command exclusion a person accepts — never an interpreter wildcard (`node *`, `uv *`, `sh *`); or switch to the vendor's HTTP driver, which the proxy brokers |
| `jest-cache-write` | low | jest is present and no in-tree `cacheDirectory` is declared | the `/private/tmp/jest_dx` EPERM observed on the monorepo; add the path to `sandbox.filesystem.allowWrite` (target-owned, ADR-0004 D-8) or set jest's `cacheDirectory` under `<rootDir>` |

`runtime.fetch_proxy_aware` is always `false`, and that is a measurement rather
than a placeholder: Node's global fetch reads no proxy variable at any version
on its own. Nothing this script can read from a repository makes it true.

## Adding a vendor by PR

1. Add one dict to `VENDORS` in `scripts/detect_network.py`, in alphabetical
   position by `id`.
2. Give it at least one real domain, and only domains the vendor actually
   serves — the fragment is a security boundary a person will trust.
3. Put a CLI in `clis` only if it exists. It reaches `excludedCommands` only
   when `templates/target-settings.json` already lists it; if it should be
   listed there, that is a separate change with its own review.
4. Mark a `*-tcp` transport whenever the vendor is reachable over a raw socket.
   That is what makes `raw-tcp-db` fire, and a wrong answer there hides a real
   failure.
5. Add an assertion to `tests/harness-init/network-test.sh` — a fixture that
   the row fires, and, when the row is broad, a fixture that it does not fire
   on a neighbouring stack.

Run `uv run --script detect_network.py --list-vendors` to see the table as the
script sees it, and `sh tests/harness-init/network-test.sh` before the PR.
