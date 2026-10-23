"""Helpers shared by the /harness-init scripts — one implementation per rule.

Not a runnable script: it carries no PEP 723 header because it is never run by
``uv run``, only imported by the scripts that sit beside it — the same
sibling-import mechanism ``install_assets.py`` already used to take
``EXCLUDED_DIRS``/``STACK_ORDER`` from ``detect_stack.py``. Stdlib only.

Every helper here was a verbatim copy in two or more scripts, and in the two
load-bearing cases the duplication was a correctness risk rather than mere
repetition: ``install_assets.py`` writes a rule's frontmatter and
``verify_harness.py`` grades it, so both must apply the same delimiter rule;
both resolve the manifest schema, so both must walk the same three-step chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

# Directories pruned from every bounded walk: VCS internals, dependency/build
# output, and cache dirs that would otherwise dominate a census. Extend by PR.
EXCLUDED_DIRS = {".git", "node_modules", "build", "dist", "vendor", ".dart_tool",
                 "__pycache__", ".venv", ".next", "target", "Pods", "reports"}

# Fixed stack order — stacks[0] is a contract, not filesystem luck, and it is
# also the concatenation order of a multi-stack adapter block. Extend by PR.
STACK_ORDER = ["python", "django", "fastapi", "flask", "node", "typescript",
               "nestjs", "react", "dart", "flutter", "functions", "go", "rust",
               "jvm"]

GIT_MISSING_RC = 127
GIT_MISSING_MSG = "git executable not found on PATH"


def _run_git(root: str, *args: str) -> subprocess.CompletedProcess:
    """Run `git -C <root> <args>` without raising: a missing git binary comes
    back as a completed process with GIT_MISSING_RC, so every caller grades one
    kind of value."""
    try:
        return subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=["git", "-C", root, *args],
            returncode=GIT_MISSING_RC,
            stdout="",
            stderr=GIT_MISSING_MSG,
        )


def _sha256_of(path: str) -> str | None:
    """The file's SHA-256, or None when it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _load_json_object(path: str) -> tuple[dict | None, str | None]:
    """Read `path` as a JSON object. Returns (obj, None) or (None, reason)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read: {exc}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "top level is not a JSON object"
    return data, None


def _try_read(path: str) -> str | None:
    """Return None if `path` is readable as text, else the reason it is not."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.read()
            return None
    except (OSError, UnicodeDecodeError) as exc:
        return str(exc)


def _resolve_asset(
    explicit: str | None, filename: str, plugin_root: str | None
) -> tuple[str | None, str | None]:
    """Resolve a settings-template / schema path through the three-step chain:
    an explicit flag, then the repository's ``templates/`` three levels above
    these scripts, then ``<plugin-root>/templates/``. Returns (path, error)."""
    if explicit:
        reason = _try_read(explicit)
        if reason is not None:
            return None, f"{filename}: {explicit}: {reason}"
        return explicit, None
    last_tried = None
    try:
        step2 = Path(__file__).resolve().parents[3] / "templates" / filename
        last_tried = str(step2)
        if step2.is_file():
            return str(step2), None
    except IndexError:
        pass
    if plugin_root:
        step3 = Path(plugin_root).resolve() / "templates" / filename
        last_tried = str(step3)
        if step3.is_file():
            return str(step3), None
    return None, f"{filename}: {last_tried}: cannot read"


def _outside(
    candidate: str,
    root_real: str,
    source_root_real: str | None = None,
    plugin_root_real: str | None = None,
) -> bool:
    """True when `candidate` resolves outside `root_real`, or inside one of the
    read-only roots a run must never write into."""
    p_real = os.path.realpath(candidate)
    if not p_real.startswith(root_real + os.sep):
        return True
    if source_root_real and p_real.startswith(source_root_real + os.sep):
        return True
    if plugin_root_real and p_real.startswith(plugin_root_real + os.sep):
        return True
    return False


def _split_frontmatter(text: str) -> tuple[list[str], list[str]] | None:
    """Split `text` at its YAML frontmatter delimiters.

    Returns (frontmatter lines, the rest starting at the closing ``---``), or
    None when there is no opening ``---`` line or no closing one. Lines are
    split on "\\n" and carry no line endings, so joining the opening line, the
    frontmatter and the rest with "\\n" reproduces the input byte for byte.

    The writer (install_assets.py) and the grader (verify_harness.py) share
    this so a rule can never be normalized under one delimiter rule and graded
    under another.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], lines[i:]
    return None


def _check_manifest_shape(manifest: dict, schema_required: list) -> str | None:
    """The two manifest rules the writer and the grader both apply: every
    schema-required key present, and no ``verdict`` key — the manifest records
    what was written, it never grades itself. Returns the failure sentence, or
    None."""
    missing = [name for name in schema_required if name not in manifest]
    if missing:
        return f"manifest missing required key(s): {', '.join(missing)}"
    if "verdict" in manifest:
        return "manifest carries a verdict key"
    return None


def _summarize(items: list[str], sep: str = "; ", limit: int = 10) -> str:
    """Join at most `limit` items, naming how many were elided."""
    shown = list(items)[:limit]
    extra = len(items) - len(shown)
    return sep.join(shown) + (f" … and {extra} more" if extra > 0 else "")


# --- the roborev registration decision (ADR-0005 D-1 / D-13) ----------------
# ONE classifier, because three hand-written copies had already disagreed:
# setup_roborev's audit() graded a record with an empty `pin` good while
# verify_harness's check-7 failed the identical record — and an empty pin is
# the exact defect v0.9.28.0 shipped to fix. Both scripts now call this.

def classify_review(review: object) -> tuple[str, str | None]:
    """(state, problem) for a manifest's `review` block.

    state is "registered" | "declined" | "absent". problem is None when the
    record is well-formed, else one sentence naming what is wrong with it.
    A record with no `decision` key predates D-13 and is a registration --
    that is what every one of them was.
    """
    if not isinstance(review, dict) or not review:
        return ("absent", "no `review` block: this repository is not registered AND no one "
                          "is recorded as having been asked (ADR-0005 D-13)")
    who = str(review.get("who") or "").strip()
    decision = review.get("decision")
    if decision is not None and decision not in ("registered", "declined"):
        # The schema's enum is not evaluated by either hand-rolled validator,
        # so an unknown value must be caught here or it reads as registered.
        return ("absent", f"`review.decision` is {decision!r}, which is neither "
                          '"registered" nor "declined"')
    if decision == "declined":
        reason = str(review.get("reason") or "").strip()
        if not who:
            return ("declined", "the decline records no `who`")
        if not reason:
            return ("declined", f"registration declined by {who} with no reason recorded")
        return ("declined", None)
    if not who:
        return ("registered", "the registration records no `who`")
    if not str(review.get("pin") or "").strip():
        return ("registered", f"registration recorded by {who} but `review.pin` is empty: the "
                              "record does not say which roborev version the approval was "
                              'given against, which is the point of D-1\'s "a file, not a memory"')
    return ("registered", None)


# --- the PR reviewer record (ENG-REV-01, business rule 5) -------------------
# Which reviewers a repository's PRs wait for. The harness was written for a
# team where Automata reviews every PR; a repository without it waited forever
# for a bot verdict that could never come. The record is ASKED, never assumed
# (the D-13 shape): "collaborators" is an ordinary answer, and absence means
# nobody was asked. ONE classifier for the writer (record_pr_review.py) and the
# grader (verify_harness.py check-11); pr-sync's review-mode.sh reads the same
# fields with jq and is tested against the same fixtures.

PR_REVIEWERS = ("automata", "automata_only", "collaborators", "none")
# The two modes whose verdict is the bot's approval at the head. They differ only in
# whether a collaborator's review is WORKED (`automata`) or merely reported
# (`automata_only`); both carry a `bot_login`, so every structural test is on this pair,
# never on the string "automata" alone.
PR_AUTOMATA_MODES = ("automata", "automata_only")
PR_REVIEW_DEFAULT_BOT = "automata-ai-bot"
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")


def classify_pr_review(block: object) -> tuple[str, str | None]:
    """(state, problem) for a manifest's `pr_review` block.

    state is "automata" | "automata_only" | "collaborators" | "none" | "absent".
    "automata_only" is "automata" with collaborators' reviews not worked -- a
    collaborator's request for changes is still reported, because GitHub blocks on it.
    "none" is an answer: no review watch, PRs merge on green CI. problem is None when the
    record is well-formed, else one sentence naming what is wrong with it.
    """
    if not isinstance(block, dict) or not block:
        return ("absent", "no `pr_review` block: nobody is recorded as having said whether "
                          "Automata reviews this repository's PRs")
    reviewer = block.get("reviewer")
    if reviewer not in PR_REVIEWERS:
        return ("absent", f"`pr_review.reviewer` is {reviewer!r}, which is not "
                          '"automata", "automata_only", "collaborators" or "none" '
                          "(or this record was written by a newer plugin)")
    who = str(block.get("who") or "").strip()
    if not who:
        return (reviewer, "the PR reviewer record names no `who`")
    if reviewer == "none":
        extra = [k for k in ("bot_login", "min_approvals") if k in block]
        if extra:
            return (reviewer, f"a `none` record carries `{extra[0]}`: it says nothing waits "
                              "for a review and names a reviewer rule anyway")
        return (reviewer, None)
    mins = block.get("min_approvals", 1)
    if isinstance(mins, bool) or not isinstance(mins, int) or not 1 <= mins <= 99:
        return (reviewer, f"`pr_review.min_approvals` is {mins!r}; it must be an integer "
                          "from 1 to 99")
    if reviewer in PR_AUTOMATA_MODES:
        bot = block.get("bot_login", PR_REVIEW_DEFAULT_BOT)
        if not isinstance(bot, str) or not _LOGIN_RE.match(bot):
            return (reviewer, f"`pr_review.bot_login` is {bot!r}, which is not a GitHub login")
    elif "bot_login" in block:
        return (reviewer, "a `collaborators` record carries a `bot_login`: the record says "
                          "there is no review bot and names one anyway")
    return (reviewer, None)


# --- the execution record (ENG-EXEC-01/05) -----------------------------------
# What dev-execution and loop-mode need to know about a repository and cannot
# read off its tree: which tracker holds its tickets, and how much autonomy its
# owners granted. One block, `execution`, in the same committed manifest as the
# PR reviewer record, so a repository answers these questions in one file
# (decided 2026-10-21). `.loop/config.yaml` is still read where it exists, as a
# fallback, never written. ONE classifier for the writer (record_execution.py),
# the grader (verify_harness.py check-15) and the reader (dev-execution's
# execution_config.py); all three are tested against the same shapes.

TRACKER_TYPES = ("jira", "youtrack", "github", "local")
# The LOOP-GATES vocabulary (docs/LOOP-GATES.md), copied here and nowhere else
# in Python. A key outside it is refused: a misspelled gate is a gate that never
# trips, and that failure is silent.
EXECUTION_GATES = {
    "budget_gate": ("on_exceed", ("proceed", "abort", "escalate_silent")),
    "verifier_blocking_gate": ("on_blocking", ("auto_fix", "abort", "escalate_silent")),
    "adr_conflict_gate": ("on_high_conflict", ("proceed_with_record", "abort", "escalate_silent")),
    "adr_rule_violation_gate": ("on_violation", ("abort",)),
    "spec_clarification_gate": ("on_gaps", ("use_defaults_and_flag", "abort", "escalate_silent")),
    "self_healing_exhaust_gate": ("on_exhausted", ("escalate_silent", "abort")),
}
GATE_ROLES = ("install", "build", "typecheck", "lint", "test", "coverage", "contract",
              "e2e", "secrets")
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_EXECUTION_KEYS = {"tracker", "gates", "gate_commands", "date", "who", "evidence"}


def _classify_gates(gates: object) -> str | None:
    if not isinstance(gates, dict):
        return "`execution.gates` must be an object"
    for name, body in gates.items():
        if name not in EXECUTION_GATES:
            return f"`execution.gates.{name}` is not a gate (docs/LOOP-GATES.md names six)"
        key, allowed = EXECUTION_GATES[name]
        if not isinstance(body, dict) or body.get(key) not in allowed:
            return (f"`execution.gates.{name}.{key}` is "
                    f"{(body or {}).get(key) if isinstance(body, dict) else body!r}; "
                    f"it must be one of {', '.join(allowed)}")
        extra = set(body) - {key, "auto_fix_max_retries"}
        if extra:
            return f"`execution.gates.{name}` carries unknown key `{sorted(extra)[0]}`"
        if "auto_fix_max_retries" in body:
            n = body["auto_fix_max_retries"]
            if name != "verifier_blocking_gate":
                return f"`auto_fix_max_retries` belongs to verifier_blocking_gate, not {name}"
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 10:
                return f"`auto_fix_max_retries` is {n!r}; it must be an integer from 0 to 10"
    return None


def classify_execution(block: object) -> tuple[str, str | None]:
    """(state, problem) for a manifest's `execution` block.

    state is the recorded tracker type, "absent" when there is no block, or
    "invalid" when a block exists but cannot be read. problem is None when the
    record is well-formed, else one sentence naming what is wrong. "local" is an
    answer (tickets are specs the person hands in), not a missing tracker.
    """
    if block is not None and not isinstance(block, dict):
        return ("invalid", f"`execution` is {type(block).__name__}, not an object")
    if not block:
        return ("absent", "no `execution` block: nobody is recorded as having said which "
                          "tracker holds this repository's tickets")
    extra = set(block) - _EXECUTION_KEYS
    if extra:
        return ("invalid", f"`execution` carries unknown key `{sorted(extra)[0]}` "
                          "(or this record was written by a newer plugin)")
    tracker = block.get("tracker")
    ttype = tracker.get("type") if isinstance(tracker, dict) else None
    if ttype not in TRACKER_TYPES:
        return ("invalid", f"`execution.tracker.type` is {ttype!r}, which is not "
                          + ", ".join(f'"{t}"' for t in TRACKER_TYPES)
                          + " (or this record was written by a newer plugin)")
    if not str(block.get("who") or "").strip():
        return (ttype, "the execution record names no `who`")
    if set(tracker) - {"type", "project_key", "done_states"}:
        return (ttype, f"`execution.tracker` carries unknown key "
                       f"`{sorted(set(tracker) - {'type', 'project_key', 'done_states'})[0]}`")
    pkey = tracker.get("project_key")
    if ttype in ("jira", "youtrack"):
        if not isinstance(pkey, str) or not _PROJECT_KEY_RE.match(pkey):
            return (ttype, f"`execution.tracker.project_key` is {pkey!r}; a {ttype} record "
                           "names the project key its tickets carry (e.g. ACME)")
    elif pkey is not None:
        return (ttype, f"a {ttype} record carries a `project_key`; only jira and youtrack "
                       "keys tickets by project")
    done = tracker.get("done_states")
    if done is not None and (not isinstance(done, list) or not done
                             or not all(isinstance(s, str) and s.strip() for s in done)):
        return (ttype, "`execution.tracker.done_states` must be a non-empty list of state names")
    if "gates" in block:
        problem = _classify_gates(block["gates"])
        if problem:
            return (ttype, problem)
    cmds = block.get("gate_commands")
    if cmds is not None:
        if not isinstance(cmds, dict):
            return (ttype, "`execution.gate_commands` must be an object of role: command")
        for role, cmd in cmds.items():
            if role not in GATE_ROLES:
                return (ttype, f"`execution.gate_commands.{role}` is not a role "
                               f"({', '.join(GATE_ROLES)})")
            if not isinstance(cmd, str) or not cmd.strip() or "\n" in cmd:
                return (ttype, f"`execution.gate_commands.{role}` must be one command line")
    return (ttype, None)
