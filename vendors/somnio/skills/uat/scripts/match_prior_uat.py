#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Find prior UAT records in docs/uat/ that the current change could regress, and queue them.

Standard library only. Scans flat records (docs/uat/UAT-*.md, the Marketplace
shape) and folder records (docs/uat/UAT-*/README.md, the /uat shape), scores
each against the run's ticket keys, changed paths and feature terms, and
extracts the case rows of every record at or above the re-run threshold so
they become RERUN criteria. Scoring is the table in
references/input-resolution-and-context-expansion.md; the threshold is
documented there too.

Usage:
    uv run match_prior_uat.py --uat-dir docs/uat --keys MH-2185,MH-2135 \
        [--predecessors MH-2068] [--paths apps/web/app/dashboard/x.tsx,apps/api/src/y.ts] \
        [--terms "lease renewal,notifications"] [--threshold 5]

stdout: JSON {threshold, rerun: [{path, score, signals, cases[], cases_parsed}], considered: [{path, score}], unreadable: []}
stderr: the ranked list; a queued record whose case table was not recognized is flagged
        so the orchestrator reads it by hand instead of re-running an empty set
exit:   0 · 1 no UAT records found, or a record could not be read · 2 usage
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Points per signal. A direct ticket-key hit alone reaches the threshold, which
# is the intent: a record written for this ticket is always re-run. A
# predecessor/twin key counts the same: predecessors are by definition the
# tickets that shipped the behavior under test, so their UAT is the regression
# baseline (a bug's feature ticket is the canonical case).
POINTS_TICKET_KEY = 5
POINTS_EPIC_KEY = 3
POINTS_PREDECESSOR_KEY = 5
POINTS_PER_MODULE = 2
MAX_MODULE_POINTS = 6
POINTS_PER_TERM = 1
MAX_TERM_POINTS = 4
DEFAULT_THRESHOLD = 5

# "observed" is the Marketplace records' column for browser/step tables (Step | Observed | Evidence).
VERDICT_HEADERS = ("result", "verdict", "status", "outcome", "observed")
EVIDENCE_HEADERS = ("evidence", "artifact", "proof")
GENERIC_MODULES = {"src", "app", "apps", "lib", "index", "main", "utils", "common", "shared", "test", "tests", "spec"}


def records(uat_dir: Path) -> list[Path]:
    flat = [p for p in uat_dir.glob("UAT-*.md")] + [p for p in uat_dir.glob("uat-*.md")] + [p for p in uat_dir.glob("*-uat.md")]
    folders = [p / "README.md" for p in uat_dir.glob("UAT-*") if p.is_dir() and (p / "README.md").is_file()]
    return sorted(set(flat + folders))


def modules_from_paths(paths: list[str]) -> set[str]:
    mods: set[str] = set()
    for p in paths:
        p = p.strip()
        if not p:
            continue
        parts = [x for x in re.split(r"[/\\]", p) if x]
        if not parts:
            continue
        stem = re.sub(r"\.(spec|test|e2e-spec|module|service|controller|component|page)?\.[a-z0-9]+$", "", parts[-1], flags=re.I)
        stem = re.sub(r"\.[a-z0-9]+$", "", stem)
        for cand in (stem, *parts[-3:-1]):
            cand = cand.lower()
            if len(cand) > 2 and cand not in GENERIC_MODULES:
                mods.add(cand)
    return mods


def parse_tables(text: str) -> list[dict]:
    """Extract rows from markdown tables that carry a verdict column and an evidence column."""
    cases: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        header, sep = lines[i].strip(), lines[i + 1].strip()
        if header.startswith("|") and re.match(r"^\|?\s*:?-{2,}", sep):
            cols = [c.strip().lower() for c in header.strip("|").split("|")]
            v_idx = next((k for k, c in enumerate(cols) if any(h in c for h in VERDICT_HEADERS)), None)
            e_idx = next((k for k, c in enumerate(cols) if any(h in c for h in EVIDENCE_HEADERS)), None)
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if v_idx is not None and e_idx is not None and len(cells) >= max(v_idx, e_idx) + 1:
                    cases.append({
                        "id": cells[0],
                        "text": " · ".join(c for k, c in enumerate(cells) if k not in (0, v_idx, e_idx))[:200],
                        "prior_verdict": cells[v_idx],
                        "prior_evidence": cells[e_idx],
                    })
                j += 1
            i = j
        else:
            i += 1
    return cases


def score(text: str, keys: set[str], epic: str | None, preds: set[str], mods: set[str], terms: list[str]) -> tuple[int, dict]:
    upper = text.upper()
    lower = text.lower()
    signals: dict[str, int] = {}
    if any(re.search(rf"\b{re.escape(k)}\b", upper) for k in keys):
        signals["ticket_key"] = POINTS_TICKET_KEY
    if epic and re.search(rf"\b{re.escape(epic.upper())}\b", upper):
        signals["epic_key"] = POINTS_EPIC_KEY
    if any(re.search(rf"\b{re.escape(k)}\b", upper) for k in preds):
        signals["predecessor_key"] = POINTS_PREDECESSOR_KEY
    hit_mods = [m for m in sorted(mods) if m in lower]
    if hit_mods:
        signals["modules"] = min(MAX_MODULE_POINTS, POINTS_PER_MODULE * len(hit_mods))
        signals["modules_hit"] = hit_mods  # type: ignore[assignment]
    hit_terms = [t for t in terms if t and t.lower() in lower]
    if hit_terms:
        signals["terms"] = min(MAX_TERM_POINTS, POINTS_PER_TERM * len(hit_terms))
        signals["terms_hit"] = hit_terms  # type: ignore[assignment]
    total = sum(v for k, v in signals.items() if isinstance(v, int))
    return total, signals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uat-dir", default="docs/uat")
    parser.add_argument("--keys", required=True, help="comma-separated ticket keys under test")
    parser.add_argument("--epic", help="parent epic key")
    parser.add_argument("--predecessors", default="", help="comma-separated predecessor/twin keys")
    parser.add_argument("--paths", default="", help="comma-separated changed paths (gh pr diff --name-only)")
    parser.add_argument("--terms", default="", help="comma-separated feature terms from the Context Frame")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    uat_dir = Path(args.uat_dir)
    if not uat_dir.is_dir():
        print(f"error: {uat_dir} is not a directory", file=sys.stderr)
        return 2
    recs = records(uat_dir)
    if not recs:
        print(f"no UAT records under {uat_dir} — nothing to re-run (first UAT for this repo?)", file=sys.stderr)
        # Same key set as every other exit path: a consumer reading result["unreadable"]
        # must not KeyError on the first-UAT-in-this-repo case.
        print(json.dumps({"threshold": args.threshold, "rerun": [], "considered": [], "unreadable": []}))
        return 1

    keys = {k.strip().upper() for k in args.keys.split(",") if k.strip()}
    preds = {k.strip().upper() for k in args.predecessors.split(",") if k.strip()}
    mods = modules_from_paths(args.paths.split(","))
    terms = [t.strip() for t in args.terms.split(",") if t.strip()]

    rerun, considered, unreadable = [], [], []
    for path in recs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # An unreadable record cannot be graded: it is reported, and it fails the run
            # (exit 1) rather than vanishing from the regression baseline.
            unreadable.append({"path": str(path), "error": str(exc)})
            continue
        total, signals = score(text, keys, args.epic, preds, mods, terms)
        entry = {"path": str(path), "score": total, "signals": signals}
        if total >= args.threshold:
            entry["cases"] = parse_tables(text)
            entry["cases_parsed"] = bool(entry["cases"])
            rerun.append(entry)
        elif total > 0:
            considered.append(entry)

    rerun.sort(key=lambda e: (-e["score"], e["path"]))
    considered.sort(key=lambda e: (-e["score"], e["path"]))
    print(json.dumps({"threshold": args.threshold, "rerun": rerun, "considered": considered,
                      "unreadable": unreadable}, indent=2))

    for e in rerun:
        note = "" if e["cases_parsed"] else "  ← no case table recognized: read the record by hand and add its cases"
        print(f"RERUN      {e['score']:>2}  {e['path']}  ({len(e['cases'])} cases){note}", file=sys.stderr)
    for e in considered:
        print(f"considered {e['score']:>2}  {e['path']}", file=sys.stderr)
    for u in unreadable:
        print(f"UNREADABLE     {u['path']}: {u['error']}", file=sys.stderr)
    if not rerun:
        print("no record reached the re-run threshold; say so in the Context Frame", file=sys.stderr)
    return 1 if unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
