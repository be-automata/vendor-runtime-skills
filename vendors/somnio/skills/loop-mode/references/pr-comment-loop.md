# PR-comment monitoring loop

Only runs if the user opted in when asked in Step 10. This is the recurring half of loop-mode — the
part that keeps checking back instead of ending at "PR opened."

## Contents

- [Watch with `Monitor`, one watch for the whole batch](#watch-with-monitor-one-watch-for-the-whole-batch) — the shared harness watch script, armed once over the batch
- [On each `COMMENT` event](#on-each-comment-event) — classify, escalate, decide, apply
- [Stopping the watch](#stopping-the-watch) — how it winds down on its own

**Draft PRs get review comments but no bot verdict.** Automata reviews drafts and comments on
them — process those comments normally through this loop — but it emits its
approve/request-changes verdict only once the PR is Ready for review. If this loop is watching a
draft PR for an approval that gates merging, that wait never ends: surface "PR is still a draft
— no verdict will come until it is marked ready" instead of silently polling, and treat marking
it ready as the person's action (loop-mode leaves PRs paused/draft by policy). In a repo recorded
`collaborators` there is no bot; the rule still holds because a draft cannot merge.

## Watch with `Monitor`, one watch for the whole batch

`dev-execution` Step 5, `/pr-sync` and this loop use the **same mechanism**: the harness PR watch
script `skills/pr-sync/scripts/pr-watch.sh`, run under the `Monitor` tool and anchored on the
head SHA. Its contract — arming, every event line, head-SHA anchoring, the draft rule, re-arming
after a push, the silence-is-not-success rule — is written once in
`skills/pr-sync/references/pr-watch-contract.md`; read it, never copy the script here. Do not
hand-roll a `ScheduleWakeup` loop, and do not poll inside a Bash call — the Bash tool's timeout
(120 s default, 600 s ceiling) kills the wait. The difference between the three callers is scope,
not machinery: Step 5 blocks on one PR's verdict (`--exit-on-verdict`); this watch is
`persistent: true`, stays up for the life of the batch, and emits an event per *new* comment.

Arm **one** watch covering every open PR, never one per PR — N watches on the same batch multiply
notifications and rate-limit consumption for nothing:

```
Monitor({
  command: "sh <abs>/skills/pr-sync/scripts/pr-watch.sh --repo <owner/name> --prs \"101 102\" --state ${XDG_STATE_HOME:-$HOME/.local/state}/somnio-pr-watch/<owner>__<repo>-loop-<KEY> --poll 60 <WATCH_FLAGS>",
  description: "PR-comment loop over the batch",
  persistent: true,
  timeout_ms: 3600000
})
```

`<abs>` is the installed plugin's directory, read the way `/pr-sync` Step 1 reads it
(`$CLAUDE_PLUGIN_ROOT`, else the `installPath` Claude Code records in
`plugins/installed_plugins.json`), never a checkout: a PR branch could carry its own guard (absolute, as every path in this skill). `<WATCH_FLAGS>` comes from `sh <abs>/skills/pr-sync/scripts/review-mode.sh
<repo root>`: `--bot <login>`, `--bot <login> --collaborators off` or `--bot none --min-approvals N`.
Splice it verbatim — never assemble the flags by hand. On `WATCH=off` (`REVIEWER=none`)
do not arm this watch: report each PR's checks once and leave the merges to the person. The state lives outside every
worktree, so a comment dump never makes one dirty or lands in a commit. Example:
`~/.local/state/somnio-pr-watch/acme__widgets-loop-MH-2135`. On `SOURCE=unrecorded`, ask the
person first (`/pr-sync` Step 1b). Poll at **60 s or slower** — raise it for a large batch, each round
costs a handful of API calls per PR. The script reports transient API failures rather than hiding
them (`UNREACHABLE` after repeated failures, `ERROR … (<cause>)` for a local one), dedupes by
`kind:id` in `<state>/pr-{n}/seen.json` — recorded only for an item whose dump
actually landed — so a restarted watch never re-processes a comment and never loses one,
polls all three comment sources (general PR comments, inline review comments, review bodies;
the two comment feeds incrementally, each behind its own `since=` anchor — contract ›
*Fetching and state*),
reports approvals and the bot's verdict at the **current head only** (a collaborator's request for
changes stands across pushes until they re-review or it is dismissed — contract › Rules), drops PRs as they merge or close, and exits
with `DONE` when the last one goes. The orchestrator only reacts to the lines it emits — the
contract's *Events* table is the authoritative list (`COMMENT` per new comment; `DRAFT`,
`CHECKS_FAILED`, `CHANGES_REQUESTED`, `APPROVED_PENDING_CHECKS`, `APPROVED_PENDING_APPROVALS`, `READY`, `WAITING`, `MERGED`,
`CLOSED` on state change; `DONE` at wind-down). `TaskStop` it when the user says stop. On `DONE`,
offer `/worktree-recycle` for the batch's worktrees; never remove one
yourself. Example: three tickets merged → three `RECYCLE merged` rows for the person to pick.

## On each `COMMENT` event

1. **One subagent per new comment.** For each new comment, spawn a subagent (Agent tool or a
   `Workflow` `agent()` call if you fold this into a script — a plain Agent call is fine here since
   this is a light, per-comment classification, not a heavy fan-out) with:
   - The comment body (from the `dump=` JSON the event names — never the 160-char excerpt),
     author, severity tag, file/line for inline comments, and whether the PR's diff context is
     available to it.
   - Instructions to determine: **who/what is commenting** (a known bot — e.g.
     `automata-ai-bot`, CodeRabbit, Copilot review — vs. a human reviewer; check the author handle
     against known bot patterns first), **what kind of comment** it is (style nit, question,
     architectural concern, regression flag, praise/approval), whether it carries **real
     architectural value** (a genuine design-pattern concern, not a preference), and whether it
     **flags or would introduce a regression** against the code that was actually shipped in this
     ticket's diff — the subagent should read the relevant diff hunk, not just the comment text, to
     judge this.
   - A confidence score (per the calibration convention already in use throughout this skill) on
     both the architectural-value and regression-risk judgments.
2. **Escalate only what's substantive.** A `COMMENT` carrying `worklist=yes` (a
   `CHANGES_REQUESTED` review or an inline carrying `review=<id>` of one — those also say
   `blocking=yes` — or, in every mode but `automata_only`, a collaborator's plain-Comment review,
   `role=` + `verdict=COMMENTED`) is
   attempted whatever its `sev`, and is never filed as a nit; the `CHANGES_REQUESTED` and
   `COLLABORATOR_REVIEW` lines' `findings:` tails are the work list (contract › *Working a
   verdict*), and the fix still lands only through step 3's decision, or through step 4's armed
   *Auto-push* cycle when Step 1 item 5 was Yes. Under `--collaborators off`
   a collaborator's plain-Comment review carries no `worklist=yes` and prints no
   `COLLABORATOR_REVIEW` line — it is reported, not worked; their request for changes still is. A `standing:` collaborator
   in a `CHANGES_REQUESTED` line is not cleared by the push: the user re-requests that review. A comment judged as a style
   nit or low-confidence noise
   doesn't need to reach the user — note it in the ticket's run summary (Step 8, append rather than
   rewrite) and move on. Anything with real architectural value or a plausible regression risk
   (confidence ≥ 6/10 on either axis) goes to you, the orchestrator — never resolved by the
   per-comment subagent on its own, and never silently applied as a fix without a decision
   (step 4's armed cycle is that decision, taken at the start of the run, inside the guard).
3. **Decide with the user.** Present it through the same `decision-protocol.md` shape: state what
   the comment actually said, whether it's from a bot or a human (and which one), what diff it's
   about, your own researched read on whether it's right, and multiple-choice options — typically
   something like "apply the suggested fix", "reply explaining why the current approach stands
   (recommended if you determined the comment is mistaken — say why)", "flag for the ticket's own
   PR review, no code change needed". Log the outcome into that ticket's run-report.md.
4. **Apply the decision.** **Armed (Step 1 item 5 answered *Yes*):** a `worklist=yes` finding
   skips step 3's ask. Fix it in the ticket's worktree, run the real verification command, stage,
   and run the *Auto-push* cycle in `skills/pr-sync/references/pr-watch-contract.md`: guard
   `check`, commit with its `TRAILER` lines, `verify`, push, `record`, `resolve-threads.sh`, all with
   the batch's `--state`. A guard refusal
   goes back to step 3. Log each cycle in the ticket's run-report.md. Everything else, and every
   finding when unarmed, follows step 3.
   **After the user confirms in step 3:** if it results in a code change, make it inside that ticket's existing
   worktree (not a new one), re-run the real verification command, commit, and push — the same
   "should pass is not evidence" discipline as Step 6's implementation phases applies here too. If it results in a
   reply: post it via `gh pr comment` / `gh api` as a reply to the original comment thread, not a
   disconnected new top-level comment.

## Stopping the watch

Ask the user, when they opt in, what should end the monitoring — typically "until I say stop" or
"until the PRs are merged/closed." The second case is already mechanical: the script drops each PR
as it merges or closes and exits with `DONE` when the last one goes, so the watch winds down
on its own and says so rather than going silent. For "until I say stop", `TaskStop` the watch.
Re-arm a fresh watch after a push if you need the new head reflected immediately — the running
script picks it up on its next round anyway, since it re-reads `headRefOid` every time. Never
leave two watches on the same batch.
