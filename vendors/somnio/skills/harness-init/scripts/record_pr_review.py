#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The PR reviewer record for /harness-init and /pr-sync (ENG-REV-01).

WHY THIS EXISTS. Every PR flow in the harness (pr-sync, dev-execution Step 5,
loop-mode Step 10) waited for Automata's verdict, because business rule 5 says
Automata is the first reviewer of every PR. A repository where Automata is not
set up as a reviewer waited for a verdict that could never come: the watch sat
at APPROVED_WITHOUT_BOT with every teammate's approval in hand. Whether Automata
reviews a repository is a fact about how that repository is configured on GitHub,
so it is asked and recorded -- never assumed and never inferred silently.

The record lives in the target's committed `.claude/harness.json` as
`pr_review`, in the D-13 shape: "collaborators" is an ordinary answer, and
absence means nobody was asked. pr-sync's `scripts/review-mode.sh` reads it to
arm the watch with `--bot <login>`, `--bot <login> --collaborators off` ("automata_only":
the bot's verdict, collaborators' reviews reported but not worked) or
`--bot none --min-approvals N`, or to skip the watch when the answer is "none" (PRs merge
on green CI; nothing waits for a review).

Usage:
    uv run record_pr_review.py <root> --check [--json]
    uv run record_pr_review.py <root> --record automata|automata_only|collaborators|none
        --who <name> [--bot <login>] [--min-approvals N] [--evidence <text>]

--check is read-only. --record writes the `pr_review` block and nothing else, and
refuses before any write when `.claude/harness.json` is absent or unreadable: the
schema requires `rules_source` and `installed[]` that only the install step can
answer, so this script never invents a manifest.

exit: 0 recorded (and well-formed) / written · 1 --check: nothing on record ·
      2 refused (bad arguments, no manifest) · 3 --check: the record is malformed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    PR_AUTOMATA_MODES,
    PR_REVIEW_DEFAULT_BOT,
    PR_REVIEWERS,
    _LOGIN_RE,
    classify_pr_review,
)


def _load(man: Path) -> tuple[dict | None, str | None]:
    if not man.is_file():
        return None, (".claude/harness.json is absent, so the PR reviewer record has nowhere "
                      "to live. Run harness-init's install step first.")
    try:
        m = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f".claude/harness.json is unreadable ({e})"
    if not isinstance(m, dict):
        return None, ".claude/harness.json is not a JSON object"
    return m, None


def describe(block: dict) -> str:
    if block["reviewer"] == "none":
        return f"no review watch, PRs merge on green CI, recorded by {block['who']}"
    if block["reviewer"] == "automata":
        return (f"automata ({block.get('bot_login', PR_REVIEW_DEFAULT_BOT)}) + collaborators, "
                f"recorded by {block['who']}")
    if block["reviewer"] == "automata_only":
        return (f"automata ({block.get('bot_login', PR_REVIEW_DEFAULT_BOT)}) only, "
                f"collaborators' reviews reported but not worked, "
                f"recorded by {block['who']}")
    return (f"collaborators only, {block.get('min_approvals', 1)} approval(s) required, "
            f"recorded by {block['who']}")


def check(root: Path, as_json: bool) -> int:
    m, err = _load(root / ".claude" / "harness.json")
    if m is None:
        state, problem = "absent", err
    else:
        state, problem = classify_pr_review(m.get("pr_review"))
    if as_json:
        print(json.dumps({"state": state, "problem": problem,
                          "record": (m or {}).get("pr_review")}))
    elif state == "absent":
        print(f"pr-review  absent  {problem}")
    elif problem:
        print(f"pr-review  invalid  {problem}")
    else:
        print(f"pr-review  {state}  {describe(m['pr_review'])}")
    if state == "absent":
        return 1
    return 3 if problem else 0


def record(root: Path, a: argparse.Namespace) -> int:
    who = (a.who or "").strip()
    if not who:
        print("error: --record needs --who: the record names the person who answered",
              file=sys.stderr)
        return 2
    if a.record == "none" and (a.bot or a.min_approvals != 1):
        print("error: a none record takes no --bot and no --min-approvals: nothing waits "
              "for a review", file=sys.stderr)
        return 2
    if a.record == "collaborators" and a.bot:
        print("error: --bot names a review bot, and a collaborators record says there is none",
              file=sys.stderr)
        return 2
    if a.bot and not _LOGIN_RE.match(a.bot):
        print(f"error: --bot {a.bot!r} is not a GitHub login", file=sys.stderr)
        return 2
    if not 1 <= a.min_approvals <= 99:
        print("error: --min-approvals must be an integer from 1 to 99", file=sys.stderr)
        return 2
    man = root / ".claude" / "harness.json"
    m, err = _load(man)
    if m is None:
        print(f"RECORD NOT WRITTEN: {err}", file=sys.stderr)
        return 2
    block: dict = {"reviewer": a.record}
    if a.record in PR_AUTOMATA_MODES:
        block["bot_login"] = a.bot or PR_REVIEW_DEFAULT_BOT
    if a.record != "none":
        block["min_approvals"] = a.min_approvals
    block["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block["who"] = who
    if a.evidence and a.evidence.strip():
        block["evidence"] = " ".join(a.evidence.split())
    state, problem = classify_pr_review(block)
    if problem:  # every input was validated above; this is the classifier's second look
        print(f"RECORD NOT WRITTEN: {problem}", file=sys.stderr)
        return 2
    prior = m.get("pr_review")
    m["pr_review"] = block
    man.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    was = ""
    if isinstance(prior, dict) and prior:
        pstate, pproblem = classify_pr_review(prior)
        was = f" (was: {describe(prior) if not pproblem else pstate + ', malformed'})"
    print(f"recorded the PR reviewer in .claude/harness.json: {describe(block)}{was}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="target repository root")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read the record; write nothing")
    mode.add_argument("--record", choices=PR_REVIEWERS,
                      help="write the answer: automata (+ collaborators), automata_only "
                           "(the bot's verdict; collaborators' reviews reported, not "
                           "worked), collaborators only, or none (no review watch; merge "
                           "on green CI)")
    ap.add_argument("--who", help="the person who answered")
    ap.add_argument("--bot", help=f"the review bot login (default {PR_REVIEW_DEFAULT_BOT})")
    ap.add_argument("--min-approvals", type=int, default=1,
                    help="collaborator approvals a PR needs; the verdict in collaborators "
                         "mode, informational on an automata/automata_only record (default 1)")
    ap.add_argument("--evidence", help="the detector summary the person answered against")
    ap.add_argument("--json", action="store_true", help="--check: one JSON object on stdout")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    return check(root, a.json) if a.check else record(root, a)


if __name__ == "__main__":
    sys.exit(main())
