# Findings and comment templates

The shapes `/uat` drafts for the report's Findings section and for the two default
destinations (a comment on the ticket or epic in the recorded tracker, a PR comment). Drafts are shown verbatim to the person;
nothing is posted without their confirmation, and a ticket is never transitioned.

## Contents

- Severity scale
- Finding entry
- Ticket comment draft
- PR comment draft
- Epic roll-up variant
- Unscoped-run variant
- Posting protocol

## Severity scale

| Severity | Meaning | Typical consequence |
|:---------|:--------|:--------------------|
| CRITICAL | The delivered flow does not work for its user, or corrupts/exposes data | verdict FAIL; blocks release |
| HIGH | An acceptance criterion fails, or a prior UAT case regressed | verdict FAIL |
| MEDIUM | Works, but deviates from the spec/contract in a way a user or integrator would notice | verdict FAIL unless the deviation is accepted in the Deviations table |
| LOW | Cosmetic, copy, or non-blocking inconsistency | recorded; verdict unaffected |
| INFO | Observation, drift item with no user-visible effect, performance note | recorded |

Confidence 1–10 follows the validate-gate calibration: 9–10 reproduced directly with evidence ·
7–8 strong pattern match · 5–6 needs a human to confirm · ≤ 4 speculative (do not post ≤ 4 as
a finding; list it under "observations").

## Finding entry

```markdown
### F-2 [HIGH] (confidence 9/10) — Renewal notification link 404s from the bell panel
- **Criteria:** C1 (spec §8.2), R3 (prior UAT UAT-develop-e2e-…-2026-08-18, case B-4)
- **Lane / origin:** web · executed case (or: production-validator · Terraform drift)
- **Reproduction:**
  1. Log in as staff admin on <ENV.web>.
  2. Open the notifications bell; click the first `lease_renewal` row.
  3. Observe the URL.
- **Observed:** navigates to `/staff/renewals?leaseId=…` → 404.
- **Expected:** `/dashboard/residents/<id>` (spec §7, MH-2185 AC-2).
- **Evidence:** `07-web-C1-bell-panel-open.png`, `08-web-C1-click-404.png`, `09-web-C1-console.txt`
- **Suggested owner:** Dev (web) · **Related:** MH-2185
```

## Ticket comment draft

Plain markdown in the tracker's dialect: Jira markdown for Jira, Markdown for YouTrack and GitHub.
No literal `curl … Bearer` lines: the Cloudflare WAF in front of Jira blocks them, and a token
shape in any tracker is a leak.

```markdown
*UAT run — <ENV> — <date> — verdict: <PASS|FAIL|BLOCKED|UNSCOPED>*

Report: <ARTIFACT_URL>
Build: <revision> / image <sha> (contains PR #<n> merge <sha>)
Cases: <n> total · <p> pass · <f> fail · <b> blocked (<r> re-run from prior UAT)

*Findings*
- F-1 [HIGH] <title> — <one line> — evidence in report §Evidence
- F-2 [MEDIUM] <title> — …

*Deviations from spec* (from the PR body and the diff)
- <spec said> → <PR delivered> — <consequence> (<accepted|F-n>)

*Terraform drift:* <none | n items, see report | not applicable>

Run record: `docs/uat/UAT-<scope>-<date>/` (branch <branch>).
_Drafted by /uat; posted by <role> after review._
```

## PR comment draft

GitHub markdown; the same content, with the criteria table condensed and evidence file names
linked to the run record path once it is committed.

```markdown
## UAT — <ENV> — <date> — **<VERDICT>**

**Report:** <ARTIFACT_URL>
**Build under test:** `<revision>` · image `<sha>` · merge `<sha>`
**Lanes:** web · api

| Criterion | Source | Kind | Verdict |
|:--|:--|:--|:--|
| C1 <text> | spec §8.2 | NEW | ✅ PASS |
| C3 <text> | PR body | NEW | ❌ FAIL → F-1 |
| R2 <text> | prior UAT 2026-08-18 B-4 | RERUN | ✅ PASS |

### Findings
- **F-1 [HIGH]** <title> — repro in the report; evidence `08-web-C1-click-404.png`

### Deviations
- <spec said> → <PR delivered> — <consequence>

### Terraform drift
<none | list | not applicable>

_Run record: `docs/uat/UAT-<scope>-<date>/`. Drafted by /uat; posted after human review._
```

## Epic roll-up variant

For an epic run, the epic comment carries the per-child table (key · PR · verdict · findings
count · report anchor) plus the **Excluded** table (key · status · reason). Each child ticket
gets its own comment draft with only its findings.

## Unscoped-run variant

Header line becomes `UNSCOPED UAT run — <ENV> — <date>`; the first paragraph states what could
not be verified (no acceptance criteria, no deviation detection). There is no default
destination; the draft is offered for the person to paste wherever they bind it.

## Posting protocol

1. First run of the session: `AskUserQuestion` — (a) review the Artifact first · (b) post now ·
   (c) fix and re-run. Persist as `POST_MODE`.
2. On (b): show both drafts verbatim; ask one confirmation per destination
   ("Post this to MH-2185?" / "Post this to PR #912?"); post only what was confirmed, with
   the tracker's comment operation (Jira `addCommentToJiraIssue`, YouTrack `ytctl comment KEY`,
   which reads the stored text back, GitHub `gh issue comment N`) and `gh pr comment`. Record the posted URLs in `run.json`.
3. On (a) or (c): write the drafts to `RUN_DIR/drafts/ticket-comment.md` and
   `RUN_DIR/drafts/pr-comment.md`; nothing is posted.
4. Never transition the ticket. Post-merge tracker administration is ENG-QA-01 (Automata / a
   person).
