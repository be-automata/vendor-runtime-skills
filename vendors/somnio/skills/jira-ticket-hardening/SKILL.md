---
name: jira-ticket-hardening
description: >-
  Rewrite a batch of Jira tickets into enterprise-grade, cold-start-ready
  SDD specs. Verified against the target repo's integration branch,
  ADR/SPEC-compliant, self-contained, each with a UAT block and a
  loop-engineering agent-topology block. Rewrites To-Do tickets in place;
  comments on In-Progress+ ones. Use when refining tickets before grooming,
  turning a design handoff into buildable tickets, auditing tickets for
  ADR/self-containment gaps, or when asked to 'harden', 'rewrite
  descriptions', or 'make tickets self-contained'. Not for refining a whole
  epic end to end — verifying links, drafting ADR/gap proposals, planning
  the ticket chain — before development starts; that is dev-epic-refinement,
  which calls this skill per ticket for the rewrite step.
allowed-tools: Read, Grep, Glob, Write, Bash(git *), Bash(ls *), Bash(date *), Agent, ToolSearch, AskUserQuestion, mcp__claude_ai_Atlassian_Rovo__*
---

# Jira Ticket Hardening

Turn thin, drifted, or design-handoff Jira tickets into **observed & verified** specs a cold-start
developer (or an autonomous agent) can execute without opening any external doc.
(Provenance: distilled from a 50-ticket production epic batch.)

## Prerequisites
- Atlassian Rovo connector MCP (`mcp__claude_ai_Atlassian_Rovo__*`): ToolSearch-load
  `getJiraIssue, searchJiraIssuesUsingJql, editJiraIssue, addCommentToJiraIssue,
  getAccessibleAtlassianResources` before starting. If unavailable → STOP with a pointer to the
  Jira-access policy: **somnio-engineering-ai `docs/DEV_HARNESS.md` § Jira access**.
- Derive the cloudId via `getAccessibleAtlassianResources` (same recipe as
  `/dev-epic-refinement` Step 0).
- A target repo whose `docs/adr/`, `docs/specs/`, and `CLAUDE.md` define the conventions to check against.
- Read `resources/pitfalls.md` **before touching Jira** — the WAF/ADF/snapshot rules prevent data loss.

## What this skill does
1. Maps the epic tree, checks every ticket against **ADR criteria · SPEC convention · cold-start
   self-containment · reality on the integration branch** (done / superseded / hardened).
2. Rewrites each To-Do description to a frozen template; **comments** (never rewrites) on In-Progress+ tickets.
3. Adds a repo-appropriate **UAT** block and a **loop-engineering topology** block to every story.
4. Emits a **priority-ranked sort strategy** and, when needed, the committed **companion docs/ADR** the
   tickets cite.

## Modes
- **Interactive (default)**: Phase 3's pilot pauses for user sign-off on the two pilot rewrites.
- **Autonomous**: when the caller (e.g. `/dev-epic-refinement`) passes an explicit autonomous-mode
  instruction, the pilot is **self-verified** instead — re-fetch both pilots, assert the ADF round-trip
  (all sections present, tables rendered, `[ ]` intact) and template completeness; on pass, scale without
  pausing. Every judgment call is recorded with rationale in the ticket's Open Decisions and the run report.
  A failed self-verification still STOPS the run (restore from snapshot, report).

---

## The workflow (6 phases — do them in order)

### Phase 0 — Map & snapshot (before any edit)
- Enumerate the tree — **unless the caller supplied an authoritative ticket map** (e.g.
  `/dev-epic-refinement` passes its `TICKET_MAP`): then use exactly those keys and skip your own
  enumeration. Standalone: `searchJiraIssuesUsingJql` with
  `key in (<epics>) OR parent in (<epics>) OR "Epic Link" in (<epics>)`, fields
  `[summary,status,issuetype,parent,labels]`. Reconcile the count against each epic's own child
  list; on mismatch, STOP and report both counts. Note each ticket's status.
- **Snapshot every target description** (markdown) to a scratch dir — this is the *only* rollback path;
  `editJiraIssue` replaces the whole description with no undo (see `resources/pitfalls.md`).
- Build a **verified citation table**: read each `docs/adr/ADR-0XX-*.md` you intend to cite and record its
  real title + what it governs. Tickets may cite **only** this table. Never hand-type an ADR number
  (that is how phantom refs slip in).

### Phase 1 — Probe reality (parallel Explore agents, read-only)
Launch up to 3 `Explore` agents concurrently to establish ground truth (skip probes the caller already
supplies as frozen inputs — `/dev-epic-refinement` passes its REALITY_REPORT/ARCH_VERDICTS/TEST_PLAN):
- **Codebase**: what already exists on the integration branch (entities, enums, guards, endpoints) → each
  ticket's "reuse/extend vs net-new" verdict. *This is the highest-leverage finding* — it stops tickets
  telling a dev to recreate an existing domain.
- **Docs/convention**: the canonical SPEC template, the UAT template, the loop-engineering format, and the
  ACCEPTANCE rules of the relevant ADRs (route families, guards, cache, AI boundary, errors).
- **Ticket sampling**: fetch a representative BE/FE/AI ticket + the foundation epic to gauge current gaps.

### Phase 2 — Plan, then review the plan
Write the plan (findings + rewrite template + sort strategy). Then **review it against sub-agents before
executing**: `system-architect` (harness plugin agent — route family, bounded-context seam, cross-repo
boundaries) and a fresh `general-purpose` agent prompted as an adversarial reviewer (regression risk,
backup story, phantom-ref audit). If a named agent type does not resolve, substitute a `general-purpose`
agent carrying that role's prompt — never skip the review pass. Integrate blocking feedback. The
architecture reviewer catches the load-bearing errors.

### Phase 3 — Pilot gate
Rewrite **one or two representative tickets** — a BE + FE pair when both types are present in the
batch; otherwise one ticket of the batch's type (a single-ticket batch pilots itself). Re-fetch to
confirm the ADF round-trip (tables survive). Interactive mode: get user sign-off on the shape.
Autonomous mode: self-verify per **Modes** above. Only then scale. This de-risks the template
before it is stamped N times.

### Phase 4 — Scale in bounded waves
Fan out fresh `general-purpose` rewrite agents **by epic**, ~5–8 tickets each, all anchored to the same frozen template +
citation table + the two approved pilots as exemplars (kills cross-batch drift). Order waves by the sort
strategy (foundations first). Each agent, per ticket:
- Re-fetch immediately before editing; anything no longer To-Do: **skip the rewrite, post the
  hardening deltas as a comment instead** (per the status-gating rule), and flag it in the manifest.
- `editJiraIssue` (markdown); re-fetch after to confirm all sections + tables rendered.
- **On a failed post-edit verification** (missing sections, mangled tables, or a WAF-blocked
  call): restore the ticket from its snapshot, check the body for WAF-triggering strings
  (literal `curl` bearer commands), retry **at most once**, then flag the ticket and move on.
  Never loop on a failing edit.
- **No comments, no status changes** on To-Do tickets.

### Phase 5 — Verify, sort, companion docs
- **Exhaustive** verify sweep: re-fetch *every* ticket; confirm all updated, none silently truncated;
  re-check that no In-Progress ticket was overwritten.
- Publish the **priority sort strategy** (P0→Pn, highest first — rank by blocking-power then value then
  readiness).
- **Output contract to the caller**: return the snapshot directory path and a per-ticket verify
  manifest (key · rewritten yes/no · sections verified · flags) — the caller's final
  verification and any rollback depend on both.
- If tickets reference uncommitted design docs or a missing ADR, author the **companion docs/ADR as a
  separate PR and merge it *before* tickets cite it as ratified** (never cite an unmerged ADR).

---

## The two hard rules
- **Status gates the channel.** To-Do → rewrite the description. In-Progress or beyond → **post a comment**
  with the hardening deltas; do not overwrite an active ticket's body.
- **Observed & verified.** Every code path, enum value, line number, and ADR citation is re-grepped against
  the working tree at rewrite time — the integration branch moves and hand-transcription is the top source
  of new inaccuracy.

## The per-ticket template & UAT
Use `resources/rewrite-template.md` — the frozen section order (Reality-check → Open decisions[owner+default]
→ Scope → contract → ADR conventions → AC → Tests → UAT → Security&Audit → Observability → Loop-Engineering
→ DoD), the BE-vs-FE UAT case shapes, and the epic "flow-overview" variant.

## The loop-engineering block
Use `resources/loop-engineering-block.md` — the Orchestrator / frontier Spec-Writer / tiered-worker topology,
the model-assignment rule ("only the Spec Writer is frontier; everything else as cheap as the task allows"),
the skills mapping, and the scorecard stub.

## Pitfalls that cause data loss or CI failures
Read `resources/pitfalls.md`: snapshot-before-overwrite, ADF `[ ]`/table fidelity, the
**Cloudflare-WAF-blocks-literal-`curl`** gotcha, branch/worktree safety when authoring companion docs, and
the verified-citation-table discipline.

## Validation checklist (before declaring done)
- [ ] Ticket count reconciled; every target snapshotted.
- [ ] Every citation resolves to a real ADR in the citation table (zero phantom refs).
- [ ] To-Do rewritten / In-Progress+ commented — no active ticket overwritten.
- [ ] Each story: reality-check + ODs(owner+default) + contract + Tests≠UAT + UAT + loop-eng block + DoD.
- [ ] BE UAT uses plain HTTP request lines (no literal `curl -H "Authorization: Bearer"`).
- [ ] Post-edit re-fetch confirmed sections + tables survived ADF on every ticket.
- [ ] Priority sort strategy published; companion ADR/docs merged before any ticket cites them.

## Process

Serves `ENG-PLAN-03` in `docs/PROCESS_MAP.md`.

## Run evidence

After delivering the output, record run evidence for the ship gate
(best-effort; never block on it):

```bash
sh "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)/plugins/somnio-engineering-ai}/scripts/ship-marker.sh" jira-ticket-hardening 2>/dev/null || true
```

In repositories whose `.claude/ship-gate.json` requires this marker,
the ship gate blocks `git push` / `gh pr create` until it is fresh.
