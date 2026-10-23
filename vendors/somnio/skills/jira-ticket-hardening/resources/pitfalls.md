# Pitfalls — read before touching Jira or authoring companion docs

These are the traps that caused (or nearly caused) data loss and CI failures in production hardening runs.

## 1. `editJiraIssue` replaces the WHOLE description — no undo
There is no per-field history in the API. Before editing anything, `getJiraIssue` (markdown) **every** target
and persist it to a scratch dir. That snapshot is the only rollback path. On a bad/truncated rewrite, restore
from it. Verify **every** ticket post-edit — not a sample; a silent table-mangle on an unsampled ticket is
unrecoverable without the snapshot.

## 2. Cloudflare WAF blocks literal `curl` bearer commands
A request body containing `curl -H "Authorization: Bearer …"` is read as an attack payload and the
`editJiraIssue`/`addCommentToJiraIssue` call is blocked (silent failure / retry loop). Write UAT/API commands as
**plain HTTP request lines** + an `Authorization: Bearer <jwt>` header line in prose. FE tickets (no curl) never
trip it. (Same class as the create-only/WAF notes in the Atlassian MCP.)

## 3. ADF round-trip fidelity
`contentFormat: "markdown"` round-trips through ADF. Markdown **tables** and nested lists survive; inline `[ ]`
comes back escaped as `\[ \]` and renders as literal brackets (fine — a visual checklist). After each edit,
re-fetch and confirm the contract tables rendered before moving on.

## 4. Status can flip mid-run — re-fetch before every edit
A ticket that was To-Do at Phase 0 may be In-Progress by the time a batch agent reaches it (a dev picked it up).
Re-fetch immediately before editing: if it left To-Do, **skip it and post a comment** with the deltas instead of
overwriting an active body. (A rewrite that lands *before* the flip is fine — the status change doesn't revert
the body.)

## 5. Never cite an unmerged ADR / phantom refs
Build the verified citation table (Phase 0) by reading each ADR file. Tickets cite only from it. If the hardening
needs a new governing ADR, hold it as an Open Decision marker ("pending ADR-0NN") until that ADR **merges** —
then upgrade the markers to citations. If the target repo has an ADR collision registry or uniqueness script
(e.g. a collision section in `docs/adr/README.md`, a `check-adr-unique`-style script), reserve the number
through it before authoring. Amend an existing ADR **inside** that ADR file (an `## Amendments` section),
never as a new `ADR-0NN-A1-*.md` — uniqueness tooling typically reads the filename prefix as a duplicate id.

## 6. Branch / worktree safety when authoring companion docs (Deliverable 2)
The main checkout may be on the user's active feature branch, and the user may run their own git ops concurrently
(commits, branch switches, `git stash`) — which can sweep your untracked doc files into a stash. To stay safe:
- Author companion docs in a **separate `git worktree`** off `origin/develop`, never on the user's branch.
- If files were stashed by a branch switch, extract **only your paths** from the stash (`git checkout stash@{0}^3
  -- <untracked paths>` for new files; `git checkout stash@{0} -- <paths>` for tracked-WIP) — do **not** pop/drop
  the stash (it holds the user's other WIP too).
- The target repo's pre-push hook may run a full lint+test suite; for a docs-only branch `--no-verify` is a
  sanctioned bypass only when the **user** authorizes/runs it (auto-mode blocks self-authorized verification
  bypass).

## 7. Verify the plan with adversarial sub-agents before executing
The plan's own author misses things. Route it through `system-architect` (harness plugin agent) plus a
fresh `general-purpose` agent prompted as an adversarial reviewer first (substitute a prompted
`general-purpose` agent for any role that doesn't resolve as a named agent type). The
architecture pass is where load-bearing errors surface (e.g. "this resource carries no place_id → it is NOT
Family B; it needs a portfolio-*filtered* global-staff guard"). Integrate blocking feedback into the frozen
template before the pilot, so 50 tickets don't inherit a wrong decision.
