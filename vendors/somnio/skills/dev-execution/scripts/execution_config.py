#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0,<7"]
# ///
"""What an execution run needs to know about the target repository, in one reading.

The one reader for dev-execution Step 0 and loop-mode Step 1, so the two skills
never disagree about the tracker, the autonomy gates or the gate commands.
Read-only: writes nothing, anywhere.

    uv run execution_config.py [<root>] [--key KEY] [--json]

Sources, in order, for each answer:

  tracker        `.claude/harness.json` `execution.tracker`  →  `.loop/config.yaml`
                 `mcp_integrations.ticket_source`  →  jira, the area default, unless
                 CLAUDE.md or AGENTS.md names another tracker  →  unrecorded (asked)
  gates          `execution.gates`  →  `.loop/config.yaml` `gates`  →  none (interactive)
  gate commands  `execution.gate_commands`  →  `.loop` `harness.gate_commands`  →  inferred
                 from the manifest's `commands`/`allowlist` and the repo's declared scripts
                 (harness-init's detect_toolchain.py, imported, never copied)
  runbook        `docs/runbooks/ticket-pipeline.md`, exact case  →  the shipped default

harness.json is the record (decided 2026-10-21); `.loop/config.yaml` is read only
where it exists and never written. When both name a tracker and they disagree,
the run stops: that is a finding, not a tiebreak.

Inline dependency: `pyyaml`, for the `.loop/config.yaml` fallback only — the
standard library has no YAML parser. Without it (or without the file) the
fallback is skipped and says so.

Prints shell-sourceable KEY=VALUE lines (or one JSON object with --json):
  TRACKER=jira|youtrack|github|local|unrecorded   PROJECT_KEY=<key>
  DONE_STATES='<a>|<b>'                            TRACKER_SOURCE=harness.json|loop-config|default|unrecorded
  TRACKER_CLI=<abs path>|''                         TRACKER_EVIDENCE='<one line>'
  GATES=recorded|interactive    GATES_SOURCE=harness.json|loop-config|none    FIX_CAP=<n>|none
  RUNBOOK=<path>|default        GATE_<ROLE>=<command>|GAP   (one line per role)

exit: 0 printed · 2 usage · 3 a record is malformed, or harness.json and
      .loop/config.yaml name different trackers (nothing printed; stderr names it)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True
_HI = Path(__file__).resolve().parents[2] / "harness-init" / "scripts"
sys.path.insert(0, str(_HI))
from _common import (  # noqa: E402
    GATE_ROLES,
    TRACKER_TYPES,
    _classify_gates,
    classify_execution,
)

try:
    import detect_toolchain  # noqa: E402
except Exception:  # pragma: no cover - a broken sibling degrades to the manifest only
    detect_toolchain = None

RUNBOOK_PATH = ("docs", "runbooks", "ticket-pipeline.md")
# Which script names fill which gate role. Ordered: the first match wins, so the
# coverage-gated variant is never mistaken for the plain test target (business
# rule 9: `test` is not the pre-push gate).
_ROLE_PATTERNS = {
    "coverage": r"^(test:cov|test:coverage|coverage|test-cov|test_coverage|cov)$",
    "test": r"^(test|test:unit|unit)$",
    "typecheck": r"^(typecheck|type-check|types|tsc|check-types|typecheck:all)$",
    "build": r"^(build)$",
    "contract": r"^(openapi:check|swagger:check|contract|contract:check|api:check|openapi)$",
    "e2e": r"^(test:e2e:local|test:e2e|e2e)$",
}
_LINT_EXCLUDE = re.compile(r"(fix|prune|write|format)", re.I)
_INSTALL_BY_RUNNER = {
    "pnpm": "pnpm install --frozen-lockfile",
    "npm": "npm ci",
    "yarn": "yarn install --immutable",
    "bun": "bun install --frozen-lockfile",
}
_TRACKER_WORDS = {
    "youtrack": re.compile(r"\byoutrack\b", re.I),
    "jira": re.compile(r"\bjira\b|atlassian\.net", re.I),
    "github": re.compile(r"\bgithub issues?\b", re.I),
}


def _err(msg: str) -> int:
    print(f"execution-config: {msg}", file=sys.stderr)
    return 3


def _load_json(p: Path) -> dict | None:
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _load_loop(root: Path) -> tuple[dict | None, str | None, str | None]:
    """(config, note, problem). A .loop that exists and cannot be read is a problem
    (exit 3), never a silent fallback: its tracker or gates would vanish unseen."""
    p = root / ".loop" / "config.yaml"
    if not p.is_file():
        return None, None, None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None, ".loop/config.yaml exists but pyyaml is unavailable; fallback skipped", None
    try:
        obj = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - any parse failure is the same finding
        return None, None, f".loop/config.yaml is unreadable ({e.__class__.__name__})"
    if not isinstance(obj, dict):
        return None, None, ".loop/config.yaml is not a mapping"
    for k in ("mcp_integrations", "harness", "gates"):
        if obj.get(k) is not None and not isinstance(obj[k], dict):
            return None, None, f".loop/config.yaml `{k}` is not a mapping"
    return obj, None, None


def _exact_case(root: Path, parts: tuple[str, ...]) -> Path | None:
    """The path only when every component matches in case. On a case-insensitive
    disk `Docs/` answers to `docs/`; on Linux CI it does not, and a run that
    finds the runbook on one machine must find it on all of them."""
    cur = root
    for part in parts:
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        if part not in names:
            return None
        cur = cur / part
    return cur if cur.is_file() else None


def _ytctl() -> str:
    # $YTCTL, then the copy this plugin ships (skills/youtrack), then a user-scope install.
    cands = [os.environ.get("YTCTL", ""),
             str(Path(__file__).resolve().parents[2] / "youtrack" / "scripts" / "ytctl")]
    for base in (os.environ.get("CLAUDE_CONFIG_DIR", ""), str(Path.home() / ".claude"),
                 str(Path.home() / ".agents")):
        if base:
            cands.append(str(Path(base) / "skills" / "youtrack" / "scripts" / "ytctl"))
    cands.append(shutil.which("ytctl") or "")
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return ""


def _named_trackers(root: Path) -> dict[str, dict[str, int]]:
    named: dict[str, dict[str, int]] = {}
    for doc in ("CLAUDE.md", "AGENTS.md"):
        try:
            text = (root / doc).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, rx in _TRACKER_WORDS.items():
            n = len(rx.findall(text))
            if n:
                named.setdefault(doc, {})[name] = n
    return named


def _evidence(root: Path, key: str | None) -> str:
    hits = [f"{doc} names {name} {n}x"
            for doc, names in _named_trackers(root).items() for name, n in names.items()]
    if key:
        hits.append(f"key {key} (prefix {key.split('-')[0]})")
    yt = _ytctl()
    hits.append("ytctl found" if yt else "ytctl not found")
    return "; ".join(hits) or "no tracker named in CLAUDE.md or AGENTS.md"


def _script_name(cmd: str) -> str:
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return ""
    if len(toks) >= 3 and toks[1] == "run":
        return toks[2]
    if len(toks) == 2 and toks[0] in ("make", "pnpm", "npm", "yarn", "bun"):
        return toks[1]
    return ""


def _infer(root: Path, man: dict | None) -> dict[str, str]:
    cands: list[str] = []
    runner = ""
    if man:
        for c in man.get("commands") or []:
            if isinstance(c, dict) and isinstance(c.get("command"), str):
                cands.append(c["command"])
        allow = man.get("allowlist") if isinstance(man.get("allowlist"), dict) else {}
        cands += [c for c in allow.get("commands") or [] if isinstance(c, str)]
        runner = allow.get("runner") or ""
    if detect_toolchain is not None:
        try:
            cmds = detect_toolchain.detect(str(root))["commands"]
            cands += cmds.get("verification") or []
            runner = runner or cmds.get("runner") or ""
        except Exception:  # noqa: BLE001 - the detector is evidence, never a gate
            pass
    seen: list[str] = []
    for c in cands:
        if c not in seen:
            seen.append(c)
    out: dict[str, str] = {}
    for c in seen:
        if "install" in c.split()[:2] and ("frozen" in c or c.split()[:2] == ["npm", "ci"]
                                            or "immutable" in c or "enforce-lockfile" in c):
            out.setdefault("install", c)
    if "install" not in out and runner in _INSTALL_BY_RUNNER:
        out["install"] = _INSTALL_BY_RUNNER[runner]
    for role, pat in _ROLE_PATTERNS.items():
        for c in seen:
            if re.match(pat, _script_name(c)):
                out.setdefault(role, c)
                break
    lints: list[str] = []
    names: set[str] = set()
    for c in seen:  # `pnpm lint` and `pnpm run lint` are one gate, run once
        name = _script_name(c)
        if name.split(":")[0] == "lint" and not _LINT_EXCLUDE.search(name[4:]) \
                and name not in names:
            names.add(name)
            lints.append(c)
    if lints:
        out["lint"] = " && ".join(lints)
    return out


def resolve(root: Path, key: str | None) -> tuple[dict | None, str | None]:
    man_path = root / ".claude" / "harness.json"
    man = _load_json(man_path) if man_path.is_file() else None
    if man_path.is_file() and man is None:
        return None, f"{man_path} is not readable JSON"
    block = (man or {}).get("execution")
    rec_state, problem = classify_execution(block)
    if block and problem:
        return None, (f"malformed execution record in {man_path} — {problem} "
                      "(fix: record_execution.py --record)")
    loop, loop_note, loop_problem = _load_loop(root)
    if loop_problem:
        return None, loop_problem
    loop = loop or {}
    ts = (loop.get("mcp_integrations") or {}).get("ticket_source") or {}
    loop_type = ts.get("type") if isinstance(ts, dict) else None
    if loop_type is not None and loop_type not in TRACKER_TYPES:
        return None, f".loop/config.yaml ticket_source.type is {loop_type!r}, not a known tracker"

    r: dict = {"notes": [loop_note] if loop_note else []}
    if rec_state != "absent":
        t = block["tracker"]
        if loop_type and loop_type != t["type"]:
            return None, (f"harness.json records tracker {t['type']} and .loop/config.yaml "
                          f"says {loop_type}: settle it in harness.json and fix the other")
        r.update(tracker=t["type"], project_key=t.get("project_key", ""),
                 done_states=t.get("done_states") or [], tracker_source="harness.json")
    elif loop_type:
        r.update(tracker=loop_type, project_key=str(ts.get("project_key") or ""),
                 done_states=[], tracker_source="loop-config")
        r["notes"].append("tracker read from .loop/config.yaml; record it in harness.json "
                          "(record_execution.py) so one file answers")
    elif not {n for names in _named_trackers(root).values() for n in names} - {"jira"}:
        # Most of the area's repos keep tickets in Jira (2026-10-22): a repo that names
        # no other tracker runs on Jira without a question. One that names another asks.
        r.update(tracker="jira", project_key=(key or "").split("-")[0] if key else "",
                 done_states=[], tracker_source="default")
        r["notes"].append("tracker defaulted to jira; record it in harness.json "
                          "(record_execution.py) if this repo uses another tracker")
    else:
        r.update(tracker="unrecorded", project_key="", done_states=[],
                 tracker_source="unrecorded")
    r["tracker_cli"] = _ytctl() if r["tracker"] in ("youtrack", "unrecorded") else ""
    r["tracker_evidence"] = (_evidence(root, key)
                             if r["tracker_source"] in ("default", "unrecorded") else "")

    gates = None
    # A recorded `gates` key is the answer even when empty: `--clear-gates` writes {}
    # so that a team-local .loop cannot hand back the autonomy a person just removed.
    if rec_state != "absent" and "gates" in block:
        gates, r["gates_source"] = block["gates"], "harness.json"
    elif isinstance(loop.get("gates"), dict):
        g = dict(loop["gates"])  # a misspelled gate is refused here too, never dropped
        problem = _classify_gates(g)
        if problem:
            return None, f".loop/config.yaml: {problem.replace('execution.gates', 'gates')}"
        gates, r["gates_source"] = g, "loop-config"
    else:
        r["gates_source"] = "none"
    r["gates"] = gates or {}
    cap = ((gates or {}).get("verifier_blocking_gate") or {}).get("auto_fix_max_retries")
    r["fix_cap"] = cap if isinstance(cap, int) else "none"

    cmds = _infer(root, man)
    loop_cmds = ((loop.get("harness") or {}).get("gate_commands") or {})
    if isinstance(loop_cmds, dict):
        for k, v in loop_cmds.items():
            role = "coverage" if k == "tests" else k
            if role in GATE_ROLES and isinstance(v, str) and v.strip():
                cmds[role] = v.strip()
    if rec_state != "absent":
        cmds.update(block.get("gate_commands") or {})
    r["gate_commands"] = {role: cmds.get(role, "GAP") for role in GATE_ROLES}

    rb = _exact_case(root, RUNBOOK_PATH)
    r["runbook"] = str(rb.relative_to(root)) if rb else "default"
    return r, None


def main(argv: list[str]) -> int:
    as_json, key, root = False, None, None
    it = iter(argv)
    for a in it:
        if a == "--json":
            as_json = True
        elif a == "--key":
            key = next(it, None)
            if not key:
                print("usage: execution_config.py [<root>] [--key KEY] [--json]", file=sys.stderr)
                return 2
        elif a.startswith("-"):
            print(f"execution-config: unknown flag {a}", file=sys.stderr)
            return 2
        else:
            root = a
    root_p = Path(root) if root else Path(os.popen("git rev-parse --show-toplevel 2>/dev/null")
                                           .read().strip() or ".")
    if not root_p.is_dir():
        print(f"execution-config: {root_p} is not a directory", file=sys.stderr)
        return 2
    r, problem = resolve(root_p.resolve(), key)
    if problem:
        return _err(problem)
    if as_json:
        print(json.dumps(r, indent=2, sort_keys=True))
        return 0
    q = shlex.quote
    print(f"TRACKER={r['tracker']}")
    print(f"PROJECT_KEY={q(r['project_key'])}")
    print(f"DONE_STATES={q('|'.join(r['done_states']))}")
    print(f"TRACKER_SOURCE={r['tracker_source']}")
    print(f"TRACKER_CLI={q(r['tracker_cli'])}")
    print(f"TRACKER_EVIDENCE={q(r['tracker_evidence'])}")
    print(f"GATES={'recorded' if r['gates'] else 'interactive'}")
    print(f"GATES_SOURCE={r['gates_source']}")
    print(f"FIX_CAP={r['fix_cap']}")
    print(f"RUNBOOK={q(r['runbook'])}")
    for role, cmd in r["gate_commands"].items():
        print(f"GATE_{role.upper()}={q(cmd)}")
    for n in r["notes"]:
        print(f"execution-config: note: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
