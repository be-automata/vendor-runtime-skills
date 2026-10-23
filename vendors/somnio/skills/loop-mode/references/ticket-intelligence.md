# Ticket intelligence pass

Runs once, right after the ticket set is confirmed (Step 1) and before the dependency graph is
locked in for layering (Step 5). Its job: replace whatever the user *thinks* they know about
acceptance criteria, tracker dependencies, and codebase fit with what's actually true — surfaced to
them, not assumed on their behalf. This is the mechanism that "eliminates gray areas" before any
spec gets written: a spec built on a stale or wrong premise is a gray area with a nice format.

## One research subagent per ticket, run in parallel

For each ticket, spawn a read-only research subagent (Agent tool, no code changes — Explore or
general-purpose, whichever this session has available) with:

1. **Acceptance criteria / Definition of Done.** Pull it from the ticket's actual tracker description
   (the adapter in `../../dev-execution/references/tracker-adapters.md`) rather than trusting the one-line scope the user gave in Step 1 —
   AC and DoD are usually far more specific than the scope summary, and the spec (Step 6) needs
   them verbatim, not paraphrased.
2. **Tracker issue links.** Read the ticket's links (Jira `issuelinks`, YouTrack `ytctl links`,
   GitHub "blocked by") for every "blocks" / "is blocked by" / "depends on" / "relates to"
   relationship to another issue, and fetch the current status of each linked issue, normalized to
   its status class (`todo`, `in_progress`, `done`, `other` — ACME's `PR Merged` is `done` only when
   `DONE_STATES` says so and the commit is on the integration branch) —
   check **any** status, not just an obvious "blocked" one, since a linked ticket that's merely
   "In Progress" can still mean its contract isn't stable yet.
3. **Cross-check against the user's declared dependency graph** (Step 1, input #2). Flag:
   - a tracker-linked blocker that ISN'T in the user's graph — this is the dangerous case, since the
     user's graph is what drives layering and worktree base-branches.
   - a user-declared dependency that the tracker's own links don't show — not necessarily wrong (the
     user may know something the tracker doesn't track), but worth surfacing so it's a conscious choice, not
     an assumption.
4. **Lightweight codebase probe.** Grep for the entities, files, endpoints, or modules the ticket's
   scope/AC actually names. This is a fast triage, not the full contract audit the spec-validation
   gate (Step 6) already does — the goal here is catching an obvious "this ticket assumes X and X
   doesn't exist in this repo at all" before spending an expensive spec-generation cycle on it, not
   re-doing that deeper audit.

Return one structured report per ticket: `{ ticketKey, ac, dod, trackerBlockers: [{key, status,
linkType}], graphMismatches, codebaseSmells, confidence-scored per finding }`.

## Reconciling before layering locks

Once all reports are back, the orchestrator reviews them together (not one at a time — a blocker
on ticket B might change how ticket A's layer should be ordered) and escalates through
`decision-protocol.md` **before** Step 5's topological sort runs, for any of:

- a tracker blocker not in the declared graph
- a declared dependency the tracker doesn't corroborate
- a blocking ticket that isn't `done` — ask whether to wait, proceed anyway with the current state
  as a known risk, or drop that ticket from this batch
- a codebase smell suggesting the ticket's premise doesn't hold in this repo right now

Don't ask one scattershot question per finding — batch the reconciliation into as few
AskUserQuestion calls as the findings allow, grouped by ticket, each with your researched
recommendation per the decision protocol. Silence (no findings) means proceed straight to Step 5
with the user's original graph unchanged — this pass exists to catch problems, not to manufacture
questions where there are none.
