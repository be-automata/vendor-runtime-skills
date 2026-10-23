#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Turn a YouTrack or GitHub issue into the flat record /uat's scripts read.

Standard library only. No network: /uat fetches with the recorded tracker's
tool and hands the files here. `check_eligibility.py` and `traverse_links.py`
read raw Jira JSON directly, so Jira needs no normalizing; every other tracker
goes through this script first, so neither script learns a tracker's shape.

Usage:
    uv run normalize_issue.py --tracker youtrack --issue raw/ACME-12.json \
        --links raw/ACME-12.links.json [--state-field Stage] > issues/ACME-12.json
    uv run normalize_issue.py --tracker github --issue raw/42.json [--key '#42'] > issues/42.json

Inputs:
  youtrack  --issue: `ytctl issue KEY` · --links: `ytctl links KEY --json` (required: the
            link phrases come from the live instance, never from a table here)
  github    --issue: `gh issue view N --json number,title,body,state,stateReason,labels,createdAt`

stdout: one JSON object {key, summary, description, status, issuetype, parent,
        issuelinks[{key, name}], labels, components, created}
exit:   0 written · 2 usage / unreadable or wrong-shaped input
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
# YouTrack's Subtask phrases. "subtask of" names the parent; "parent for" names a child,
# which is not context for the ticket under test (the epic fan-out reads children itself).
PARENT_PHRASE = "subtask of"
CHILD_PHRASE = "parent for"


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def _load(path: str, want: type) -> object:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"cannot read {path}: {exc}")
    if not isinstance(data, want):
        _die(f"{path} must hold a JSON {'object' if want is dict else 'array'}")
    return data


def _cf(issue: dict, name: str) -> str | None:
    for f in issue.get("customFields") or []:
        if isinstance(f, dict) and f.get("name") == name:
            v = f.get("value")
            if isinstance(v, dict):
                return v.get("name") or v.get("login") or v.get("fullName")
            if isinstance(v, list):
                return ",".join(str(x.get("name") or x.get("login")) for x in v if isinstance(x, dict)) or None
            return str(v) if v is not None else None
    return None


def _iso(ms: object) -> str:
    if isinstance(ms, (int, float)):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ms if isinstance(ms, str) else ""


def youtrack(issue: dict, links: list, state_field: str) -> dict:
    key = issue.get("idReadable")
    if not isinstance(key, str) or not KEY_RE.match(key):
        _die(f"the issue has no valid idReadable ({key!r})")
    status = next((s for s in (_cf(issue, state_field), _cf(issue, "Stage"), _cf(issue, "State")) if s), "")
    parent, out = None, []
    for group in links:
        if not isinstance(group, dict):
            _die("each --links entry must be an object (ytctl links KEY --json)")
        lt = group.get("linkType") or {}
        verb = lt.get("targetToSource") if group.get("direction") == "INWARD" else lt.get("sourceToTarget")
        verb = " ".join(str(verb or lt.get("name") or "").lower().split())
        for other in group.get("issues") or []:
            okey = other.get("idReadable") if isinstance(other, dict) else None
            if not isinstance(okey, str) or not KEY_RE.match(okey):
                continue
            if verb == PARENT_PHRASE:
                if parent and parent != okey:
                    _die(f"{key} is a subtask of both {parent} and {okey}; resolve it in YouTrack")
                parent = okey
            elif verb != CHILD_PHRASE:
                out.append({"key": okey, "name": verb})
    return {
        "key": key,
        "summary": issue.get("summary") or "",
        "description": issue.get("description") or "",
        "status": status,
        "issuetype": _cf(issue, "Type") or "",
        "parent": parent,
        "issuelinks": out,
        "labels": [t.get("name") for t in issue.get("tags") or [] if isinstance(t, dict) and t.get("name")],
        "components": [c for c in (_cf(issue, "Subsystem") or "").split(",") if c],
        "created": _iso(issue.get("created")),
    }


def github(issue: dict, key: str | None) -> dict:
    num = issue.get("number")
    if not isinstance(num, int):
        _die("the issue has no number (gh issue view N --json number,...)")
    state = str(issue.get("state") or "").upper()
    reason = str(issue.get("stateReason") or "").upper()
    # GitHub closes an issue when its PR merges, so "closed" is not "accepted": it never
    # matches the eligibility gate's Done list. The merged PR is the delivery evidence.
    status = "open" if state == "OPEN" else (
        "closed as not planned" if reason == "NOT_PLANNED" else "closed as completed" if state == "CLOSED" else "")
    return {
        "key": key or f"#{num}",
        "summary": issue.get("title") or "",
        "description": issue.get("body") or "",
        "status": status,
        "issuetype": "",
        "parent": None,
        "issuelinks": [],
        "labels": [lb.get("name") for lb in issue.get("labels") or [] if isinstance(lb, dict) and lb.get("name")],
        "components": [],
        "created": issue.get("createdAt") or "",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tracker", required=True, choices=("youtrack", "github"))
    p.add_argument("--issue", required=True)
    p.add_argument("--links", help="youtrack: `ytctl links KEY --json` output (required)")
    p.add_argument("--state-field", default="Stage", help="youtrack board field (default: Stage)")
    p.add_argument("--key", help="github: the key to record (default: #<number>)")
    a = p.parse_args()

    issue = _load(a.issue, dict)
    if a.tracker == "youtrack":
        if not a.links:
            p.error("--links is required for youtrack")
        rec = youtrack(issue, _load(a.links, list), a.state_field)
    else:
        rec = github(issue, a.key)
    print(json.dumps(rec, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
