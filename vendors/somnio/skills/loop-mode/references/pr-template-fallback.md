# PR template fallback

Only use this if the repo genuinely has no `.github/pull_request_template.md`, no
`.github/PULL_REQUEST_TEMPLATE/`, and the `generate-pr` skill isn't available to detect one another
way. Check for those first — this fallback exists so a PR still gets a structured body, not so it
becomes the default path.

```markdown
## Summary
- {one bullet per meaningful change, from the ticket's spec Scope section — not a diff dump}

## Ticket
{TICKET-KEY} — {one-line scope}

## Test plan
- [ ] {each verification command actually run during implementation, from the run-report's
      "Subagents spawned" table — real commands, not a generic "tests pass"}

## Notes for reviewers
{anything from the run-report's "Gaps / blockers" or "Decisions" sections a reviewer would want
surfaced up front — e.g. an accepted default for an unresolved open decision, or a structural
placeholder standing in for missing backend work}

🤖 Generated with loop-mode
```

Fill every placeholder from the ticket's actual `run-report.md` (Step 8) — this template is a
shape, not filler text to leave in place.
