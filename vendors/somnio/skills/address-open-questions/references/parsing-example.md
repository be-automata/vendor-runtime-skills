# Parsing example

## Contents

- Input from a ticket (OD-8 to OD-12)
- Input from the conversation

A real input from a acme-core ticket, and how Step 1 parses it. This shows the
parse only; it carries no research and no recommendation.

## Input

```
New open decisions, each with a default (owner: backend lead)
- OD-8: TRUSTED_PROXY_CIDRS is a literal in the bootstrap, not an SSM parameter.
- OD-9: this PR sets keepAliveTimeout: 125_000 in both servers; ACME-833 takes it over later.
- OD-10: amend ADR-0013 (D-5, D-8, stale line references). Moving it to Accepted is your call in that PR.
- OD-11: log the raw Host header, and ACME-834's count excludes /health.
- OD-12: add a second HTTPS uptime check instead of re-pointing the existing one, since the raw path stays live until ACME-834.
```

## What to notice

- **Each line is a default, not a question.** Restate it as a choice so an
  alternative is visible: "literal in the bootstrap **or** an SSM parameter".
- **One owner covers the set.** "owner: backend lead" applies to every line.
  If the person running the skill is not the backend lead, every question is
  **Recommend and route** unless they say the lead delegated it.
- **Compound lines split.** OD-10 holds two decisions with different owners
  (amend the ADR; accept it). OD-11 holds two unrelated decisions (what to
  log; what a metric counts). Split them into `OD-10a/b`, `OD-11a/b` and keep
  the original ID as the prefix so the record maps back.
- **Linked tickets become dependencies.** ACME-833 and ACME-834 are read in
  Step 2; OD-11b and OD-12 both hinge on ACME-834's scope, so they are resolved
  together.
- **Identifiers become greps.** `TRUSTED_PROXY_CIDRS`, `keepAliveTimeout`,
  `ADR-0013`, `/health` are searched in the repository before any option is
  scored.

## Parsed

| ID | Question as a choice | Default | Owner | Door | Linked | Research |
|:---|:---------------------|:--------|:------|:-----|:-------|:---------|
| OD-8 | Where do trusted proxy CIDRs live: code literal or SSM parameter? | literal | backend lead | two-way | — | full |
| OD-9 | Set the HTTP keep-alive timeout in this PR, or leave it to ACME-833? Is 125 s right? | 125 000 ms now, both servers | backend lead | two-way | ACME-833 | full; likely blocked on the load balancer's idle timeout |
| OD-10a | Amend ADR-0013 (D-5, D-8, line refs) in this PR, or in a separate PR? | amend here | backend lead | two-way | ADR-0013 | light |
| OD-10b | Move ADR-0013 to Accepted? | owner's call | backend lead / TL | one-way | ADR-0013 | none — escalate (ENG-PLAN-02) |
| OD-11a | Log the raw Host header, a sanitized one, or none? | raw | backend lead | two-way | — | full (log injection, PII) |
| OD-11b | Does ACME-834's count exclude /health? | exclude | backend lead | two-way | ACME-834 | light |
| OD-12 | Add a second HTTPS uptime check, or re-point the existing one? | add a second | backend lead | two-way | ACME-834 | full |

Resolution order: OD-10b first (one-way, escalated, so its draft goes out
early), then OD-9 (blocked fact to chase), then OD-11b with OD-12 (shared
ACME-834 scope), then the remaining two-way doors, each on its own.

## Input from the conversation

Run with no argument after the person picked a ticket and the session discussed
it. Step 1 merges the ticket's Open decisions section with what the
conversation left unanswered, and quotes where each came from:

```
Collected 3 open questions — confirm before I continue:

| ID   | Source                         | Question as a choice                                         |
|:-----|:-------------------------------|:-------------------------------------------------------------|
| OD-9 | ACME-833 › Open decisions        | Set keepAliveTimeout in this PR, or leave it to ACME-833?     |
| Q-1  | chat, assistant, after reading the bootstrap | Read the CIDRs once at boot, or on every request? |
| Q-2  | chat, person: "not sure we even need the second check" | Keep OD-12's second uptime check at all?     |
```

What to notice:

- **Unanswered only.** A question the person answered two turns later ("ok,
  once at boot is fine") is settled — drop it, and say it was dropped.
- **Restate chat questions as choices**, the same as ticket items. "Not sure we
  even need the second check" becomes "keep it or drop it".
- **A chat question can reopen a ticket item.** Q-2 challenges OD-12's default;
  it joins OD-12 rather than becoming a separate decision.
