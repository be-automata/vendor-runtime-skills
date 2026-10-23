# Idempotent link wiring

The link pass must be safe to re-run: a second `/dev-epic-refinement` on the same epic creates
zero duplicate links and deletes nothing.

## Algorithm

1. **Fetch** — `TICKET_MAP` already carries each ticket's links (Jira `issuelinks`, YouTrack
   `ytctl links KEY --json`). Normalize every existing link to a triple
   `(inwardKey, linkTypeName, outwardKey)`.
2. **Resolve types once** — Jira `getIssueLinkTypes`, YouTrack `ytctl link-types`; use the
   *names and directions the instance reports* (Jira typically `Blocks`: outward "blocks", inward
   "is blocked by"; YouTrack `Depend`: "is required for" / "depends on"; `Relates`: symmetric).
   Never hand-write a type name or phrase that the instance did not return.
3. **Diff** — desired graph (from `CHAIN_PLAN`) minus existing triples = `LINK_DELTA`. Treat
   `Relates` as symmetric when diffing (A→B equals B→A). A `Blocks` in the desired direction that
   already exists in the opposite direction is NOT a duplicate — it is a suspected-wrong link.
4. **Add only** — one create per missing triple (Jira `createIssueLink`, YouTrack
   `ytctl command "<phrase> <OTHER>" KEY`). **Never delete or modify** an existing
   link; suspected-wrong links (wrong direction, obsolete dependency) are listed in the
   refinement report for the TL to resolve in the tracker.
5. **Verify** — re-fetch each touched ticket and assert the new links are present. Any miss is
   retried once, then reported.

## Notes

- Blocked tickets: after wiring, tickets with an unresolved `is blocked by` on an unshipped
  dependency should be called out in the report (the target team's convention may move them to a
  blocked/backlog state — this skill reports, it does not transition status).
- Cross-project links (e.g. MH ↔ MAR) are allowed in the graph but flagged in the report.
