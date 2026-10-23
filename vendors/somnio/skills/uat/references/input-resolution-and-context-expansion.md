# Input resolution and context expansion playbook

How `/uat` turns whatever it was handed into a scoped, reproducible run: the input precedence,
the eligibility states, the traversal that builds the Context Frame, and the prior-UAT
selection rule.

## Contents

- Input precedence and parsing
- Fetching per tracker
- Target repo resolution
- Eligibility states (exhaustive)
- Epic fan-out
- The unscoped fallback
- Context expansion traversal
- Prior-UAT selection
- The Context Frame (output shape)
- Ambiguity stop conditions

## Input precedence and parsing

| Rank | Input | Recognizer | Resolves to |
|:-----|:------|:-----------|:------------|
| 1 | Ticket | `^[A-Z][A-Z0-9_]*-\d+$`, `/browse/<KEY>` (Jira), `/issue/<KEY>` (YouTrack) or `/issues/<n>` (GitHub), not an epic | `TICKETS=[KEY]` |
| 2 | PR | `github.com/<owner>/<repo>/pull/<n>`, `#<n>`, or a bare integer | `PR`, then `TICKETS` from branch/title/body |
| 3 | Epic | key/URL whose type is Epic (Jira issuetype, YouTrack `Type`), or one with children | `TICKETS = eligible children` |
| 4 | Free-form plan | anything else (a path, a paragraph) | `INPUT_KIND=plan` → unscoped fallback |

A ticket and a PR given together are cross-checked: the PR must reference the ticket, or the
mismatch is reported and the person picks one.

PR → ticket resolution order: branch name (`feat/MH-2185-…`), PR title (`[MH-2185] …`), PR body
(`MH-2185`, a tracker URL). First key wins; extra keys are listed as "also mentioned".

Ticket → PR resolution: search every candidate repo (`gh pr list --repo <r> --state all
--search <KEY>`), then **keep only PRs whose branch name, title, or body contains `<KEY>`** —
GitHub's search is fuzzy and returns neighboring tickets (observed: searching MH-2281 returned
MH-2278's PR). No surviving PR means "no PR", never "use the closest one".

## Fetching per tracker

`TRACKER` comes from `execution_config.py` (SKILL.md Step 0). Save each issue as
`issues/<KEY>.json` in the shape `check_eligibility.py` and `traverse_links.py` read. Keep the raw
YouTrack and GitHub files under `issues/raw/`, never in the glob the traversal reads, or a key
lands twice and the traversal refuses.

| Tracker | Fetch | Save as `issues/<KEY>.json` |
|:--|:--|:--|
| `jira` | `getJiraIssue` (fields below) | the raw response |
| `youtrack` | `"$TRACKER_CLI" issue KEY > issues/raw/KEY.json` and `"$TRACKER_CLI" links KEY --json > issues/raw/KEY.links.json` | `normalize_issue.py --tracker youtrack --issue … --links … [--state-field <field>]` |
| `github` | `gh issue view N --json number,title,body,state,stateReason,labels,createdAt` | `normalize_issue.py --tracker github --issue …` |
| `local` | none | no ticket: the input is a plan (the unscoped fallback) |

`--state-field` is the YouTrack board field when the instance's config names one (`Stage` by
default, Acme's). YouTrack links are read from the live link phrases: `depends on` is a
predecessor, `is required for` a successor, `subtask of` the parent, and `parent for` a child,
which the traversal ignores. A GitHub issue carries no QA state and closes when its PR merges, so
its closed state never counts as accepted: the merged PR is the delivery evidence.

## Target repo resolution

The change landed in exactly one repository per lane, and the run must execute from that
checkout. Resolve, in order, and stop at the first hit:

| Source | Signal | Example |
|:-------|:-------|:--------|
| PR | the PR's own repository | `github.com/somnio-projects/marketplace-resident-app/pull/230` |
| Ticket remote links / comments | a PR URL | "merged via …/marketplace-resident-app/pull/230" |
| Ticket body | a `Repo:` line | `**Repo:** marketplace-resident-app (Flutter)` |
| Summary prefix | `[BE]` / `[FE]` / `[AI]` / `[QA]` per the Marketplace convention | `[FE] Thread rows are keyed by index` → resident app |
| Labels | stack labels | `flutter` → resident app; `nestjs`/`api` → monorepo |
| Current checkout | `.loop/config.yaml` `pr_target`, where that file exists | tie-breaker only |

Compare with `git remote get-url origin` of the current checkout. Mismatch → stop with the
repo name and ask the person to run from that checkout — every path the run touches is relative
to the working directory, so this is a person's action, not a flag the skill can take. Two repos →
one lane per repo, each with its own `RUN_DIR` under that repo's `docs/uat/`; the report and the
drafts link both. The Marketplace mapping (`[BE]` monorepo · `[FE]` Flutter resident app ·
`[AI]` AI-services repo) is the current convention; other projects state theirs in the target
repo's `CLAUDE.md`, and an unmapped prefix is an ambiguity stop.

## Eligibility states (exhaustive)

Evaluated by `scripts/check_eligibility.py`. When a PR is supplied it is the delivery
evidence and it wins: a ticket in Ready-for-QA next to an open, draft, closed-unmerged, or
wrong-base PR is **not** delivered. Ticket status alone is sufficient only when no PR was
supplied. A MERGED PR whose base branch is unknown fails closed.

| Ticket status (normalized) | PR state | Verdict | Reason shown |
|:---------------------------|:---------|:--------|:-------------|
| Ready for QA / QA / In QA / Ready for Testing / Testing / Ready for UAT / UAT / PR Merged | none supplied, or MERGED into the integration branch | ELIGIBLE | — |
| Ready-for-QA equivalent | OPEN / draft / CLOSED-unmerged / MERGED elsewhere | NOT_ELIGIBLE | the PR reason below wins |
| any | MERGED into the integration branch | ELIGIBLE | — |
| any | MERGED, base branch unknown (missing `baseRefName`) | NOT_ELIGIBLE | merged into `<unknown base>` — fail closed |
| To Do / Backlog / Open / Selected for Development | not merged | NOT_ELIGIBLE | not started |
| In Progress / In Development | not merged | NOT_ELIGIBLE | still being built |
| In Review / Code Review / In PR | open PR | NOT_ELIGIBLE | not merged yet |
| Blocked / On Hold | any | NOT_ELIGIBLE | blocked upstream |
| Done / Closed / Released | any, `--rerun` absent | NOT_ELIGIBLE | already accepted; pass `--rerun` |
| Done / Closed / Released | any, `--rerun` present | ELIGIBLE (rerun) | — |
| any | OPEN (not draft) | NOT_ELIGIBLE | PR open |
| any | OPEN draft | NOT_ELIGIBLE | PR is a draft |
| any | MERGED into a non-default branch | NOT_ELIGIBLE | merged into `<branch>`, not the integration branch |
| any | CLOSED unmerged | NOT_ELIGIBLE | PR closed without merge |
| unknown status | unknown | NOT_ELIGIBLE | unrecognized status `<raw>` — extend the table by PR |

`PR Merged` is Acme's YouTrack stage for code on develop that QA has not taken yet.
`Staging (TF)` is not in the table: QA sets it, and what it means for a re-run is the team's call.

The integration branch is the repo's default branch (`gh repo view --json defaultBranchRef`), or
`.loop/config.yaml` `pr_target.base_branch` where that file exists.

## Epic fan-out

Enumerate with the tracker's children operation:
- Jira: `searchJiraIssuesUsingJql` with `parent = <EPIC> OR "Epic Link" = <EPIC>`, fields
  `summary,status,issuetype,parent,issuelinks,labels,fixVersions`. Reconcile the count against
  the epic's own child list; on mismatch, stop and report both counts (same rule as
  jira-ticket-hardening).
- YouTrack: the `parent for` lines of `"$TRACKER_CLI" links <EPIC>`, then one fetch per child.
- GitHub: `gh api repos/{o}/{r}/issues/<n>/sub_issues`.

Run the eligibility check per child. The run report carries two tables: **In scope** (eligible
children, each with its PR) and **Excluded** (key · status · reason). Excluded children are
never silently dropped.

## The unscoped fallback

Triggered by `INPUT_KIND=plan` or by a PR with no resolvable ticket. Say, in one block:

> This run is **UNSCOPED**. It has no ticket or PR, so it cannot: verify acceptance criteria it
> was not given; detect deviations between a spec and what shipped; post findings back anywhere.
> Bind it to a ticket or PR now, or continue unscoped with the plan as the only criteria source.

Ask with `AskUserQuestion` (options: bind to ticket · bind to PR · continue unscoped). Only an
explicit "continue unscoped" proceeds; the report header and `run.json` carry `UNSCOPED`.

## Context expansion traversal

Start from every key in `TICKETS`. Fetch each issue once (Jira fields
`summary,description,status,issuetype,parent,issuelinks,labels,components,fixVersions,created`;
other trackers per § Fetching per tracker) into `issues/<KEY>.json`, then fetch what the
traversal below asks for, then run `scripts/traverse_links.py` over the whole set so ranking is deterministic and reviewable.

1. **Up.** `parent` (Jira `parent` or `Epic Link`, YouTrack `subtask of`). Read the epic's
   description for intent, scope and its own acceptance criteria. If the parent is itself a child (sub-task), go up twice.
2. **Sideways and back.** For every `issuelinks` entry, classify by link type:
   - `is blocked by` / `depends on` / `caused by` / `is caused by` → **predecessor** (weight 3)
   - `blocks` / `causes` / `is required for` → **successor** (weight 1, context only)
   - `relates to` → **related** (weight 2)
   - `duplicates` / `is duplicated by` / `clones` → **twin** (weight 2; its spec may be the better one)
3. **Siblings.** Children of the same epic whose summary shares a component, label, or a noun
   phrase with the ticket (weight 1; weight 2 when `Done` and older — likely the feature ship).
4. **Bug rule.** When the ticket is a Bug: the highest-ranked `Done` Story among predecessors,
   twins and siblings whose scope contains the bug's surface is the **feature ticket**. Its
   spec (`docs/specs/` by key) is the default reproduction path. If no such ticket exists, the
   reproduction path is ambiguous → stop condition.
5. **Code path.** `gh pr diff --name-only` → the modules touched → `docs/specs/**` files mentioning
   the ticket key or those modules → the flow description (§1 Goal, §5 Architecture, §7 API
   Contract, §8 Success criteria of the 20-section spec).
6. **Prior UAT.** See the next section.

Fetch depth: one hop for links, plus the parent and its children. Do not crawl further without a
reason stated in the frame.

## Prior-UAT selection

`scripts/match_prior_uat.py` scans `docs/uat/**/*.md` (the run records) and scores each:

| Signal | Points |
|:-------|:-------|
| Record names one of the run's ticket keys | 5 |
| Record names the parent epic key | 3 |
| Record names a predecessor/twin key (the ticket that shipped the behavior under test) | 5 |
| Record mentions a changed path's module (directory or file stem) | 2 per module, max 6 |
| Record contains a Context Frame term (feature noun phrase) | 1 per term, max 4 |

**Re-run threshold: 5.** Anything at or above it is queued as `RERUN` cases: its case table
rows (the `| id | … | verdict | evidence |` tables) become criteria with `kind=RERUN` and
`source=<record path>`. Records below the threshold are listed as "considered, not re-run" with
their score so the choice is auditable. Re-running is the default whenever the change touches
a flow a prior UAT covered; the person may drop a queued record with a stated reason.

## The Context Frame (output shape)

```markdown
## Context Frame
- **Use case under test:** <one sentence, user's words>  — source: <ticket|spec path>
- **Parent epic:** <KEY> <summary> — intent: <one line>  — source: <tracker>
- **Predecessors (define the behavior):**
  - <KEY> <summary> (<link type>) — spec: docs/specs/<...>  — why it matters: <one line>
- **Feature ticket (bugs only):** <KEY> — spec: <path>
- **Reproduction path:** <surface> →
  1. <step>  2. <step>  3. <assert>          — source: <spec §n | ticket AC | prior UAT case id>
- **Expected behavior ("correct" looks like):** <observable statement>  — source: <…>
- **Prior UAT runs re-executed:** <record path> (score n) · …
- **Considered, not re-run:** <record path> (score n) · …
```

Every bullet has a source. A bullet without one is a guess and is not allowed.

## Ambiguity stop conditions

Stop and ask (with the exact gap named) when any of these holds after expansion:

- The reproduction path has a step the frame cannot name (which screen, which endpoint, which
  user role).
- "Correct" is stated only as a negative ("no longer fails") without the positive observable.
- Two sources disagree on the expected behavior and the PR body does not settle it.
- The bug rule found no feature ticket and no spec covers the surface.
- The environment does not contain the PR's merge commit (that is BLOCKED, not ambiguous — but it
  also stops the run).
