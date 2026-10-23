# Refinement report template

Write to `docs/specs/<EPIC_KEY>/refinement-report.md` in the target repo. Keep the section order
frozen so runs are comparable across epics.

```markdown
# Epic refinement report — <EPIC_KEY>

- Run date: <YYYY-MM-DD> · Mode: <full | dry-run> · Integration branch SHA: <sha>
- Operator: /dev-epic-refinement (harness) · Tracker: <jira | youtrack> · Hardening engine: <jira-ticket-hardening | youtrack-ticket-hardening>

## Verdict
<"Bullet-proof: every ticket self-contained, linked, and reality-checked." — or the exact list of
what remains and why.>

## Ticket map (before → after)
| Key | Type | Status | Reality verdict | Rewritten | Notes |
|---|---|---|---|---|---|

## Link graph
<final Blocks/Relates graph; added links marked NEW; suspected-wrong existing links listed with
rationale — never deleted by the run>

## Gaps resolved
- Created tickets: <keys + one-line why>
- api-contracts authored: <paths>
- ADR proposals (pending TL ratification): <titles + affected tickets carrying the Open Decision>

## Auto-decisions (autonomous mode)
| # | Decision | Rationale | Recorded in |
|---|---|---|---|

## Escalations (Confusion Protocol)
<none, or the questions raised and their answers>

## Evidence
- Reality probes: <Plan/Explore summaries, file:line anchors>
- FE evidence (gstack browse): <flows checked, screenshots location — no secrets>
- Hardening verify sweep: <N tickets re-fetched, all intact | failures + resolution>
```
