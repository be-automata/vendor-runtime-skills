# The acceptance gate

## Contents

1. Extract
2. Verify each criterion, one at a time
3. Defer only with a reason and a time
4. Grade
5. Carry it into the PR and the hand-off

PRs were merged with acceptance criteria nobody checked. Some of those criteria could have been
verified on the dev's machine, or against develop, before the PR opened. Not every criterion can
be met when a PR opens, and that is expected. What the gate refuses is a criterion **nobody looked
at**.

Script: `scripts/acceptance_gate.py`. It runs after Step 4 and before the push. It grades against
the worktree and the ticket, never against the matrix's claims about itself.

## 1. Extract

Save the ticket body to `<state>/<KEY>/ticket.md`, the same text Step 1 read:

```bash
uv run --script <abs>/skills/dev-execution/scripts/acceptance_gate.py extract \
  --body <state>/<KEY>/ticket.md --out <state>/<KEY>/acceptance.json
```

Every `## Acceptance Criteria` item becomes a row, `PENDING`. Its tier comes from the ticket:

| Tier | Read from | Can be verified before the PR |
|:--|:--|:--|
| `local` | a `## Tests` line naming the AC, a T0/T1a UAT row, "local stack" | yes |
| `device` | a T3 / simulator / agent-device UAT row | yes, when a simulator is available |
| `develop` | a T1a-dev / T1b / acme-dev / develop / staging UAT row | only by deploying the branch, which is a person's call |
| `production` | a T2 / live / production row | no |
| `manual` | a row owned by QA or a person | no |
| `unmapped` | nothing names the AC | treated as yes |

`grade` re-reads the ticket. A row dropped from the matrix is refused as `missing`. A tier
relabelled in the matrix does not change what the ticket says. Only top-level items are criteria;
an indented sub-bullet belongs to the one above it.

Limit: the ticket `grade` re-reads is the copy Step 1 saved. The gate defends against edits to
the matrix, not to that copy, so `ticket.md` is written once, from the tracker, and never edited.

## 2. Verify each criterion, one at a time

Work every row. For each one, pick the cheapest way to verify it that is still real:

- **local:** the test that proves it (`{"kind":"test","path":…,"name":…}`) plus the coverage gate
  (`{"kind":"command","command":"<GATE_COVERAGE>"}`); or the repo's local UAT stack, with its
  report path as `{"kind":"file",…}`.
- **device:** the mobile lane of `/uat`, or the repo's simulator runner. Evidence is the
  screenshot or `device-evidence.json` path.
- **develop:** check whether the change can be verified against develop without deploying it.
  Examples: a read-only call to an endpoint the change does not touch, or a contract
  check against the deployed swagger. If it can, do it. If it needs the branch deployed, ask the
  person. That deploy is externally visible. Otherwise defer it as `needs-deploy`.
- A criterion found failing is `FAIL`. Fix it in this ticket when it is in scope, then re-run
  Step 3.4's gates and Step 4. Otherwise stop and ask. Never mark it met.

Write each result into the row: `status`, `evidence[]`, or `defer`.

What `grade` accepts as a PASS:
- at least one `command` or `file` item. A `test` name or a `url` only corroborates.
- a `command` must be one of the repo's `GATE_*` commands. `grade` runs it in the worktree, once
  per command, and ignores any `exit` written in the matrix. `true` is not a gate command.
- a `file` or `test` path is a file, relative to the worktree, never absolute or with `..`.

## 3. Defer only with a reason and a time

`defer` = `{"reason", "when", "confirmed_by"}`. Reasons, closed list:

| Reason | Use when |
|:--|:--|
| `needs-deploy` | it can only be observed on develop/staging after merge |
| `needs-production` | it can only be observed on production |
| `needs-device` | no simulator or device on this machine |
| `blocked-by-dependency` | it waits on a ticket that has not shipped (name it in `when`) |
| `pending-decision` | it hangs on an open decision (name the OD in `when`) |
| `manual-qa` | QA owns the check by the ticket's own UAT table |

`when` says when and how it will be verified. Example: `after merge: /uat ACME-900 on acme-dev`.

**A criterion that can be verified before the PR** (`local`, `device`, `unmapped`) is deferred
only with `confirmed_by`, the person who agreed. Put those rows to the person in one
`AskUserQuestion` per ticket. List each one with what it would take to verify now. The
exceptions are `blocked-by-dependency` and `pending-decision`: they are facts, not choices.

## 4. Grade

```bash
uv run --script <abs>/skills/dev-execution/scripts/acceptance_gate.py grade \
  --matrix <state>/<KEY>/acceptance.json --body <state>/<KEY>/ticket.md --root <worktree>
```

`ACCEPTANCE ok met=<n> deferred=<n>` lets the ticket ship. Each `ACCEPTANCE refuse <reason>` line
is work:
- `ignored`, `missing`: verify the row or defer it;
- `failed`: fix it or stop;
- `bad-evidence`, `no-evidence`: produce real evidence (a gate command that exits 0 when the gate
  runs it, or a file);
- `unconfirmed-defer`: ask the person;
- `bad-defer`: give a listed reason and a `when`.

The gate never softens. A refusal is not bypassed by editing the matrix, and a `--hold-ship`
report carries the refusals as they are.

## 5. Carry it into the PR and the hand-off

- `render` prints the PR body's `## Acceptance criteria` section: the criteria met with their
  evidence, and the deferred ones with why, when, and who agreed. A deferred criterion is never
  ticked.
- The run report lists every deferred row as a follow-up. Each `needs-deploy` row becomes a
  `/uat <KEY>` to run once develop carries the merge. The ticket is not done until it runs.
  Say so when handing the merge to the person.
- With `TRACKER=youtrack` or `jira`, offer one ticket comment that lists the deferred criteria.
  Post it only on the person's yes.
