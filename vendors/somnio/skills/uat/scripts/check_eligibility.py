#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Eligibility gate for /uat: is this ticket/PR pair ready for a remote UAT run?

Standard library only. Reads the issue JSON (raw Jira getJiraIssue output, or
the flat record normalize_issue.py writes for YouTrack and GitHub) and
optionally the PR JSON (as returned by
`gh pr view --json state,isDraft,baseRefName,mergedAt,number,url`), and
decides deterministically. The full state table lives in
references/input-resolution-and-context-expansion.md; this script is that
table, executable.

Usage:
    uv run check_eligibility.py --issue-json issue.json [--pr-json pr.json]
        [--integration-branch develop] [--rerun]
    uv run check_eligibility.py --pr-json pr.json            # PR-only input

stdout: one JSON object {verdict, reason, ticket_status, pr_state, eligible_by}
stderr: the human-readable reason
exit:   0 ELIGIBLE · 1 NOT_ELIGIBLE · 2 usage / unreadable input
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Status names are normalized to lowercase with single spaces before lookup.
# Extend these tuples by PR when a project uses a new workflow name; an unknown
# status is NOT_ELIGIBLE on purpose (a gate that guesses is not a gate).
# "pr merged" is Acme's YouTrack stage for code on develop that QA has not taken yet.
READY_FOR_QA = (
    "ready for qa", "qa", "in qa", "ready for testing", "testing", "ready for uat", "uat",
    "ready for test", "pr merged",
)
NOT_STARTED = ("to do", "todo", "backlog", "open", "selected for development", "new")
IN_PROGRESS = ("in progress", "in development", "development")
IN_REVIEW = ("in review", "code review", "in pr", "review", "pr review")
BLOCKED = ("blocked", "on hold")
ACCEPTED = ("done", "closed", "released", "resolved")


def _norm(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _load(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"error: {path} must hold a JSON object", file=sys.stderr)
        sys.exit(2)
    return data


def ticket_status(issue: dict | None) -> str | None:
    """Pull the status name from either the raw Jira shape or a flattened one."""
    if issue is None:
        return None
    fields = issue.get("fields", issue)
    status = fields.get("status")
    if isinstance(status, dict):
        return status.get("name")
    return status if isinstance(status, str) else None


def pr_state(pr: dict | None) -> tuple[str | None, bool, str | None]:
    """Return (state, is_draft, base_branch). Accepts `gh pr view --json` output."""
    if pr is None:
        return None, False, None
    state = pr.get("state")
    if state is None and pr.get("mergedAt"):
        state = "MERGED"
    return (str(state).upper() if state else None), bool(pr.get("isDraft")), pr.get("baseRefName")


def decide(issue: dict | None, pr: dict | None, integration: str, rerun: bool) -> dict:
    status_raw = ticket_status(issue)
    status = _norm(status_raw)
    state, draft, base = pr_state(pr)

    # Fail closed on an unknown base: a MERGED PR whose baseRefName is missing may have
    # landed anywhere (a release branch, a feature branch) — that is not "delivered".
    merged_into_integration = state == "MERGED" and base == integration

    # When a PR is supplied it is the delivery evidence and it wins: a ticket moved to
    # Ready-for-QA next to an open, draft, closed-unmerged, or wrong-base PR is not
    # delivered, whatever the workflow column says. Ticket status alone is only
    # sufficient when no PR was supplied.
    if state is not None and not merged_into_integration:
        if state == "MERGED":
            return _verdict("NOT_ELIGIBLE",
                            f"PR merged into '{base or '<unknown base>'}', not the integration branch '{integration}'",
                            status_raw, state, None)
        if state == "OPEN" and draft:
            return _verdict("NOT_ELIGIBLE", "PR is still a draft", status_raw, state, None)
        if state == "OPEN":
            return _verdict("NOT_ELIGIBLE", "PR is open, not merged yet", status_raw, state, None)
        if state == "CLOSED":
            return _verdict("NOT_ELIGIBLE", "PR was closed without merging", status_raw, state, None)
        return _verdict("NOT_ELIGIBLE", f"unrecognized PR state '{state}'", status_raw, state, None)

    # ACCEPTED is checked BEFORE the merged-PR branch, and the order is load-bearing:
    # an accepted ticket practically always HAS a merged PR, so grading the PR first
    # made `--rerun` unreachable and let /uat silently re-run a full remote UAT against
    # an already-signed-off ticket. Re-running is fine; doing it without being asked is not.
    if status in ACCEPTED:
        if rerun:
            return _verdict("ELIGIBLE", f"ticket is '{status_raw}' and --rerun was passed",
                            status_raw, state, "rerun")
        return _verdict("NOT_ELIGIBLE",
                        f"ticket is already accepted ('{status_raw}'); pass --rerun to re-execute",
                        status_raw, state, None)
    if status in READY_FOR_QA:
        by = "ticket_status+pr_merged" if merged_into_integration else "ticket_status"
        return _verdict("ELIGIBLE", f"ticket status '{status_raw}' is a Ready-for-QA equivalent"
                        + (f" and PR is merged into '{base}'" if merged_into_integration else " (no PR supplied)"),
                        status_raw, state, by)
    if merged_into_integration:
        return _verdict("ELIGIBLE", f"PR is merged into '{base}'", status_raw, state, "pr_merged")
    # Ticket-driven refusals. Reaching here means no PR was supplied, or the PR is
    # merged somewhere other than the integration branch.
    if status in BLOCKED:
        return _verdict("NOT_ELIGIBLE", f"ticket is '{status_raw}' (blocked upstream)", status_raw, state, None)
    if status in IN_REVIEW:
        return _verdict("NOT_ELIGIBLE", f"ticket is '{status_raw}' — not merged yet", status_raw, state, None)
    if status in IN_PROGRESS:
        return _verdict("NOT_ELIGIBLE", f"ticket is '{status_raw}' — still being built", status_raw, state, None)
    if status in NOT_STARTED:
        return _verdict("NOT_ELIGIBLE", f"ticket is '{status_raw}' — not started", status_raw, state, None)
    if not status and not state:
        return _verdict("NOT_ELIGIBLE", "no ticket status and no PR state to grade", status_raw, state, None)
    return _verdict("NOT_ELIGIBLE", f"unrecognized ticket status '{status_raw}' — extend the status table by PR",
                    status_raw, state, None)


def _verdict(verdict: str, reason: str, status: str | None, state: str | None, by: str | None) -> dict:
    return {"verdict": verdict, "reason": reason, "ticket_status": status, "pr_state": state, "eligible_by": by}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--issue-json", help="issue JSON file (Jira getJiraIssue output, or normalize_issue.py output)")
    parser.add_argument("--pr-json", help="PR JSON file (gh pr view --json state,isDraft,baseRefName,mergedAt)")
    parser.add_argument("--integration-branch", default="develop",
                        help="branch a PR must be merged into (default: develop)")
    parser.add_argument("--rerun", action="store_true", help="allow an already-accepted ticket to be re-run")
    args = parser.parse_args()

    if not args.issue_json and not args.pr_json:
        parser.error("at least one of --issue-json or --pr-json is required")

    result = decide(_load(args.issue_json), _load(args.pr_json), args.integration_branch, args.rerun)
    print(json.dumps(result))
    print(f"{result['verdict']}: {result['reason']}", file=sys.stderr)
    return 0 if result["verdict"] == "ELIGIBLE" else 1


if __name__ == "__main__":
    sys.exit(main())
