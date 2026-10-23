#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The execution record for /harness-init, /dev-execution and /loop-mode (ENG-EXEC-01/05).

WHY THIS EXISTS. dev-execution gated on the Atlassian Rovo connector and stopped
with zero work in a repository whose tickets live in YouTrack (acme-core,
2026-10-21), and both execution skills read their autonomy gates from a
`.loop/config.yaml` most repositories never had. Which tracker holds a
repository's tickets is a fact about how the team works, so it is asked and
recorded on a person's answer -- the execution skills run on Jira, the area's usual
tracker, while nothing is recorded -- in the same committed
`.claude/harness.json` that already records the PR reviewer. One file answers
both questions (decided 2026-10-21).

The record lives as `execution`:

    {"tracker": {"type": "youtrack", "project_key": "ACME",
                 "done_states": ["PR Merged", "Staging (TF)", "Done"]},
     "gates": {...the docs/LOOP-GATES.md vocabulary...},          optional
     "gate_commands": {"coverage": "pnpm run test:coverage"},     optional
     "date": "...", "who": "...", "evidence": "..."}

Usage:
    uv run record_execution.py <root> --check [--json]
    uv run record_execution.py <root> --record jira|youtrack|github|local --who <name>
        [--project-key KEY] [--done-state NAME ...] [--gates-json FILE]
        [--gate-command ROLE=CMD ...] [--clear-gates] [--evidence TEXT]

--record replaces the tracker and keeps a prior record's `gates` and
`gate_commands` unless new ones are passed (--clear-gates records `gates: {}`, "none", and drops the commands): a
person changing the tracker did not answer the autonomy question again. It
refuses before any write when `.claude/harness.json` is absent or unreadable,
for the reason record_pr_review.py does: this script never invents a manifest.

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
from _common import GATE_ROLES, TRACKER_TYPES, classify_execution  # noqa: E402


def _load(man: Path) -> tuple[dict | None, str | None]:
    if not man.is_file():
        return None, (".claude/harness.json is absent, so the execution record has nowhere "
                      "to live. Run harness-init's install step first.")
    try:
        m = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f".claude/harness.json is unreadable ({e})"
    if not isinstance(m, dict):
        return None, ".claude/harness.json is not a JSON object"
    return m, None


def describe(block: dict) -> str:
    t = block["tracker"]
    where = t["type"] + (f" {t['project_key']}" if t.get("project_key") else "")
    gates = "gates recorded" if block.get("gates") else "no autonomy gates (interactive)"
    return f"tracker {where}, {gates}, recorded by {block['who']}"


def check(root: Path, as_json: bool) -> int:
    m, err = _load(root / ".claude" / "harness.json")
    if m is None:
        state, problem = "absent", err
    else:
        state, problem = classify_execution(m.get("execution"))
    if as_json:
        print(json.dumps({"state": state, "problem": problem,
                          "record": (m or {}).get("execution")}))
    elif state == "absent":
        print(f"execution  absent  {problem}")
    elif problem:
        print(f"execution  invalid  {problem}")
    else:
        print(f"execution  {state}  {describe(m['execution'])}")
    if state == "absent":
        return 1
    return 3 if problem else 0


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def record(root: Path, a: argparse.Namespace) -> int:
    who = (a.who or "").strip()
    if not who:
        return _fail("--record needs --who: the record names the person who answered")
    if a.clear_gates and (a.gates_json or a.gate_command):
        return _fail("--clear-gates drops the gates; it takes no --gates-json or --gate-command")
    tracker: dict = {"type": a.record}
    if a.project_key:
        tracker["project_key"] = a.project_key
    if a.done_state:
        tracker["done_states"] = a.done_state
    gates = None
    if a.gates_json:
        try:
            gates = json.loads(Path(a.gates_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return _fail(f"--gates-json {a.gates_json} is unreadable ({e})")
    cmds: dict[str, str] = {}
    for item in a.gate_command or []:
        role, sep, cmd = item.partition("=")
        if not sep or role not in GATE_ROLES or not cmd.strip():
            return _fail(f"--gate-command {item!r} is not ROLE=CMD with ROLE one of "
                         + ", ".join(GATE_ROLES))
        cmds[role] = cmd.strip()

    man = root / ".claude" / "harness.json"
    m, err = _load(man)
    if m is None:
        print(f"RECORD NOT WRITTEN: {err}", file=sys.stderr)
        return 2
    prior = m.get("execution") if isinstance(m.get("execution"), dict) else {}
    block: dict = {"tracker": tracker}
    if a.clear_gates:
        block["gates"] = {}  # recorded as "none": a .loop fallback must not refill it
    else:
        if gates is not None:
            block["gates"] = gates
        elif "gates" in prior:
            block["gates"] = prior["gates"]
        merged = {**(prior.get("gate_commands") or {}), **cmds}
        if merged:
            block["gate_commands"] = merged
    block["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block["who"] = who
    if a.evidence and a.evidence.strip():
        block["evidence"] = " ".join(a.evidence.split())
    state, problem = classify_execution(block)
    if problem or state == "absent":
        print(f"RECORD NOT WRITTEN: {problem}", file=sys.stderr)
        return 2
    m["execution"] = block
    man.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    was = ""
    if prior:
        pstate, pproblem = classify_execution(prior)
        was = f" (was: {describe(prior) if not pproblem else pstate + ', malformed'})"
    print(f"recorded the execution settings in .claude/harness.json: {describe(block)}{was}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="target repository root")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read the record; write nothing")
    mode.add_argument("--record", choices=TRACKER_TYPES,
                      help="the tracker that holds this repository's tickets; local means "
                           "the person hands in the spec (a file or pasted text)")
    ap.add_argument("--who", help="the person who answered")
    ap.add_argument("--project-key", help="jira/youtrack project key, e.g. ACME")
    ap.add_argument("--done-state", action="append",
                    help="a tracker state that satisfies a blocker (repeatable); default: "
                         "the tracker's resolved states")
    ap.add_argument("--gates-json", help="a JSON file with the LOOP-GATES `gates` object")
    ap.add_argument("--gate-command", action="append",
                    help="ROLE=CMD, overriding the command detected for that gate role")
    ap.add_argument("--clear-gates", action="store_true",
                    help="drop recorded gates and gate commands")
    ap.add_argument("--evidence", help="the detector summary the person answered against")
    ap.add_argument("--json", action="store_true", help="--check: one JSON object on stdout")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        return _fail(f"{root} is not a directory")
    return check(root, a.json) if a.check else record(root, a)


if __name__ == "__main__":
    sys.exit(main())
