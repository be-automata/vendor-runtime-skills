---
description: Initialize a target repository's AI harness — install the pinned Somnio stack rules for every agent that will work there, the CLI catalog's skills and commands, merged settings seeded with the network policy this repo's own dependencies imply, the CLAUDE.md rule map and the provenance manifest, then verify. An empty repository gets the same full setup. Nothing is committed.
allowed-tools: Bash, Read, AskUserQuestion, Skill
---

Initialize the AI harness in a target repository.

1. **What lands in the target** — five write groups, two agent groups, plus the manifest:
   - `.claude/rules/<stack>/*.md` — copied from the pinned rules source, with each rule's
     `globs:` frontmatter key normalized into a lazy `paths:` list; the manifest records
     the hash as written and the hash of the verbatim source
   - `.claude/settings.json` — merged, never replaced; the previous file is kept as `.bak`.
     The sandbox network policy inside it is **read from this repo**, not hand-written: the
     vendors it already depends on (Cloudflare, Neon, Supabase, GCP, Sentry, …) decide which
     domains, `WebFetch` allows and CLI exclusions a sandboxed session gets, so calls to those
     services work without anyone editing sandbox settings. Anything the repo already tuned wins
   - `CLAUDE.md` — the managed block between the `somnio:harness` markers only
   - `.gitignore` — the managed block
   - `.claude/ship-gate.json` — a draft with `required: []`
   - the **adapter rule files** for the non-claude agents that were chosen — `AGENTS.md`
     (codex), `.github/copilot-instructions.md` (copilot), `.windsurfrules` (windsurf) as
     managed blocks; `.cursor/rules/somnio-<stack>.md` as copies
   - the **CLI skills and commands** for every chosen agent, installed by one
     `somnio skills install --agent <id> --project` call each — `.claude/skills/<name>/` for
     claude, `.codex/skills/<name>.md` for codex, and so on. The CLI's forbidden **rules**
     installer is never run (ADR-0003 D-1/D-2); `skills install` is a different command. These files stay owned by the CLI's own
     `.somnio-skills.json`: `--uninstall` lists them and leaves them, and prints
     `somnio skills remove` as the way to remove them
   - `.claude/harness.json` — the committed provenance manifest, which also records the
     agents, the somnio binary version, the harness pin, every CLI-installed path, the
     detected `network` policy, and the `review` block holding the roborev decision
     (ADR-0005 D-1/D-13 — who answered, when, and registered or declined), so a later run
     carries them forward and `verify` can grade them

   Three things seeding cannot fix are reported as **gaps**, never silently worked around:
   a Node runtime below 24 whose `fetch` ignores the proxy, a direct database socket the HTTP
   proxy cannot broker, and the allowlist boundary that only `strictAllowlist` closes. Relay
   them at hand-over; they are a person's decision.

2. **Nothing is committed, pushed, or PR'd.** The tree is left dirty on purpose so a person
   reviews and commits. `.claude/.harness/` is gitignored evidence, not provenance.

3. **Where the confirmations are:**
   - C2, the Repo Reading card — "does this match reality?"; on an empty repository, one
     extra multi-select for which stacks to install, from the list the corpus actually serves
   - Step 2b — which agents will work in this repository (claude pre-selected)
   - C4, the write plan — "apply this plan?"
   - Step 6 — the roborev registration decision (ENG-REV-05, ADR-0005 D-13): asked on every
     full run with the five disclosures read out first, pre-selected yes, never
     auto-registered. Declining is an ordinary answer and is recorded too. `--rules-only`
     skips this step entirely
   - the optimize pass — which rule rewrites to apply
   - `--uninstall` — a listed-paths confirmation, plus the read-only roborev reversal listing

4. **The hard gate:** without a resolvable pinned `somnio-ai-tools` rules source the run stops
   and names `/install` (ADR-0003 D-4). It also refuses on a dirty
   `.claude/`, `CLAUDE.md`, `.gitignore` or `.loop/`, and refuses to run against this
   repository. A missing `somnio` binary is **not** a gate: the rules still install and the
   skills step is skipped with a named gap and the doctor's fix line; a binary that differs
   from the harness pin is a warning the run records and continues past.

5. Hand off to the skill, forwarding the flags as given:

```
Skill: harness-init
$ARGUMENTS
```

Flags: `[path] [--rules-only] [--dry-run] [--reconfigure] [--uninstall]`.
