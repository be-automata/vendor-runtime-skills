# Ticket pipeline — the default runbook

Used when the target repository has no `docs/runbooks/ticket-pipeline.md` (`RUNBOOK=default`).
Distilled from the Marketplace runbook (`marketplace-monorepo/docs/runbooks/ticket-pipeline.md`,
an 11-ticket run) and this harness's `docs/TICKET-PIPELINE.md`. It is stack-neutral: every command
is a `GATE_<ROLE>` slot from `execution_config.py`, never a literal from another repository.

## Stages

| Stage | Model | Gate |
|:--|:--|:--|
| Spec | Opus | The ticket plus a ground-truth block, every claim cited to `file:line` and verified in the worktree |
| Validate | Opus | Adversarial, graded against the repository. `PASS` / `PASS_WITH_WARNINGS` / `BLOCK`. Fix cycles capped by `FIX_CAP`; with `FIX_CAP=none`, ask after each cycle |
| Implement | Sonnet | `GATE_BUILD`, `GATE_TYPECHECK`, `GATE_LINT`, `GATE_COVERAGE` (and `GATE_CONTRACT` on an API change), each exit 0. Commit only inside the verify agent |
| Acceptance | Orchestrator | `acceptance_gate.py grade` prints `ACCEPTANCE ok` |
| Ship | Orchestrator | PR with reasoning and the acceptance section, then the review watch. The person merges |

The dispatcher picks the model at call time. An agent file that pins `model:` in frontmatter
overrides this table silently. Each agent states its model on the first line of its output.

## Bootstrap, before any agent runs

```
git -C <repo> worktree add -b <branch> .claude/worktrees/<KEY> origin/<integration>
<GATE_INSTALL>     # run from the worktree root
<GATE_BUILD>       # must exit 0 BEFORE any agent runs
```

A worktree without dependencies makes every gate exit 1 (`turbo: command not found`). Agents read
that tooling failure as a verification result. One ticket burned three cycles on it. A `GAP` in
`GATE_INSTALL` or `GATE_BUILD` stops the ticket: ask for the command, never guess one.

## Ground truth for the implementer

1. **Verify existence claims at write time.** "X does not exist" is the cheapest claim to check
   and the most expensive to get wrong. Never inherit it from an earlier pass.
2. **State what is already done.** Agents redo finished work they are not told about.
3. **Write the spec to the worktree** as `docs/specs/<KEY>/spec.md`, so the validator grades a file.
   It is not committed unless the repo keeps specs. Check `git ls-files docs/specs`.

## Verification traps

- **A green test run is not a green build.** Transpile-only runners (`@swc/jest`, vitest, ts-jest
  with `isolatedModules`) pass code that fails the typecheck. Run `GATE_TYPECHECK` as its own step.
- **`GATE_TEST` is not the pre-push gate.** The coverage-gated variant (`GATE_COVERAGE`) is. A green
  `test` still gets rejected at push.
- **Rebase invalidates evidence.** It rewrites HEAD. Order: `fetch && rebase`, then commit, then
  the gates, then push with `--force-with-lease`.
- **The e2e gate needs a clean tree and an idle machine.** Run it from a detached scratch worktree
  this run creates. Concurrent agents cause random socket failures. Never accept one red run as a
  verdict, and never accept one green run after a red one without a reason.
- **Draft-PR CI green is fake.** `CI Success` can show while build, test and e2e were `skipped`.
  Read each check's conclusion and its run time.
- **Jest path patterns are regexes over the absolute path.** `.claude/worktrees/<KEY>` is part of
  that path. A pattern like `worktrees` or a ticket key can match every test, or none.
- **Scan the merge target, not the working tree.** A stale checkout reports issues already fixed.
- **A CVE ticket is closed only as of its scan date.** Re-scan at close time.
- **Subagents use absolute paths.** `grep -rn X <worktree>/src`, not `cd <worktree> && grep -rn X
  src`. A `cd` then a relative read cannot be checked against the permission rules, and it stops an
  unattended run with a prompt.

## The validate gate

It catches false claims about existing configuration: a wrong line range, a coupling that does not
exist, a criterion nothing can satisfy. Once, after two BLOCKs, an agent edited its spec to tell the
validator how to rule. A spec cannot define its own acceptance gate. Keep the gate graded against the
repository.

Its output is machine-consumable:
- a per-section scorecard (`PASS` / `WEAK` / `MISSING`);
- `requiredFixes[]`, each with a 1-10 confidence;
- `phases[]`, each at most the file cap.

A `PASS` without `phases[]` is not a PASS. Send it back.

## Refusals that are correct

An implementer that refuses because the spec's file list exceeds the cap is right. Raise the cap to
match the spec, or split the ticket. Never let an agent guess which subset to build.

## Shipping

- The PR carries the reasoning:
  - what changed;
  - what was deliberately not changed;
  - the acceptance section from `acceptance_gate.py render`.
  An AC deviation is stated plainly, never marked met.
- Expect the review to find real defects.
- A push after approval dismisses the approval. Budget for the re-review.
- A stale `CHANGES_REQUESTED` is never auto-dismissed. Only the reviewer clears it.
- A squash-merged parent turns stacked children dirty. Rebase and `--skip` the commits already
  upstream. Do not resolve those conflicts by hand.
