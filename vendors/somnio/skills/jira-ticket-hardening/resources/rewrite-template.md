# Rewrite template & UAT shapes

Freeze this once at the start of a run and have every batch agent fill the **same** skeleton verbatim.
Branch by repo using the target repo's summary prefixes (from its `CLAUDE.md` — e.g. a Marketplace
run uses `[BE]` apps/api · `[FE]` apps/web · `[AI]` the AI-services repo).

## Story template (section order is load-bearing)

```
> Epic: <parent> · Repo: <repo> · Done = merged to <integration branch> · Depends on: … · Reused by: …

## Summary
## Reality check on origin/develop      # reuse/extend vs net-new, with file:line evidence
## Open decisions (owner + default-if-unresolved)   # ABOVE the AC; tag dependent AC lines "(pending OD-N)"
## Scope
## API / Data contract   # BE/AI: method/path/guard/body/success/errors table. FE: props/state/query.
## Conventions (ADR-anchored)            # cite ONLY the verified citation table
## Acceptance Criteria
## Tests   # unit/integration expectations, AC-linked — DISTINCT from UAT; this is what gates the target repo's test command
## UAT     # see shapes below — branch by repo
## Security & Audit (<audit ADR>)   ·   ## Observability   # slots forced; "N/A + reason" is allowed
## Loop-Engineering Strategy         # see loop-engineering-block.md
## Verification / DoD                # the target repo's verification command; + its API-contract check for BE endpoints
## Out of scope   ·   ## Dependencies
```

Resolve convention-settled questions **in-ticket** (cite the house rule); reserve Open Decisions for genuine
product/architecture questions, each with an **owner** and a **default-if-unresolved** so a blocked dev is never stuck.

## Epic variant — "flow-overview" (do NOT stamp the story template on epics)
`goal · child-story map · the feature's hard rules · cross-flow reuse seams · dependencies (+ a Blocked column
when a child is gated on unshipped infra)`.

## BE / AI UAT case shape (command + Pass/Fail + evidence)
```
## UAT-1 — <one-line assertion>
**Preconditions:** migration/seed state; how the JWT is minted.
**Command:** plain HTTP request line (e.g. `POST /api/v1/<...>`) + an `Authorization: Bearer <jwt>` header
             line in prose.  ⚠️ NEVER a literal `curl -H "Authorization: Bearer …"` — the WAF blocks it.
**Expected:** exact status + body/invariant.  ✅ PASS / ❌ FAIL — **Evidence:** response body / DB row / log.
```

## FE UAT case shape (interaction → observable → evidence)
```
## UAT-1 — <observable assertion>
**Preconditions:** account/role, target entity, URL, seed state (per the target product's domain).
**Steps:** ordered interactions.
**Expected (observable):** [ ] visual assertion  [ ] state/reorder  [ ] optimistic + revert-on-failure
                           [ ] network call fires once (body/shape)
**Evidence:** screenshot + HAR / Playwright trace.  Result: ✅ PASS / ❌ FAIL
```
`[ ]` renders as literal brackets after the ADF round-trip (a clean visual checklist) — keep it, don't switch glyphs.

## Corrections a hardening pass typically bakes in
- "Define new X" → "**extend** the existing `<path>`" when the probe found X already ships.
- Wrong route family / envelope / pagination → the ADR-correct shape; flag any deviation from `CLAUDE.md` as a
  doc-bug follow-up (don't silently diverge).
- Cross-repo work (AI) → name the seam explicitly (client + service-bearer), don't imply it lives in the monorepo.
- Blocked-by-unshipped-infra → an explicit dependency, and cap the rewrite depth on genuinely blocked tickets.
