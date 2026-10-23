#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Rank the parent, predecessors, twins and siblings of a ticket from fetched issue JSON.

Standard library only. This script performs no network calls: /uat fetches
each issue with the recorded tracker's tool into one JSON file per issue
(raw Jira getJiraIssue shape, or the flat {key, summary, status, issuetype,
parent, issuelinks, labels, components, created} object normalize_issue.py
writes for YouTrack and GitHub) and hands the set here, so the traversal is deterministic and reviewable rather than improvised
in the transcript. The weights are the ones documented in
references/input-resolution-and-context-expansion.md.

Usage:
    uv run traverse_links.py --root MH-2185 --issues issues/*.json

stdout: JSON {root, parent, feature_ticket, predecessors[], twins[], related[],
              successors[], siblings[], missing[]}
stderr: a short ranked summary, plus the keys referenced but not fetched
exit:   0 complete frame · 1 ambiguous (root missing, unfetched links, bug with no
        feature ticket, or an isolated ticket) — the orchestrator stops and asks ·
        2 usage / duplicate issue files
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

# Link-type classification. Link names vary per tracker and instance (YouTrack's Depend
# type reads "depends on" / "is required for"); matching is on
# lowercase substrings of the *directional* name (inward/outward) so
# "is blocked by" and "blocks" land on different sides.
PREDECESSOR = ("is blocked by", "depends on", "is caused by", "caused by", "is a prerequisite", "follows")
SUCCESSOR = ("blocks", "causes", "is depended on by", "is required for", "precedes")
TWIN = ("duplicates", "is duplicated by", "clones", "is cloned by")
RELATED = ("relates to", "relates", "is related to")

WEIGHT = {"predecessor": 3, "twin": 2, "related": 2, "sibling": 1, "successor": 1}
# A Done sibling that predates the root is the likely feature ship: bump it.
DONE_OLDER_SIBLING_BONUS = 1
ACCEPTED = ("done", "closed", "released", "resolved")
STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with", "from", "by", "as",
    "is", "be", "at", "it", "this", "that", "fix", "bug", "add", "update", "remove", "new",
}


def flatten(raw: dict) -> dict:
    """Normalize the raw Jira shape (fields nested) into a flat record."""
    fields = raw.get("fields", raw)
    parent = fields.get("parent")
    parent_key = parent.get("key") if isinstance(parent, dict) else parent
    if not parent_key:
        parent_key = fields.get("customfield_10014") or fields.get("epicLink") or fields.get("epic_link")
    status = fields.get("status")
    issuetype = fields.get("issuetype")
    links = []
    for link in fields.get("issuelinks", []) or []:
        link_type = link.get("type", {}) if isinstance(link.get("type"), dict) else {}
        if "outwardIssue" in link:
            links.append({"key": link["outwardIssue"].get("key"),
                          "name": (link_type.get("outward") or link_type.get("name") or link.get("name") or "")})
        elif "inwardIssue" in link:
            links.append({"key": link["inwardIssue"].get("key"),
                          "name": (link_type.get("inward") or link_type.get("name") or link.get("name") or "")})
        elif "key" in link:  # already flattened
            links.append({"key": link["key"], "name": link.get("name", "")})
    return {
        "key": raw.get("key") or fields.get("key"),
        "summary": fields.get("summary", ""),
        "status": status.get("name") if isinstance(status, dict) else (status or ""),
        "issuetype": issuetype.get("name") if isinstance(issuetype, dict) else (issuetype or ""),
        "parent": parent_key,
        "links": [l for l in links if l["key"]],
        "labels": list(fields.get("labels", []) or []),
        "components": [c.get("name") if isinstance(c, dict) else c for c in fields.get("components", []) or []],
        "created": fields.get("created", ""),
    }


def classify(name: str) -> str | None:
    n = " ".join(name.lower().split())
    for group, label in ((PREDECESSOR, "predecessor"), (TWIN, "twin"), (SUCCESSOR, "successor"), (RELATED, "related")):
        if any(n == t or n.startswith(t) for t in group):
            return label
    return None


def terms(summary: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", summary.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="the ticket under test (e.g. MH-2185)")
    parser.add_argument("--issues", nargs="+", required=True, help="issue JSON files (globs accepted)")
    args = parser.parse_args()

    paths: list[Path] = []
    for pattern in args.issues:
        paths.extend(Path(p) for p in glob.glob(pattern))
    issues: dict[str, dict] = {}
    for path in paths:
        try:
            rec = flatten(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
            return 2
        if rec["key"]:
            if rec["key"] in issues:
                # Two files for one key means a stale or foreign fetch could silently replace
                # the authoritative one; refuse rather than pick.
                print(f"error: duplicate issue {rec['key']} (second copy: {path}) — remove the stale file", file=sys.stderr)
                return 2
            issues[rec["key"]] = rec

    root = issues.get(args.root)
    if root is None:
        print(f"error: root {args.root} not among the {len(issues)} fetched issues", file=sys.stderr)
        return 1

    buckets: dict[str, list[dict]] = {k: [] for k in ("predecessors", "twins", "related", "successors", "siblings")}
    missing: set[str] = set()
    seen: set[str] = {args.root}

    def add(bucket: str, key: str, why: str, weight: int) -> None:
        if key in seen:
            return
        seen.add(key)
        rec = issues.get(key)
        if rec is None:
            missing.add(key)
            buckets[bucket].append({"key": key, "why": why, "weight": weight, "fetched": False})
            return
        buckets[bucket].append({"key": key, "summary": rec["summary"], "status": rec["status"],
                                "issuetype": rec["issuetype"], "why": why, "weight": weight, "fetched": True})

    for link in root["links"]:
        kind = classify(link["name"])
        if kind is None:
            continue
        add({"predecessor": "predecessors", "twin": "twins", "related": "related", "successor": "successors"}[kind],
            link["key"], f"link '{link['name']}'", WEIGHT[kind])

    parent = issues.get(root["parent"]) if root["parent"] else None
    if root["parent"] and parent is None:
        missing.add(root["parent"])

    root_terms = terms(root["summary"])
    root_created = root["created"]
    for key, rec in issues.items():
        if key == args.root or rec["parent"] != root["parent"] or not root["parent"]:
            continue
        shared_terms = root_terms & terms(rec["summary"])
        shared_meta = (set(rec["labels"]) & set(root["labels"])) | (set(rec["components"]) & set(root["components"]))
        if not shared_terms and not shared_meta:
            continue
        weight = WEIGHT["sibling"]
        if rec["status"].lower() in ACCEPTED and rec["created"] and root_created and rec["created"] < root_created:
            weight += DONE_OLDER_SIBLING_BONUS
        why = "sibling: " + ", ".join(sorted(shared_terms | {str(m) for m in shared_meta}))
        add("siblings", key, why, weight)

    for bucket in buckets.values():
        bucket.sort(key=lambda r: (-r["weight"], r["key"]))

    # Bug rule: the best Done story among predecessors/twins/siblings is the feature ticket.
    feature = None
    if root["issuetype"].lower() == "bug":
        candidates = [r for b in ("predecessors", "twins", "siblings") for r in buckets[b]
                      if r.get("fetched") and r.get("status", "").lower() in ACCEPTED
                      and r.get("issuetype", "").lower() in ("story", "task", "feature")]
        candidates.sort(key=lambda r: (-r["weight"], r["key"]))
        feature = candidates[0]["key"] if candidates else None

    out = {
        "root": {"key": root["key"], "summary": root["summary"], "status": root["status"], "issuetype": root["issuetype"]},
        "parent": ({"key": parent["key"], "summary": parent["summary"], "status": parent["status"]} if parent
                   else ({"key": root["parent"], "fetched": False} if root["parent"] else None)),
        "feature_ticket": feature,
        **buckets,
        "missing": sorted(missing),
    }
    print(json.dumps(out, indent=2))

    linked = sum(len(b) for b in buckets.values())
    for name in ("predecessors", "twins", "related", "siblings", "successors"):
        for r in buckets[name][:5]:
            print(f"{name[:-1]:>11}  {r['key']:<10} w={r['weight']}  {r.get('summary', '(not fetched)')[:60]}", file=sys.stderr)
    # Exit code is the gate: an incomplete frame (unfetched links, a bug with no feature
    # ticket, an isolated ticket) is AMBIGUOUS and exits 1 so the orchestrator stops.
    ambiguous = False
    if missing:
        print(f"not fetched ({len(missing)}): {', '.join(sorted(missing))} — fetch and re-run for a complete frame",
              file=sys.stderr)
        ambiguous = True
    if root["issuetype"].lower() == "bug" and feature is None:
        print("bug rule: no Done feature ticket found — the reproduction path is AMBIGUOUS; stop and ask",
              file=sys.stderr)
        ambiguous = True
    if linked == 0 and parent is None:
        print("nothing linked and no parent: the ticket is isolated — treat the frame as ambiguous", file=sys.stderr)
        ambiguous = True
    return 1 if ambiguous else 0


if __name__ == "__main__":
    sys.exit(main())
