# Templates

## Contents

- Context brief
- Question card
- Decision record
- Ticket update (body rewrite or comment)
- Handoff draft (one per owner)
- Product memo

## Context brief

```markdown
## Where we are
<2-3 sentences: the work, its stage, why these questions exist now. Every ticket
key gets a short gloss on first mention: ACME-834 (close the raw ports once clients moved).>

| ID | Kind | Source | Question | Default | Owner | Door | Depends on | Blocks impl |
|:---|:-----|:-------|:---------|:--------|:------|:-----|:-----------|:------------|
| OD-8 | technical | ticket | <one line> | <default or —> | <owner or unknown> | two-way | — | yes |
| Q-1 | business | chat | <one line> | — | <PO/PM or shipper> | one-way | OD-8 | yes |

**Resolution order:** <IDs, with one line on why>
**What I could not see:** <unreachable tickets, missing files, unknown owners>
```

## Question card

```markdown
### <ID> — <short title>   ·   class: <Decide here | Recommend and route | Escalate | Blocked on a fact | Ask product>

**In plain words.** <What is decided and why it matters, for someone new to the work.>

**What the repo says.** <path:line facts; "nothing found" is a finding.>

**What others do.** <2-4 bullets, each with a link and how close it is to us.>

| Option | What it means | Trade-off | Door | Confidence |
|:-------|:--------------|:----------|:-----|:-----------|
| A. <default> (stated default) | <one line> | <one line> | two-way | 7/10 — <why not higher> |
| B. <alternative> | ... | ... | ... | 5/10 — ... |

**Recommendation:** <option> (<score>/10) — <Precedent: who, link | Repository answers it: path:line | No precedent found: what was searched>. Would change if: <the one fact>.
```

## Decision record

```markdown
# Open decisions — <scope> — <YYYY-MM-DD>

Resolved with: <person, role>. Source: <ticket / PR / ADR links; "this conversation">.
Repository evidence as of: <repo>@<short sha>.

**Implementation verdict: <READY | READY ON DEFAULTS | BLOCKED>**
- Assumptions (non-blocking defaults): <IDs>
- Blocked on: <ID — who or what it waits for>
- Continue with: <e.g. `/dev-execution ACME-833`>

| ID | Outcome | Chosen | Confidence | Decided by |
|:---|:--------|:-------|:-----------|:-----------|
| OD-8 | decided | A. <option> | 7/10 | <person> |
| OD-10b | escalated | — (recommend B) | 6/10 | pending <TL> |

## <ID> — <title>
- **Outcome:** <decided | routed | escalated | blocked | deferred>
- **Blocks implementation:** <yes | no>
- **Chosen / recommended:** <option>
- **Why:** <the person's reason, or the recommendation's>
- **Evidence:** <links and path:line>
- **Follow-ups:** <code change, ADR amendment, ticket>
- **Waiting for:** <only when not decided>

## Findings outside the questions
- <what was found> — <path:line or link> — <who should look at it>

## Still open, by owner
- <owner>: <IDs>
```

## Ticket update (body rewrite or comment)

The ticket's stage decides the channel, as in `youtrack-ticket-hardening`.

**Before In Progress — `<KEY>.new.md`, the exact body to publish** (no wrapper,
no code fence). Start from the current body and keep every fact in it. Change
only what the decisions change:

```markdown
## Open decisions (owner · default-if-unresolved)
- OD-8 — RESOLVED <date> (<who>): <outcome in one line>. Was: <the old default>.
- OD-10b — OPEN, routed to <owner>: <question>. Recommended: <option> (<n>/10).
```

…and in each section a decision touches (Scope, Contract, Acceptance Criteria,
Tests, UAT), the line rewritten to match it, so the spec and the decisions never
disagree. Add a line under the header block: `Open decisions resolved <date>,
record: <link or path>.`

**In Progress or later — `<KEY>.comment.md`:**

```markdown
Open decisions resolved (<date>):
- **OD-8** — <outcome in one line>.
- **OD-9** — <outcome>. Follow-up: <ticket>.
Routed to <owner>: OD-10b (ADR-0013 acceptance).
Full record: <link or path>.
```

## Handoff draft (one per owner)

One message per owner, holding every question routed to them. It must stand
alone: the owner has not seen this session. Technical questions are handed off
here, never as separate memos.

```markdown
Hi <owner> — <TICKET> (<few-word gloss>) has <n> decisions that are yours.

**<ID> — <one-line question>** (default if nobody acts: <default>)
Recommend <option>, <n>/10 — <Precedent: who, link | Repository answers it: path:line | No precedent found: what was searched>. Would change if: <fact>.

**<ID> — …**

Full record: <link or path>.
```

## Product memo

Only for **Ask product** questions. For a product owner, product manager, or
whoever shipped the behaviour. **The body is at most 150 words** — count them,
excluding the greeting line and the To/cc line, and cut until it fits. No file
paths, code identifiers, logins in code format or line numbers: link the ticket.
Only the recommended option carries a score.

```markdown
To: <name> (<role>) · cc: <name> (<why>)

Hi <name> — a product call on <TICKET> (<few-word gloss>).

**Today:** <what users see now, one sentence>
**The question:** <the choice, in product terms>
**Options:**
- A. <effect on users / business>
- B. <effect on users / business>
**We recommend:** <option>, <n>/10 — <one-line reason>.
**If we don't hear back by <date>:** we build <default>, which is <easy / hard> to undo.
**Ticket:** <link>
```
