---
name: loop-mode
description: >-
  Run a parallel multi-ticket epic build through dependency-layered worktrees
  with hard-gated spec/validate/implement pipelines. Verifies real tracker
  AC/blockers first (whichever tracker the repo records), then one
  bootstrapped worktree+branch per ticket, a 20-section spec verified against
  real code, a PASS/WARN/BLOCK validation gate, phased implementation
  committed only on real zero-exit verification, an acceptance gate before
  any push (no criterion ignored), FE/UI-aware design references, a
  run-summary per ticket, and an optional recurring PR-comment loop with HITL
  decisions. Use when the user wants several related tickets built in
  parallel, says "loop-mode" / "modo loop" / "implementemos estos tickets en
  paralelo", or wants an autonomous loop with checkpoints. Single ticket or
  supervised sequential epic → /dev-execution; un-hardened tickets →
  /dev-epic-refinement first. Never edits settings.json/hooks/CLAUDE.md.
  Requires the Workflow tool and a git repo.
argument-hint: "[epic-or-ticket-keys]"
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Write, Bash, Agent, AskUserQuestion, Skill, Workflow, ToolSearch, mcp__claude_ai_Atlassian_Rovo__*
---

# loop-mode

An autonomous agentic loop over N tickets from one epic: investigate first, spec before any code,
one git worktree + branch per ticket, FE/UI-aware, paused before any push/PR until the user says
otherwise, with an optional recurring loop that watches PR comments after tickets ship.

**The point of this whole pattern:** eliminate gray areas. A gray area is anything the loop would
otherwise guess at silently — a tracker dependency nobody checked, an acceptance criterion nobody
read or verified, a design convention nobody looked up, a "should pass" nobody actually ran. Every step below
exists to close one specific gray area before implementation starts, not after.

**Scope boundary.** This skill is a specialist in producing an autonomous agentic loop with
human-in-the-loop (HITL) checkpoints where genuinely needed. It does not superimpose itself on the
project's own harness configuration — it never edits `settings.json`, hooks, or `CLAUDE.md`; it
reads and follows them, the same as any other work in this repo would.

This skill is heavyweight by design — it spawns real worktrees and multiple agents per ticket.
Only run it when the user has actually asked for parallel multi-ticket implementation, not for a
single ticket or a quick fix — those go to `/dev-execution` (the sequential reference pipeline).
Tickets must already be hardened SDD specs; thin tickets route to `/dev-epic-refinement` first.

**Execution config.** At the start of a run (Step 1), read the target repo in one call, the same
reader `/dev-execution` Step 0 uses:

```bash
uv run --script <abs>/skills/dev-execution/scripts/execution_config.py <repoPath> --key <first key>
```

It reads `.claude/harness.json` `execution` first and `.loop/config.yaml` only as a fallback
(harness `docs/LOOP-GATES.md`); exit 3 (a malformed record, or the two files naming different
trackers) stops the run. Its named gates (`budget_gate`, `verifier_blocking_gate`,
`adr_conflict_gate`, `adr_rule_violation_gate`, `spec_clarification_gate`,
`self_healing_exhaust_gate`) decide, per escalation class, whether the run proceeds, records,
auto-fixes, or aborts — the decision protocol (Step 7) routes through them, falling back to
asking the user when `GATES=interactive`. `adr_rule_violation_gate: abort` is the mechanical form
of "no ADR, no development". Its `GATE_<ROLE>` lines are the commands Step 6 passes to the
Workflow; a `GAP` in install, build or coverage is asked in Step 1, never guessed.

**Tracker access.** The tracker is `TRACKER` from that reader: the recorded one, else Jira
(`TRACKER_SOURCE=default`) unless the repo names another. The five operations and their tools per
tracker are `../dev-execution/references/tracker-adapters.md`. With `TRACKER=unrecorded`, ask in Step 1 (Jira · YouTrack · GitHub issues · Local) and offer to record
it with harness-init's `record_execution.py`. A tracker whose tool is absent degrades Step 2 to
the user-supplied AC and says so (harness `docs/DEV_HARNESS.md` § Tracker access).

## Step 0 — Orchestrator language (ask this first, before anything else)

Ask the user, before any other question: should the orchestrator's status updates, escalations,
and decision prompts be in **español** or **English**? This governs only how *you* (the
orchestrator) talk to the user during this run — it does not change spec language (Step 6 follows
the project's own spec corpus), commit messages, or subagent-internal prompts, and it does not
change PR description language, which follows the project's own template/policy (Step 9).

## Step 1 — Gather ticket inputs (always ask what's missing)

Five inputs are non-negotiable. Do not infer or guess any of them — ask via AskUserQuestion if the
user's message didn't already supply them. Every past rebase collision this pattern hit traced
back to skipping one of the first four.

1. **Ticket set** — the N ticket keys with a one-line scope each. If the user only named an epic,
   look it up through the tracker adapter rather than asking them to retype what's already tracked.
2. **Dependency graph** — which tickets are independent vs. which extend/mount inside another. Get
   this explicit even if it seems obvious — it drives worktree base-branches and merge order. Step
   2 cross-checks this against the tracker's own data, so treat this as the user's starting belief, not
   the final word.
3. **File-ownership map** — for any file more than one ticket might touch (shared barrels,
   registries, shared constants/catalogs), assign a single owner ticket. If the user isn't sure,
   grep the repo for files each ticket's scope would plausibly touch and flag overlaps for them to
   assign rather than assigning it yourself.
4. **Push/PR policy** — default is **paused**: nothing gets pushed or opened as a PR until the
   user gives explicit go-ahead, ticket by ticket or as a batch.
5. **Auto-push of review fixes** — always ask, in the same opening round, one AskUserQuestion:
   *once the PRs are open, push the fixes for review findings without asking each time?*
   - **Yes (recommended)** — Step 10 arms `autopush-guard.sh` on every PR in the batch (ADR-0008
     D-12). Each fix still passes the guard; a refusal is asked.
   - **No** — every review-fix push is asked, as before. Step 9 records the No on each PR it
     opens (`autopush-guard.sh decline`), so nothing can arm it.

   This answers only the review-fix pushes after the PRs exist. The first push and the PRs stay
   paused under item 4. Merge, replies, review requests and the ready flip stay the user's either
   way. Record the answer and who gave it in the run state; Step 10 reads it.

Things you *can* infer without asking (state your inference so the user can correct it): base
branch (usually `develop` or `main`), the verification command (check root `package.json` /
`CLAUDE.md`), worktree root (default `.claude/worktrees/`), branch naming (default
`feat/{ticket}-{slug}`).

## Step 2 — Ticket intelligence pass

Before locking in anything from Step 1's dependency graph, read `references/ticket-intelligence.md`
and follow it: spawn one read-only research subagent per ticket, in parallel, to pull the real
acceptance criteria/Definition of Done from the tracker, check its own issue links for blockers (in
**any** status, not just an obvious "blocked" one), cross-check those against the user's declared
graph, and run a fast codebase-fit probe for obvious "this ticket's premise doesn't hold here"
smells.

Reconcile everything the reports surface through the decision protocol (Step 7) **before** Step 5's
layering runs — a tracker blocker the user's graph missed, a declared dependency the tracker
doesn't corroborate, or a blocker still in progress all need a conscious call, not a silent override in
either direction. A ticket whose Open decisions section still holds an item that blocks
implementation (its answer changes the code) leaves the run's layering, together with the batch
tickets that depend on it. `/address-open-questions <ticket>` asks the person a question per
decision, so it runs in the main session, never inside the Workflow or a subagent. The ticket
rejoins at the next layer once its record reads `Verdict: READY` or `Verdict: READY ON
DEFAULTS`; on `Verdict: BLOCKED`, report the owner through Step 7. The rest of the batch
proceeds. No findings means proceed with the user's original graph unchanged.

## Step 3 — Model orchestration

Ask the user (multiple choice, AskUserQuestion) which models to assign to the two roles in this
pipeline. Lead with the scheme that's proven for this pattern, marked as the recommendation, but
let them override — some repos or budgets call for something else:

- **Research/verification** (ticket intelligence, spec generation, the spec-validation gate) →
  recommend the most capable Opus model currently available.
- **Implementation** (writing code, running verification, committing) → recommend Sonnet.

Model rule (from the reconciled pipeline fix): **where a stage names a model, the dispatcher
chooses at call time — no agent in this pipeline carries a `model:` frontmatter field** (a pinned
frontmatter silently beats prose), and since no availability probe exists, an agent asked to run
on a specific model states which model it is actually running on as its first output line.

Offer options like: "Opus for research+verification / Sonnet for implementation (recommended)",
"Same tier for everything — ask which", "Custom — I'll specify per role". Resolve whatever they
pick into a concrete `models: { spec, validate, implement }` map — this feeds `args.models` in
Step 6's Workflow call.

## Step 4 — FE/UI detection

Scan each ticket's scope/AC (now enriched by Step 2) for FE/UI signals: `[FE]` tags, mentions of
screens/views/components/pages, "design", "Figma", "mockup", "UI", "layout", or a scope that
touches a frontend app path directly. For every flagged ticket, follow
`references/fe-ui-detection.md` — in short:

1. Ask whether the user wants to attach a **Figma link**, a **Claude/Figma design reference** (via
   the Figma MCP if connected), or an **image**, with "skip / no visual reference" as a valid
   option.
2. Ask a second question recommending the **latest available Opus model, or Fable 5**, for that
   ticket's spec+implementation stages — visual fidelity benefits from the strongest model more
   than most backend-only phases. Let the user override.
3. Record, per FE ticket: `isFE: true`, `feModel`, and `designRef`. These flow into
   `args.tickets[]` in Step 6.

FE tickets carry a hard requirement baked into their prompts regardless of the above: read the
project's `CLAUDE.md` (and any nested one, e.g. `apps/web/CLAUDE.md`) and inspect `DESIGN.md` or
whatever design-system source the project actually has before writing any UI code.

## Step 5 — Topological layering

Sort the tickets into dependency layers using the graph reconciled in Step 2 and the `needs`
vocabulary + DAG checklist in `references/wave-planning.md`: layer 0 is every ticket with no
dependency, layer 1 depends only on layer-0 tickets, and so on. Do this reasoning yourself —
don't delegate it to an agent. A dependent ticket's worktree must branch off its
parent's *finished* branch, which is only possible if each layer runs after the previous one
produced real commits.

## Step 6 — Per-layer Workflow invocation

For each layer, call the `Workflow` tool with `scriptPath` pointing at
`references/loop-mode-workflow.js`, and `args` shaped like:

```js
{
  repoPath: "/absolute/path/to/the/target/repo", // REQUIRED — see guardrails below
  tickets: [
    {
      key: "MH-1234", slug: "ambient-view", scope: "...",
      acceptanceCriteria: [...], definitionOfDone: [...],   // from Step 2
      baseBranch: "develop",              // layer-0 only
      parentBranch: "feat/MH-1200-shell", // dependent tickets only — the PARENT'S branch name
      isFE: true, feModel: "opus",        // from Step 4, omit entirely for non-FE tickets
      designRef: { type: "figma", value: "https://figma.com/..." },
    },
  ],
  fileOwnership: [{ file: "apps/web/domains/copilot/index.ts", owner: "MH-1234" }],
  models: { spec: "opus", validate: "opus", implement: "sonnet" }, // from Step 3
  baseBranch: "develop",
  // REQUIRED, from execution_config.py's GATE_<ROLE> lines — the script refuses without them:
  // Compose only the roles that are not GAP (a typecheck or lint GAP is dropped and said so
  // in the run report); the script refuses a GAP anywhere in a command.
  verifyCommand: "<GATE_BUILD> && <GATE_TYPECHECK> && <GATE_LINT> && <GATE_COVERAGE>",
  installCommand: "<GATE_INSTALL>",   // the Setup stage runs install then build before any agent
  buildCommand: "<GATE_BUILD>",
  appPath: "apps/web",
  worktreeRoot: ".claude/worktrees",
  branchPrefix: "feat/",
  maxFilesPerPhase: 5,
}
```

Spec generation inside the script follows the canonical 20-section template
(`references/spec-template.md`) and validation returns a PASS / PASS_WITH_WARNINGS / BLOCK verdict
capped at 3 fix-and-revalidate cycles before escalating — same as this repo's own `/generate-spec`
skill, so a loop-mode spec is indistinguishable in structure from a manually-run one. The script
never pushes or opens PRs — that stays with you, per Step 9, even on a clean run.

## Step 7 — Decision protocol (applies to every escalation, here and in Step 10)

Read `references/decision-protocol.md` in full before the first escalation of a run — it also
carries the four Karpathy guardrails (verifiable success, surfaced assumptions, simplicity,
surgical changes) that govern every subagent's behavior, not just what reaches the user. In short:
every decision handed to the user is **multiple choice** via AskUserQuestion, and you must have
already researched the codebase/tracker/ADRs/`CLAUDE.md` enough to include your own recommendation as
one of the options, clearly labeled — never hand the user an unresearched shrug of options. This
applies to ticket-intelligence mismatches (Step 2), spec-validation blockers, implementation
blockers, rebase conflicts, and PR-comment findings (Step 10) alike.

## Step 8 — Run summary per finished goal

When a ticket reaches a terminal state (done or blocked) in the Workflow's return value, write a
markdown run summary using `references/run-summary-template.md` to
`{worktreePath}/docs/specs/{ticket}/run-report.md`. This is the artifact this loop pattern leaves
behind per goal — ticket-intelligence findings, orchestrator decisions, subagents spawned
(label/model/phase/verdict), gaps/blockers with confidence scores, spec-validation verdict history
(including any fix-and-revalidate cycles), and the model assignments used for that ticket.

## Step 9 — Push / PR (only when the user lifts the pause)

Never automatic, regardless of how clean a layer looks. When the user explicitly says to proceed:

0. **ROBOREV LAST — flush each branch's pending review before it is pushed.** Per ticket, from its
   worktree, in the same dependency order as the pushes below:

   ```
   "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)/plugins/somnio-engineering-ai}/scripts/roborev-guard" review --branch --wait
   ```

   **The guard, never the `roborev` binary** (ADR-0005 D-6, amended 2026-09-16 — a skill step is
   not a person, so the "attended use" exemption does not cover it). Exit **0** passed, **1** AMBIGUOUS (see below), **3** the guard refused (report and stop flushing this ticket), **69** roborev absent,
   which is normal for an unregistered repo — skip and say so.
   This lane needs the flush more than the sequential one: every ticket works in
   `.claude/worktrees/<ticket>`, and a worktree is graded by its own `.roborev.toml` rather than
   the main checkout's, so what the post-commit hook did or did not enqueue there is not something
   to assume. Flushing explicitly is how the branch arrives at its PR with its review state known.
   **Exit 1 is ambiguous — disambiguate it before acting.** `roborev` returns 1 both when a
   review ran and failed its verdict, and when no review ran at all (`Error: no commits on branch
   since main`, `already on main — create a feature branch first`). Treating the second as
   findings starts a fix round for findings that do not exist. After any nonzero exit, ask which
   happened:

   ```
   roborev list --json   # a job whose git_ref covers the range you just flushed?
   ```

   A record covering the range means a review ran: exit 1 is a verdict, and its findings are work.
   **No record means nothing was reviewed** — report that and continue; it is not a fix round, and
   under ADR-0005 D-12 it is also not a pass, because absence is never a clean review.
   **Findings are work, never a gate** (ADR-0005 D-12): exit 1 produces a fix round triaged under
   ADR-0007, capped by `FIX_CAP` (the recorded `verifier_blocking_gate.auto_fix_max_retries`)
   — the same dial the validator lane uses, read and never redefined; `FIX_CAP=none` asks. A fix means re-running the
   layer's verification before the push, and the flush stays last.

0b. **Acceptance gate, per ticket, before its push.** Run `/dev-execution` Step 4b on each
   ticket's worktree (`../dev-execution/references/acceptance-gate.md`): every acceptance
   criterion PASS with evidence or DEFERRED with a listed reason and when; a criterion verifiable
   before the PR is deferred only on the user's word, asked once per ticket. Push only a ticket
   whose `acceptance_gate.py grade` printed `ACCEPTANCE ok`; its `render` output goes into the PR
   body. A refusal holds that ticket, not the batch. Its dependents wait with it.

1. Push each ticket's branch, in dependency/merge order (layer 0 first).
2. Build the PR the way this repo already expects: if the `generate-pr` skill is available, invoke
   it per ticket — it already detects `.github/pull_request_template.md` / review rules and does
   the self-review. Only if no such convention exists anywhere in the repo, fall back to
   `references/pr-template-fallback.md`.
   `<abs>` here and in Step 10 is the installed plugin's directory, read the way `/pr-sync` Step 1 reads it
   (`$CLAUDE_PLUGIN_ROOT`, else the `installPath` Claude Code records in
   `plugins/installed_plugins.json`), never a checkout: a PR branch could carry its own guard.
   The PR-opened hook asks for `/pr-sync` on each PR. In loop-mode, Step 10 owns the watch:
   do not run that `/pr-sync`, or the PR gets two watches. **If Step 1 item 5 was No**, record
   the No right after `gh pr create` returns each PR:

   ```bash
   sh <abs>/skills/pr-sync/scripts/autopush-guard.sh decline --repo <owner/name> --pr <n> --who "<person>"
   ```

   The decline is absolute: `arm`, `check` and `resolve-threads.sh` refuse the PR until a person
   runs `decline --lift`, so an arming that ran first still pushes nothing.
3. Leave final approval to the user or the named reviewer — never self-approve.

## Step 10 — Optional: PR-comment monitoring loop

After PRs are open, ask the user (multiple choice) whether they want continuous monitoring of PR
comments as they land. If yes, read `references/pr-comment-loop.md` and follow it — in short: arm
**one** `Monitor` watch (`persistent: true`) over the whole batch, running the shared harness
watch script (`skills/pr-sync/scripts/pr-watch.sh`, the same mechanism `dev-execution` Step 5 and
`/pr-sync` use) anchored on the head SHA and armed with the repo's reviewer mode
(`review-mode.sh`, whichever of the four modes the repo records; on `WATCH=off` no watch is armed and the
batch's checks are reported once); each `COMMENT`
event it emits spawns one subagent to identify who/what is commenting (bot vs. human, and which
bot), judge whether it carries real architectural value, and whether it flags a regression against
the shipped diff.
Anything substantive gets escalated to you, the orchestrator, and you take it to the user through
the same decision protocol as Step 7.

**Auto-push (Step 1 item 5).** If the user answered *Yes*, the batch needs the watch: say so when
asking the monitoring question, and pre-select *yes*. If they then decline monitoring, nothing is
armed; say every review-fix push will be asked. Once the watch is armed, arm the guard on every PR
in the batch, with the batch's `--state` and the person who answered:

```bash
sh <abs>/skills/pr-sync/scripts/autopush-guard.sh arm --state <state> --pr <n> --repo <owner/name> --who "<person>" --source flag
```

Show each line verbatim. A refusal (`SOMNIO_PR_SYNC_AUTOPUSH=off`) leaves that PR asked. Every
`worklist=yes` finding then goes through the contract's *Auto-push* cycle, from the ticket's own
worktree (`references/pr-comment-loop.md` step 4). With *No*, arm nothing.

## Communication discipline

Only you, the orchestrator, talk to the user. Subagent output stays internal — you read it,
extract what matters, and report it in the language chosen in Step 0. Format status updates like a
technical console log, not prose: concise, prefixed by ticket key and stage, explicit about state
transitions — e.g. `MH-1234 [Implement] phase 2/3 verify: FAIL exit 1 — 3 test failures in
copilot.spec.ts`. No filler, no "great news!" — this is operational status, not customer-facing
copy.

## Guardrails carried over from prior runs of this pattern

- One grep is never enough for a rename or contract check — direct references, type references,
  string literals, barrel exports, and DI tokens, checked separately.
- Prefer `rebase` over `merge` when stacking a dependent ticket's branch onto its parent.
- Every finding that asserts something about existing code carries a confidence score — enforced
  in the workflow script's schemas, and the same standard applies to anything you say directly to
  the user.
- "Should pass" is never acceptable evidence — the workflow script re-runs the real verification
  command and only commits on an actual zero exit code.
- Every agent in the pipeline verifies its own claims before reporting them (spec file existence,
  real `git status --short` output, not memory of intent) — a phase with zero real changes or more
  than `maxFilesPerPhase` changed files is blocked, never silently committed.
- Before relaunching anything after a session interruption, check real git state first
  (`git worktree list`, `git -C <worktree> status`) and check `/workflows` for a still-active run
  before starting a duplicate.
- **Always pass `args.repoPath` as an absolute path to the Workflow call in Step 6.** A subagent
  spawned by `Workflow` does not reliably inherit the orchestrator's working directory — a dry run
  of this exact skill once created real worktrees and branches inside the wrong repo because
  nothing pinned the target repo explicitly, and it only surfaced because the run was checked
  before trusting it. `loop-mode-workflow.js` now refuses to run without `repoPath` and anchors
  every git command to it with `-C` — never edit that requirement away to save a line in `args`.
- After any Workflow-based run against a real repo (dry run or not), verify with `git worktree
  list` and `git status` that only the expected worktrees/branches exist before reporting success
  — this is the same "verify before claiming" discipline applied to yourself, not just the agents.
- **Tear down worktrees with git, never `rm -rf`, and `--force` only on what this run made.**
  - A detached worktree this run spun up for a shipping-SHA e2e gate: `git worktree remove --force
    <path>`, then `git worktree prune`.
  - Anything else, a killed run's leftover included: plain `git worktree remove <path>`, only
    after the person confirms. Git refuses a worktree with uncommitted or untracked work; that
    refusal stands. Example: `git -C <path> status --short` prints `?? notes.md` — stop and ask.
  - A raw `rm -rf` trips the repo's `rm -rf` ask gate on every run and, on a bad path expansion,
    can delete the wrong tree; `git worktree remove` is scoped to a registered worktree.

## Process

Serves `ENG-EXEC-05` in `docs/PROCESS_MAP.md` (parallel multi-ticket variant of ENG-EXEC-01).

## Run evidence

After delivering the output, record run evidence for the ship gate
(best-effort; never block on it):

```bash
sh "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)/plugins/somnio-engineering-ai}/scripts/ship-marker.sh" loop-mode 2>/dev/null || true
```

In repositories whose `.claude/ship-gate.json` requires this marker,
the ship gate blocks `git push` / `gh pr create` until it is fresh.
