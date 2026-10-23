# Run summary template

Write this to `{worktreePath}/docs/specs/{ticket}/run-report.md` when a ticket reaches a terminal
state (`done` or `blocked-in-phase`/`blocked-at-*`) in the Workflow's return value. This is the
artifact loop-mode leaves behind per finished goal — fill every section from real data the
Workflow run actually returned, never from memory or assumption.

```markdown
# Run report — {TICKET-KEY}

**Status:** done | blocked-at-setup | blocked-at-spec | blocked-at-validate | blocked-in-phase
**Worktree:** {worktreePath}
**Branch:** {branch} (base: {baseRef})
**Models used:** spec={specModel} · validate={validateModel} · implement={implementModel}
**FE/UI ticket:** yes/no — design reference: {designRef.type} {designRef.value or "none"}

## Orchestrator decisions

One row per user decision this ticket triggered (Step 7 decision protocol, whether raised from
Step 2's ticket intelligence, Step 6's pipeline, or Step 10's PR-comment loop). Empty table if none.

| Stage | Question asked | Orchestrator recommendation | User's choice | Rationale (user's words if given) |
|---|---|---|---|---|

## Subagents spawned

One row per `agent()` call the Workflow made for this ticket.

| Label | Phase | Model | Verdict/summary |
|---|---|---|---|
| setup:{key} | Setup | — | ok / blocked — {notes} |
| spec:{key} | Spec | {specModel} | written / blocked |
| validate:{key} | Validate | {validateModel} | passed / issues: {n} |
| implement:{key}:{phase} | Implement | {implementModel} | done / blocked — {reason} |
| verify:{key}:{phase} | Implement | — | exit {code} |

## Gaps / blockers found

Every issue the validation gate or an implementing agent surfaced, with its confidence score —
copy these verbatim from the Workflow's `blocked[]` entries and the validation agent's `issues[]`,
don't paraphrase away the confidence number.

- [confidence N/10] {issue text} — {resolution: fixed / accepted-as-is / deferred, and how}

## PR status

- Pushed: yes/no · PR: {url or "not opened — push/PR still paused"}
- Template source: generate-pr skill / {repo}/.github/pull_request_template.md / fallback template

## Notes

Anything a future reader needs that doesn't fit the sections above — e.g. an OD (open decision)
from the ticket's own tracker description that was still unresolved and required using its documented
default.
```
