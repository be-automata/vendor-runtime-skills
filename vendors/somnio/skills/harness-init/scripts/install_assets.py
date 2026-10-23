#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install the Somnio harness assets into a target repository — plan, apply, uninstall.

Read-mostly outside of ``--apply``/``--uninstall``: ``--plan`` computes the complete write
plan and writes nothing under ``--root``. ``--apply`` performs the write groups (rule files,
the per-agent adapter rule files, ``.claude/settings.json``, ``CLAUDE.md``, ``.gitignore``,
``.claude/ship-gate.json``), then the CLI skills step, and writes ``.claude/harness.json``
last, only after every other write in the run has succeeded. ``.claude/settings.json`` is produced only by
``plugins/somnio-engineering-ai/scripts/merge-settings.py``, invoked here as a subprocess —
this script never hand-rolls a settings merge.

This script is the rule **frontmatter writer**: at copy time a rule's ``globs:`` key
(scalar, comma-separated scalar, or YAML list) becomes a ``paths:`` block list, so the
installed rule lazy-loads. Every other frontmatter key and the whole body are copied
verbatim; a rule with no frontmatter, or one that already carries ``paths:``, is copied
byte-for-byte. Each ``installed[]`` entry records ``sha256`` (the bytes as written) and
``sha256_pre_optimize`` (the verbatim source bytes); they are equal when nothing was
rewritten.

A re-run keeps what this harness accepted (HN-10b). A rule whose on-disk bytes still match
the manifest's ``sha256`` but differ from what the corpus would write now — an
``optimize-context-rules`` rewrite blessed by ``--refresh-hashes`` — is planned as
``{"action": "keep"}`` and never rewritten, provided the corpus did not move: the entry's
``source_rev`` still matches the source rev and its ``sha256_pre_optimize`` still equals the
verbatim source file's hash. When either moved, the newer corpus wins (overwrite, or a drift
conflict). The manifest also carries ``objective`` and ``commands`` so a flagless re-apply
renders the same CLAUDE.md commands table and ship-gate candidates; an explicit flag wins.

Network policy. ``--network`` takes the JSON ``detect_network.py`` printed. Its
``settings_fragment`` joins the settings merge between the template and the repo's own
settings — template, then fragment, then the repo — so the repo still wins on anything it
tuned and the allowlists end up as the union of all three. The detected vendors, the seeded
domains and exclusions, and the **gaps** (never applied — reported, and recorded so the
doctor and the hand-over can name them) go into the manifest's optional ``network`` key, and a
flagless re-apply reads that back instead of dropping the target to the template's baseline.
An explicit, parseable ``--network`` always wins. A missing, empty or malformed payload is
never a refusal: no fragment, no gaps, and the run is exactly what it would have been without
the flag.

Declared commands (#403). ``--toolchain`` takes the JSON ``detect_toolchain.py`` printed. Its
``commands.settings_fragment`` joins the same merge, one layer after the network fragment —
template, network, commands, then the repo — and carries the ``Bash(...)`` allow rules for the
verification commands the repository ACTUALLY DECLARES. This is a fragment rather than a
template row because it used to be one: seven ``Bash(pnpm run ...)`` rules sat in
``templates/target-settings.json``, lifted from a single backend monorepo, and on the next repo
the template touched five of them named nothing that existed. A per-repo fact belongs in a
per-repo reading, and a repository that declares nothing gets no allow rules — no names beats
invented names. The runner, the file that named it, the seeded ``commands`` and the ones
``refused`` with their reason go into the manifest's optional ``allowlist`` key; a flagless
re-apply reads that back. Same three lanes and the same defensive contract as ``--network``.

Every refusal (exit 2) is raised in the plan phase, before the first write: the marker
shapes of ``CLAUDE.md``/``.gitignore`` and the structural validity of the manifest are
graded there, so ``--apply`` exiting 2 always means nothing was written.

Usage:
    uv run install_assets.py --root <target> --source <resolver JSON path or '-'>
                              --stacks <comma list> --plugin-root <dir>
                              (--plan | --apply | --uninstall)
                              [--objective <text>] [--commands <json>] [--accept-drift]
                              [--network <path>] [--toolchain <path>]
                              [--adopt-existing]
                              [--take <comma list>] [--keep <comma list>]
                              [--agents <comma list>] [--skills <comma list>]
                              [--no-cli-skills] [--cli-version-pin <ver>]
                              [--plan-out <path>] [--settings-template <path>] [--schema <path>]
                              [--json]

stdout: the plan JSON (one line, ``mode: "plan"`` or ``"apply"``) for --plan/--apply;
        ``{"mode":"uninstall","removed":[...],"restored":[...],"skipped":[...],
        "stripped":[...],"manifest_removed":true}`` (one line) for --uninstall. A refusal
        prints nothing on stdout.
stderr: a human summary line, suppressed by --json. On a refusal, exactly one line
        beginning "error:".
exit:   0 success (--plan always; --apply/--uninstall with no conflicts) · 1 --apply with
        conflicts · 2 refusal, nothing written

Agents. ``--agents`` (default ``claude``) names who will work in the target repo. Every
non-claude agent with an adapter at the pin gets one rule file — a managed block for codex
(``AGENTS.md``), copilot (``.github/copilot-instructions.md``) and windsurf
(``.windsurfrules``), a per-stack copy for cursor (``.cursor/rules/somnio-<stack>.md``) — and
every agent, claude included, gets the CLI catalog's skills through one
``somnio skills install --agent <id> --project`` call with ``cwd`` = the target root. That is
``skills install``; ``rules install`` is never run (ADR-0003 D-1/D-2). The CLI's own
``.somnio-skills.json`` stays the owner of what it wrote — those paths are recorded under the
manifest's ``cli_installed`` and are never deleted by ``--uninstall``, which lists them and
prints the CLI's own removal command instead. A failing or absent CLI is a recorded
``not_in_scope`` gap, never a change of exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# _common.py ships beside this script and owns what more than one of these
# scripts needs: the canonical stack order (which fixes the concatenation order
# of a multi-stack adapter block), the directory names a bounded walk must never
# descend into, and the small read helpers below. Imported, never copied — two
# copies would be two answers, and the frontmatter splitter in particular must
# be the same one verify_harness.py grades with.
# Importing a sibling module byte-compiles it unless this is set: a plugin
# cache (or a rules source) is read-only by contract, and a __pycache__/ dir
# appearing beside these scripts is a write nobody asked for. The env var
# PYTHONDONTWRITEBYTECODE=1 on every SKILL.md invocation covers the
# interpreter; this flag covers the import below when it is not set.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXCLUDED_DIRS,
    STACK_ORDER,
    _check_manifest_shape,
    _load_json_object,
    _outside,
    _resolve_asset,
    _run_git,
    _sha256_of,
    _split_frontmatter,
    _try_read,
)

# --- Module constants. Extend by PR. ---------------------------------------

BLAST_RADIUS = [".claude/harness.json", ".claude/.harness/"]
PRESERVE_KEYS = ["extraKnownMarketplaces", "enabledPlugins"]
RULES_SUBPATH = "agent-rules/rules"
UPSTREAM_REPO = "somnio-software/somnio-ai-tools"
TIER_ENUM = {"env", "owned-clone", "pub-cache", "working-copy"}

CLAUDE_BEGIN = "<!-- somnio:harness:begin -->"
CLAUDE_END = "<!-- somnio:harness:end -->"
GITIGNORE_BEGIN = "# somnio:harness:begin"
GITIGNORE_END = "# somnio:harness:end"
GITIGNORE_BLOCK = (
    "# somnio:harness:begin\n"
    ".claude/.harness/\n"
    ".claude/*.local.md\n"
    ".claude/settings.json.bak\n"
    "# somnio:harness:end"
)
GITIGNORE_BASE_LINES = [".claude/.harness/", ".claude/*.local.md", ".claude/settings.json.bak"]


def _gitignore_block(negations: list[str]) -> str:
    """The managed .gitignore block: the three base lines, then the `!`
    re-include lines this run needs so its own writes are not ignored by the
    target's rules (issue #25). Deterministic order, so a re-run is unchanged."""
    lines = [GITIGNORE_BEGIN, *GITIGNORE_BASE_LINES]
    for line in sorted(set(negations)):
        lines.append(line)
    lines.append(GITIGNORE_END)
    return "\n".join(lines)


def _check_ignored(root: str, rel_paths: list[str]) -> list[dict]:
    """Ask git which of `rel_paths` its ignore rules would drop, before anything
    is written (`--no-index` grades paths that do not exist yet). Returns one
    {path, source, line, pattern} per ignored path; an empty list when none
    is, when git is missing, or when the root is not a repository."""
    if not rel_paths:
        return []
    res = _run_git(root, "check-ignore", "-v", "--no-index", "--", *rel_paths)
    if res.returncode != 0:
        return []
    out: list[dict] = []
    for line in res.stdout.splitlines():
        # "<source>:<linenum>:<pattern>\t<path>"
        head, sep, path = line.partition("\t")
        if not sep:
            continue
        parts = head.split(":", 2)
        if len(parts) != 3:
            continue
        source, lineno, pattern = parts
        if pattern.startswith("!"):
            continue  # matched a negation: re-included, not ignored
        if source == ".gitignore" and pattern in GITIGNORE_BASE_LINES:
            continue
        out.append({"path": path, "source": source, "line": lineno, "pattern": pattern})
    return out


def _negation_for(path: str, pattern: str) -> list[str] | None:
    """The `!` lines that re-include `path` past `pattern`, or None when git
    cannot re-include it at all: a pattern that excludes a parent *directory*
    of the path (".claude", ".claude/") closes the subtree, and no negation
    below it is ever consulted."""
    pat = pattern.strip().lstrip("/").rstrip("/")
    parents = []
    parts = path.split("/")
    for i in range(1, len(parts)):
        parents.append("/".join(parts[:i]))
    if pat in parents and not any(ch in pat for ch in "*?["):
        return None
    if path.startswith(".claude/rules/"):
        return ["!.claude/rules/", "!.claude/rules/**"]
    return ["!" + path]


def _glob_to_regex(pattern: str) -> re.Pattern:
    p = pattern.strip().strip('"').strip("'").lstrip("/")
    out = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += ".*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        elif p[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile("^" + out + "$")


def _rule_paths(text: str) -> list[str]:
    """The `paths:` list of a normalized rule (block list items only)."""
    parts = _split_frontmatter(text)
    if parts is None:
        return []
    fm, _rest = parts
    patterns: list[str] = []
    in_paths = False
    for line in fm:
        if re.match(r"^paths:\s*$", line):
            in_paths = True
            continue
        if in_paths:
            m = re.match(r"^\s+-\s+(.+)$", line)
            if m:
                patterns.append(m.group(1).strip().strip('"').strip("'"))
                continue
            in_paths = False
    return patterns

# --- Agents: rule adapters and the CLI skills step -------------------------
# The agent ids the somnio CLI's `skills install --agent` accepts. Extend by PR,
# together with the CLI pin. `--agent` is single-valued (last wins), so the apply
# phase makes one call per agent, never one call with a list.
AGENT_IDS = [
    "claude", "cursor", "gemini", "antigravity", "codex", "auggie", "amp",
    "aider", "cline", "opencode", "codebuddy", "qwen", "copilot", "windsurf",
    "roo", "kilocode", "amazonq",
]

# Rule adapters the pinned corpus ships, per agent. `dirname` is under
# agent-rules/adapters/; `filename` is the per-stack file inside
# <dirname>/<stack>/, matched by name so a dotfile like `.windsurfrules`
# resolves (None = take the single .md/.mdc found there). `target` is the
# managed-block file in the target repo. claude is deliberately absent: our rules
# path is the copied .claude/rules/ tree plus the frontmatter normalizer, never
# the claude adapter's generated CLAUDE.md fragment (ADR-0003 D-1/D-2).
# An agent with no row here, and an agent whose row has no file for a requested
# stack at the pin, is recorded in not_in_scope and still gets the CLI skills.
ADAPTERS = {
    "codex": {"dirname": "codex", "filename": "system-prompt.md", "target": "AGENTS.md"},
    "copilot": {
        "dirname": "copilot",
        "filename": "copilot-instructions.md",
        "target": ".github/copilot-instructions.md",
    },
    "windsurf": {
        "dirname": "windsurf",
        "filename": ".windsurfrules",
        "target": ".windsurfrules",
    },
    # cursor's adapter is `adapters/cursor/rules/<stack>/*.mdc`, copied per file
    # rather than concatenated into a managed block.
    "cursor": {"dirname": "cursor/rules", "filename": None, "target": ".cursor/rules/"},
}
ADAPTER_SUBPATH = "agent-rules/adapters"
ADAPTER_ORIGIN_PREFIX = "adapter-block:"

# The skill set a run installs, derived from the selected stacks. A stack with a
# `<stack>_plan`/`<stack>_health` pair contributes it; every run also gets the
# core five; flutter additionally gets dart_model_from_json. soc2_audit,
# iso27001_audit, clockify_tracker, dora_metrics and workflow_builder are
# deliberately never installed by harness-init. Extend by PR.
STACK_SKILLS = {
    "flutter": ["flutter_plan", "flutter_health"],
    "nestjs": ["nestjs_plan", "nestjs_health"],
    "react": ["react_plan", "react_health"],
    "python": ["python_plan", "python_health"],
    "angular": ["angular_plan", "angular_health"],
    "angularjs": ["angularjs_plan", "angularjs_health"],
}
CORE_SKILLS = [
    "git_commit_format", "git_branch_format", "security_audit",
    "harness_audit", "optimize_claude_config",
]
FLUTTER_EXTRA_SKILLS = ["dart_model_from_json"]

CLI_BIN = "somnio"
CLI_MANIFEST_NAME = ".somnio-skills.json"
CLI_TIMEOUT_S = 120
CLI_UNINSTALL_HINT = (
    "CLI-installed skills are owned by the somnio CLI's own manifest and are left "
    "in place; remove them with: somnio skills remove"
)

HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
GENERATED_BY_RE = re.compile(r"^somnio-engineering-ai@.+$")
GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
COMMANDS_ERROR = "--commands must be a JSON array of strings or {command, proves} objects"
OBJECTIVE_DEFAULT = "TODO: state the objective"
SETTINGS_REL = ".claude/settings.json"
SETTINGS_BAK_REL = ".claude/settings.json.bak"
PLACEHOLDER_SHA = "0" * 64

# Frontmatter normalization: a top-level `key:` line, and a YAML block-list item.
FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(.*)$")
FM_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")


# --- Small stdlib-only helpers ----------------------------------------------


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _write_text(path: Path, text: str) -> None:
    _write_bytes(path, text.encode("utf-8"))


def _rev_matches(a: str, b: str) -> bool:
    """Prefix-tolerant rev comparison: true when both are hex revs and one
    prefixes the other."""
    if not (HEX_RE.match(a) and HEX_RE.match(b)):
        return False
    return a.startswith(b) or b.startswith(a)


def _source_unmoved(entry: dict, pre_sha: str, source_rev: str) -> bool:
    """True when the manifest entry proves the corpus file behind it has not
    moved: its `source_rev` still matches the current source rev and its
    `sha256_pre_optimize` — the verbatim source hash the writer recorded —
    equals the current verbatim source hash. An entry without the verbatim
    hash cannot prove it, and is graded as moved."""
    prev_pre = entry.get("sha256_pre_optimize")
    prev_rev = entry.get("source_rev")
    if not isinstance(prev_pre, str) or not isinstance(prev_rev, str):
        return False
    return prev_pre.lower() == pre_sha.lower() and _rev_matches(prev_rev, source_rev)


def _unchanged_note(unchanged: list[str], kept: list[str]) -> str:
    """The parenthesis of the plan summary: `(9 unchanged)`, or
    `(9 unchanged, 3 kept (accepted rewrites))` when a keep is planned."""
    if not unchanged:
        return ""
    note = f"{len(unchanged)} unchanged"
    if kept:
        note += f", {len(kept)} kept (accepted rewrites)"
    return f" ({note})"


def _carried_note(carried_forward: list[str], commands: list[dict]) -> str:
    """The carry-forward clause of a plan/apply summary, empty when nothing
    was read back from the manifest."""
    parts: list[str] = []
    if "commands" in carried_forward:
        parts.append(f"commands: carried forward ({len(commands)})")
    if "objective" in carried_forward:
        parts.append("objective: carried forward")
    return f", {', '.join(parts)}" if parts else ""


def _stacks_in_order(stacks: list[str]) -> list[str]:
    """The selected stacks in `STACK_ORDER`, unknown names last, alphabetically.

    This is the concatenation order of a multi-stack adapter block, so it is a
    contract: the same selection must always produce the same bytes.
    """
    return sorted(
        stacks,
        key=lambda s: (STACK_ORDER.index(s) if s in STACK_ORDER else len(STACK_ORDER), s),
    )


def _valid_stack_name(name: str) -> bool:
    if not name:
        return False
    # The name reaches a Markdown table cell and the managed CLAUDE.md block;
    # anything but [a-z0-9_-] can break a row or forge a marker.
    return re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) is not None


def _parse_stacks(raw: str) -> tuple[list[str] | None, str | None]:
    if raw == "":
        return [], None
    parts = raw.split(",")
    for part in parts:
        if not _valid_stack_name(part):
            return None, part
    return list(dict.fromkeys(parts)), None


def _parse_rel_paths(raw: str | None) -> tuple[list[str] | None, str | None]:
    """Parse a comma-separated list of target-relative POSIX paths."""
    if raw is None:
        return [], None
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item.startswith("/") or ".." in item.split("/") or "\\" in item:
            return None, item
        out.append(item)
    return list(dict.fromkeys(out)), None


def _parse_commands(raw: str | None) -> tuple[list[dict], bool, str | None]:
    """Returns (commands, was_provided, error)."""
    if raw is None:
        return [], False, None
    try:
        data = json.loads(raw)
    except ValueError:
        return [], True, COMMANDS_ERROR
    if not isinstance(data, list):
        return [], True, COMMANDS_ERROR
    out: list[dict] = []
    for item in data:
        if isinstance(item, str):
            out.append({"command": item, "proves": None})
        elif isinstance(item, dict) and isinstance(item.get("command"), str):
            proves = item.get("proves")
            if proves is not None and not isinstance(proves, str):
                return [], True, COMMANDS_ERROR
            out.append({"command": item["command"], "proves": proves})
        else:
            return [], True, COMMANDS_ERROR
    return out, True, None


def _manifest_commands(manifest: dict) -> list[dict] | None:
    """The `commands` a previous run recorded, normalized, or None when the
    manifest carries none (or carries something malformed — never trusted)."""
    raw = manifest.get("commands")
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("command"), str):
            return None
        proves = item.get("proves")
        if proves is not None and not isinstance(proves, str):
            return None
        out.append({"command": item["command"], "proves": proves})
    return out




# --- Network policy (HN-11b) ------------------------------------------------
# `detect_network.py` is advisory: it names the vendors a repo talks to and
# emits the settings fragment that lets a sandboxed session reach them. This
# script is the only writer of that fragment into `.claude/settings.json`, and
# it writes it the same way it writes everything else — through
# merge-settings.py, never by hand. Every field below is optional on the wire:
# an older or newer detector may emit less, or something malformed, and the
# answer is always the same — no fragment, no gaps, carry on.


def _clean_strings(value: object) -> list[str]:
    """The non-empty strings in `value`, deduped, order preserved. Anything
    that is not a list of strings contributes nothing."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in out:
            out.append(item)
    return out


def _network_fragment(domains: list[str], excluded: list[str]) -> dict:
    """The settings fragment a recorded policy reconstructs to: one
    `WebFetch(domain:…)` allow rule per domain (ADR-0004 — an allow rule
    pre-allows sandboxed egress), the sandbox allowlist, the exclusions."""
    fragment: dict = {}
    if domains:
        fragment["permissions"] = {"allow": [f"WebFetch(domain:{d})" for d in domains]}
    sandbox: dict = {}
    if domains:
        sandbox["network"] = {"allowedDomains": list(domains)}
    if excluded:
        sandbox["excludedCommands"] = list(excluded)
    if sandbox:
        fragment["sandbox"] = sandbox
    return fragment


def _normalize_network(obj: object, detected_at: str) -> dict | None:
    """A `detect_network.py` payload read defensively into the internal policy
    shape, or None when there is nothing usable in it.

    The fragment is the detector's own `settings_fragment` when it emitted one
    (that is the contract: the detector decides what to seed); the recorded
    `domains`/`excluded_commands` are read back out of that fragment, so what
    the manifest records and what `verify_harness.py` grades are exactly what
    was merged. A payload with no fragment still contributes its vendors and
    gaps — reported, never applied."""
    if not isinstance(obj, dict):
        return None
    vendors: list[dict] = []
    for item in obj.get("vendors") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        vid = item["id"].strip()
        if not vid or any(v["id"] == vid for v in vendors):
            continue
        confidence = item.get("confidence")
        vendors.append({
            "id": vid,
            "confidence": confidence if isinstance(confidence, str) and confidence.strip() else "unknown",
        })
    gaps: list[dict] = []
    for item in obj.get("gaps") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        gid = item["id"].strip()
        if not gid or any(g["id"] == gid for g in gaps):
            continue
        severity = item.get("severity")
        summary = item.get("summary")
        gaps.append({
            "id": gid,
            "severity": severity if isinstance(severity, str) and severity.strip() else "unknown",
            "summary": summary if isinstance(summary, str) else "",
        })
    fragment = obj.get("settings_fragment")
    fragment = fragment if isinstance(fragment, dict) else {}
    sandbox = fragment.get("sandbox")
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    net = sandbox.get("network")
    net = net if isinstance(net, dict) else {}
    domains = _clean_strings(net.get("allowedDomains"))
    excluded = _clean_strings(sandbox.get("excludedCommands"))
    if not vendors and not gaps and not domains and not excluded:
        return None
    return {
        "vendors": vendors,
        "domains": domains,
        "excluded_commands": excluded,
        "gaps": gaps,
        "detected_at": detected_at,
        "fragment": fragment if (domains or excluded) else {},
    }


def _read_network(path: str, detected_at: str) -> tuple[dict | None, str | None]:
    """(policy, reason). A missing, unreadable, empty or malformed file is
    never a refusal: the reason is reported and the run proceeds with no
    fragment and no gaps."""
    reason = _try_read(path)
    if reason is not None:
        return None, reason
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, str(exc)
    policy = _normalize_network(obj, detected_at)
    if policy is None:
        return None, "no vendors, domains, exclusions or gaps in the payload"
    return policy, None


def _network_from_manifest(manifest: dict) -> dict | None:
    """The `network` block a previous run recorded, read back into the internal
    policy shape — the carry-forward lane. The fragment is reconstructed from
    the recorded domains and exclusions (the manifest records the policy, not
    the detector's whole payload); `detected_at` is kept as recorded, so a
    flagless re-apply does not restamp it."""
    raw = manifest.get("network")
    if not isinstance(raw, dict):
        return None
    vendors: list[dict] = []
    for item in raw.get("vendors") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            confidence = item.get("confidence")
            vendors.append({
                "id": item["id"],
                "confidence": confidence if isinstance(confidence, str) and confidence.strip() else "unknown",
            })
    gaps: list[dict] = []
    for item in raw.get("gaps") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            severity = item.get("severity")
            gaps.append({
                "id": item["id"],
                "severity": severity if isinstance(severity, str) and severity.strip() else "unknown",
                "summary": "",
            })
    domains = _clean_strings(raw.get("domains"))
    excluded = _clean_strings(raw.get("excluded_commands"))
    if not vendors and not gaps and not domains and not excluded:
        return None
    detected_at = raw.get("detected_at")
    return {
        "vendors": vendors,
        "domains": domains,
        "excluded_commands": excluded,
        "gaps": gaps,
        # Kept only when it is the shape the schema accepts: a manifest hand-
        # edited into an unparseable timestamp carries its policy forward, it
        # does not turn the next re-apply into a refusal.
        "detected_at": (
            detected_at
            if isinstance(detected_at, str) and GENERATED_AT_RE.match(detected_at)
            else None
        ),
        "fragment": _network_fragment(domains, excluded),
    }


def _network_plan_block(policy: dict | None) -> dict:
    """The plan's `network` object. Always present, empty when no policy is in
    effect, so a reader never has to distinguish absent from empty."""
    if policy is None:
        return {"vendors": [], "domains_added": [], "excluded_added": [], "gaps": []}
    return {
        "vendors": [v["id"] for v in policy["vendors"]],
        "domains_added": list(policy["domains"]),
        "excluded_added": list(policy["excluded_commands"]),
        "gaps": [
            {"id": g["id"], "severity": g["severity"], "summary": g["summary"]}
            for g in policy["gaps"]
        ],
    }


def _network_record(policy: dict | None) -> dict | None:
    """The manifest's optional top-level `network` key, or None when there is
    no policy to record (a manifest written before HN-11b, or a target with no
    detected vendors, stays exactly as it was)."""
    if policy is None:
        return None
    record = {
        "vendors": [{"id": v["id"], "confidence": v["confidence"]} for v in policy["vendors"]],
        "domains": list(policy["domains"]),
        "excluded_commands": list(policy["excluded_commands"]),
        "gaps": [{"id": g["id"], "severity": g["severity"]} for g in policy["gaps"]],
        "detected_at": policy["detected_at"],
    }
    if record["detected_at"] is None:
        record.pop("detected_at")
    return record


def _network_note(policy: dict | None) -> str:
    """The network clause of a plan/apply summary. Empty when no vendor was
    detected — a repo that talks to nothing says nothing."""
    if policy is None or not policy["vendors"]:
        return ""
    return (
        f", network: {len(policy['vendors'])} vendor(s), "
        f"+{len(policy['domains'])} domain(s), "
        f"+{len(policy['excluded_commands'])} excludedCommand(s), "
        f"{len(policy['gaps'])} gap(s)"
    )


# --- Declared commands (#403) -----------------------------------------------
# `detect_toolchain.py` reads the verification commands a repository actually
# DECLARES — its package.json `scripts` map and its Makefile targets, invoked
# through the runner its lockfile names — and emits the allow fragment they
# imply. This script merges that fragment; it never derives an allow rule
# itself.
#
# WHY IT IS A FRAGMENT AND NOT A TEMPLATE. `templates/target-settings.json`
# used to carry seven `Bash(pnpm run ...)` rules copied from one backend
# monorepo. On acme-core (#403) five of them named nothing that existed, two
# of those being near-misses of the real names (`test:cov` for `test:coverage`,
# `openapi:check` for `swagger:check`) — the expensive kind of wrong, because it
# reads as correct. A per-repo fact belongs in a per-repo reading.
#
# Same defensive contract as the network fragment: an older or newer detector
# may emit less, or something malformed, and the answer is always the same —
# no fragment, no commands, carry on.


def _normalize_toolchain(obj: object, detected_at: str) -> dict | None:
    """A `detect_toolchain.py` payload read into the internal policy shape, or
    None when it carries nothing to seed or report.

    The commands are read back out of the detector's own `settings_fragment`,
    so what the manifest records and what `verify_harness.py` grades are exactly
    what was merged — never a second derivation that could disagree."""
    if not isinstance(obj, dict):
        return None
    cmds = obj.get("commands")
    if not isinstance(cmds, dict):
        return None
    fragment = cmds.get("settings_fragment")
    fragment = fragment if isinstance(fragment, dict) else {}
    perms = fragment.get("permissions")
    perms = perms if isinstance(perms, dict) else {}
    allow = _clean_strings(perms.get("allow"))
    commands = [r[5:-1] for r in allow if r.startswith("Bash(") and r.endswith(")") and len(r) > 6]
    refused: list[dict] = []
    for item in cmds.get("scripts") or []:
        if not isinstance(item, dict) or item.get("verification"):
            continue
        command, why = item.get("command"), item.get("refused")
        if isinstance(command, str) and command.strip():
            refused.append({"command": command, "why": why if isinstance(why, str) else ""})
    runner = cmds.get("runner")
    runner = runner if isinstance(runner, str) else ""
    runner_source = cmds.get("runner_source")
    runner_source = runner_source if isinstance(runner_source, str) else ""
    if not commands and not refused:
        return None
    return {
        "runner": runner,
        "runner_source": runner_source,
        "commands": commands,
        "refused": refused,
        "detected_at": detected_at,
        "fragment": {"permissions": {"allow": [f"Bash({c})" for c in commands]}} if commands else {},
    }


def _read_toolchain(path: str, detected_at: str) -> tuple[dict | None, str | None]:
    """(policy, reason). Never a refusal — the reason is reported and the run
    proceeds with no command fragment, which leaves the allowlist at the
    template's repo-independent baseline. That is the correct fallback: no
    names beats invented names."""
    reason = _try_read(path)
    if reason is not None:
        return None, reason
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, str(exc)
    policy = _normalize_toolchain(obj, detected_at)
    if policy is None:
        return None, "no declared commands in the payload"
    return policy, None


def _toolchain_from_manifest(manifest: dict) -> dict | None:
    """The `allowlist` block a previous run recorded — the carry-forward lane,
    so a `--reconfigure` that omits `--toolchain` does not silently drop the
    target's own commands back to the baseline."""
    raw = manifest.get("allowlist")
    if not isinstance(raw, dict):
        return None
    commands = _clean_strings(raw.get("commands"))
    refused: list[dict] = []
    for item in raw.get("refused") or []:
        if isinstance(item, dict) and isinstance(item.get("command"), str) and item["command"].strip():
            why = item.get("why")
            refused.append({"command": item["command"], "why": why if isinstance(why, str) else ""})
    if not commands and not refused:
        return None
    runner = raw.get("runner")
    runner_source = raw.get("runner_source")
    detected_at = raw.get("detected_at")
    return {
        "runner": runner if isinstance(runner, str) else "",
        "runner_source": runner_source if isinstance(runner_source, str) else "",
        "commands": commands,
        "refused": refused,
        "detected_at": (
            detected_at
            if isinstance(detected_at, str) and GENERATED_AT_RE.match(detected_at)
            else None
        ),
        "fragment": {"permissions": {"allow": [f"Bash({c})" for c in commands]}} if commands else {},
    }


def _toolchain_plan_block(policy: dict | None) -> dict:
    """The plan's `allowlist` object. Always present, empty when no policy is in
    effect, so a reader never distinguishes absent from empty."""
    if policy is None:
        return {"runner": "", "runner_source": "", "commands_added": [], "refused": []}
    return {
        "runner": policy["runner"],
        "runner_source": policy["runner_source"],
        "commands_added": list(policy["commands"]),
        "refused": [dict(r) for r in policy["refused"]],
    }


def _toolchain_record(policy: dict | None) -> dict | None:
    """The manifest's optional top-level `allowlist` key. The refused list is
    recorded too: a person asking "why is `pnpm run swagger:update` not in
    here" gets the answer from the file rather than from a re-run."""
    if policy is None:
        return None
    record = {
        "runner": policy["runner"],
        "runner_source": policy["runner_source"],
        "commands": list(policy["commands"]),
        "refused": [{"command": r["command"], "why": r["why"]} for r in policy["refused"]],
        "detected_at": policy["detected_at"],
    }
    if record["detected_at"] is None:
        record.pop("detected_at")
    return record


def _toolchain_note(policy: dict | None) -> str:
    """The allowlist clause of a plan/apply summary. Empty when the repo
    declared nothing — a repo with no scripts says nothing."""
    if policy is None or not (policy["commands"] or policy["refused"]):
        return ""
    return (
        f", commands: +{len(policy['commands'])} allow rule(s) from "
        f"{policy['runner_source'] or 'the repo'}, "
        f"{len(policy['refused'])} refused"
    )


def _sidecar_path(target_rel: str) -> str:
    if target_rel.startswith(".claude/"):
        rest = target_rel[len(".claude/"):]
    else:
        rest = target_rel
    return f".claude/.harness/new/{rest}"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _split_globs(value: str) -> list[str]:
    """A globs value is one glob, or several separated by commas. A comma
    inside a brace group (`src/**/*.{ts,tsx}`) is part of the glob, never a
    separator (HN-01b: the writer used to split it into two dead patterns)."""
    whole = _strip_quotes(value)
    parts: list[str] = []
    buf = ""
    depth = 0
    for ch in whole:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [g for g in (_strip_quotes(part) for part in parts) if g]


def _normalize_frontmatter(text: str) -> str:
    """Rewrite a rule's `globs:` frontmatter key as a lazy `paths:` block list.

    Returns `text` unchanged when there is no frontmatter, no `globs:` key, or a
    `paths:` key is already present. Every other key and the whole body are kept
    verbatim; only the `globs:` lines are replaced, in place.
    """
    split = _split_frontmatter(text)
    if split is None:
        return text
    fm, rest = split

    globs_idx = None
    for i, line in enumerate(fm):
        match = FM_KEY_RE.match(line)
        if match is None:
            continue
        key = match.group(1)
        if key == "paths":
            return text
        if key == "globs" and globs_idx is None:
            globs_idx = i
    if globs_idx is None:
        return text

    inline = FM_KEY_RE.match(fm[globs_idx]).group(2)
    globs: list[str] = []
    end = globs_idx + 1
    if inline.strip():
        globs = _split_globs(inline)
    else:
        while end < len(fm):
            item = FM_ITEM_RE.match(fm[end])
            if item is None:
                break
            globs.extend(_split_globs(item.group(1)))
            end += 1
    if not globs:
        return text

    block = ["paths:"] + [f'  - "{g}"' for g in dict.fromkeys(globs)]
    open_line = text.split("\n", 1)[0]
    return "\n".join([open_line] + fm[:globs_idx] + block + fm[end:] + rest)


def _rule_payload(source_path: Path) -> tuple[bytes, str]:
    """Return (bytes to write, sha256 of the verbatim source bytes)."""
    raw = source_path.read_bytes()
    pre_sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, pre_sha
    return _normalize_frontmatter(text).encode("utf-8"), pre_sha


def _frontmatter_summary(text: str) -> str:
    """First sentence of the frontmatter `description:` value; '—' when the
    frontmatter has no description, and the always-loaded wording when there is
    no frontmatter at all — one place, so an installed rule and an adopted one
    render the same way in the CLAUDE.md rule map."""
    split = _split_frontmatter(text)
    if split is None:
        return NO_FRONTMATTER
    for line in split[0]:
        if line.startswith("description:"):
            val = line[len("description:"):].strip()
            if not val:
                return "—"
            m = re.search(r"^(.*?[.?!])(\s|$)", val)
            summary = m.group(1) if m else val
            summary = re.sub(r"\s+", " ", summary).strip()
            return summary if summary else "—"
    return "—"


_HTML_COMMENT_LINES_RE = re.compile(r"(?ms)^[ \t]*<!--.*?-->[ \t]*\n")


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _md_cell(text: str) -> str:
    """A string on its way into a Markdown table cell of the committed
    CLAUDE.md. Paths and descriptions come from the target repository, which
    is data, not instructions: control characters (a newline in a filename)
    are dropped and pipes escaped so a cell can never become a new row or
    leave the table (Codex adversarial review, 2026-09-05)."""
    return _CONTROL_CHARS_RE.sub("", text).replace("|", "\\|")


NO_STACK_DIR = "(no stack dir)"  # an adopted rule sitting directly under .claude/rules/
NO_FRONTMATTER = "— (no frontmatter: always loaded)"

_PLACEHOLDER_COMMENT_RE = re.compile(r"<!--\s*\{\{[A-Z_]+\}\}:.*?-->\n?", re.DOTALL)


def _strip_template_comments(text: str) -> str:
    """Drop whole-line HTML comment blocks from a stretch of the CLAUDE.md
    template that lies OUTSIDE the managed block. Those comments address this
    repository — "this is the harness-init greenfield template", "{{OBJECTIVE}}:
    one paragraph…" — and were being rendered verbatim into every greenfield
    target's committed CLAUDE.md, where they read as instructions about a file
    the target never had. They stay in the template, which is where they belong;
    they just do not travel. The managed block is untouched: its markers are
    HTML comments and load-bearing."""
    stripped = _HTML_COMMENT_LINES_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", stripped)


def _marker_line_positions(text: str, marker: str) -> list[int]:
    """Offsets of every line whose stripped content is exactly `marker`."""
    positions: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.strip() == marker:
            positions.append(offset)
        offset += len(line)
    return positions


def _replace_or_append_block(
    existing_text: str, begin: str, end: str, block_with_markers: str
) -> tuple[str, str] | None:
    """Merge `block_with_markers` into `existing_text`. Returns (new_text,
    "merge") or None when the marker configuration is malformed."""
    # A marker counts only as a whole line: prose that quotes
    # `<!-- somnio:harness:begin -->` inside backticks is not a block (HN-09).
    begin_positions = _marker_line_positions(existing_text, begin)
    end_positions = _marker_line_positions(existing_text, end)
    if not begin_positions and not end_positions:
        text = existing_text
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n" + block_with_markers, "merge"
    if len(begin_positions) == 1 and len(end_positions) == 1 and begin_positions[0] < end_positions[0]:
        start = begin_positions[0]
        stop = end_positions[0] + len(end)
        return existing_text[:start] + block_with_markers + existing_text[stop:], "merge"
    return None


def _marker_pair_problem(full: Path, begin: str, end: str) -> str | None:
    """None when `full` reads as UTF-8 and holds exactly one begin/end marker
    pair in order; otherwise the sentence --refresh-hashes records."""
    try:
        text = full.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        return str(exc)
    if _replace_or_append_block(text, begin, end, begin + "\n" + end) is None:
        return "harness markers are malformed"
    begins = _marker_line_positions(text, begin)
    if not begins:
        return "managed block missing"
    return None


# --- Agents: adapter rules and the CLI skills step ---------------------------


def _parse_agents(raw: str) -> tuple[list[str] | None, str | None]:
    """Parse --agents. Unknown ids are a refusal, not a silent drop."""
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item not in AGENT_IDS:
            return None, item
        out.append(item)
    return list(dict.fromkeys(out)), None


def _parse_skill_ids(raw: str | None) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return None, None
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if not re.match(r"^[a-z0-9_]+$", item):
            return None, item
        out.append(item)
    return list(dict.fromkeys(out)), None


def _derive_skills(stacks: list[str]) -> list[str]:
    """The default skill set for a run: each selected stack's catalog pair (in
    stack order), then the core five, then flutter's extra."""
    out: list[str] = []
    for stack in sorted(stacks):
        out.extend(STACK_SKILLS.get(stack, []))
    out.extend(CORE_SKILLS)
    if "flutter" in stacks:
        out.extend(FLUTTER_EXTRA_SKILLS)
    return list(dict.fromkeys(out))


def _probe_somnio() -> tuple[str | None, str | None, str | None]:
    """Probe the CLI binary: `(exe, version, reason)`.

    `reason` is None when the probe succeeded, and otherwise the sentence the
    plan records as a gap. "Not on PATH" and "on PATH but its --version cannot
    be read" are different problems with different fixes, so they are never
    collapsed into one message.
    """
    exe = shutil.which(CLI_BIN)
    if exe is None:
        return None, None, "somnio binary not on PATH"
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return exe, None, f"somnio on PATH ({exe}) but --version failed: {exc}"
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    match = re.search(r"v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", first) if first else None
    if match is None:
        shown = first[:120] if first else "(no output)"
        return exe, None, f"somnio on PATH ({exe}) but --version unparseable: {shown}"
    return exe, match.group(1), None


def _adapter_stack_file(source_root: str, agent: str, stack: str) -> Path | None:
    """The adapter file for (agent, stack) at the pin, or None."""
    spec = ADAPTERS.get(agent)
    if spec is None:
        return None
    stack_dir = Path(source_root) / ADAPTER_SUBPATH / spec["dirname"] / stack
    if not stack_dir.is_dir():
        return None
    if spec["filename"]:
        # Matched by name, never by suffix: windsurf's adapter file is the
        # dotfile `.windsurfrules`, which has no `.md` suffix at all.
        candidate = stack_dir / spec["filename"]
        return candidate if candidate.is_file() else None
    files = sorted(p for p in stack_dir.iterdir() if p.is_file() and p.suffix in (".md", ".mdc"))
    return files[0] if files else None


def _cursor_stack_files(source_root: str, stack: str) -> list[Path]:
    """Every rule file cursor's adapter carries for `stack` at the pin."""
    stack_dir = Path(source_root) / ADAPTER_SUBPATH / ADAPTERS["cursor"]["dirname"] / stack
    if not stack_dir.is_dir():
        return []
    return sorted(p for p in stack_dir.iterdir() if p.is_file() and p.suffix in (".md", ".mdc"))


def _read_cli_manifest(path: Path) -> dict | None:
    obj, reason = _load_json_object(str(path))
    if obj is None or not isinstance(obj.get("skills"), dict):
        return None
    return obj


def _scan_cli_manifests(
    root_path: Path, previous: dict[str, tuple[int, str]] | None = None
) -> dict[str, tuple[int, str]]:
    """Every `.somnio-skills.json` under `root` (bounded depth), path -> stamp.

    EXCLUDED_DIRS are pruned — a vendored node_modules can carry a manifest of
    its own, and it is not this run's.

    The CLI writes one per agent, at the root of that agent's project-scope
    install directory (`.claude/skills/`, `.codex/skills/`, ...). Scanning for
    them keeps this script from having to duplicate the CLI's agent→path table:
    the manifest a call touches is the one whose stamp moved.

    The stamp is (mtime_ns, content digest), not content alone — re-installing
    the same skill set rewrites a byte-identical manifest, and a content-only
    stamp would read that as "this agent installed nothing".

    `previous` is the stamp map from the scan before this one. A file whose
    st_mtime_ns has not moved since then keeps that scan's digest instead of
    being re-read: the stamp already carries mtime_ns, so an unmoved mtime is
    exactly the case where the digest cannot have been recomputed to anything
    else. Attribution is unaffected — the comparison is still stamp vs stamp.
    """
    found: dict[str, tuple[int, str]] = {}
    previous = previous or {}
    root_real = os.path.realpath(str(root_path))
    for dirpath, dirnames, filenames in os.walk(root_real):
        rel = os.path.relpath(dirpath, root_real)
        depth = 0 if rel == "." else len(Path(rel).parts)
        if depth >= 3:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        if CLI_MANIFEST_NAME in filenames:
            full = os.path.join(dirpath, CLI_MANIFEST_NAME)
            try:
                mtime_ns = os.stat(full).st_mtime_ns
                prior = previous.get(full)
                if prior is not None and prior[0] == mtime_ns:
                    digest = prior[1]
                else:
                    digest = hashlib.sha256(Path(full).read_bytes()).hexdigest()
            except OSError:
                continue
            found[full] = (mtime_ns, digest)
    return found


def _cli_entries_from_manifest(root_path: Path, manifest_full: str, agent: str) -> list[dict]:
    """Turn one CLI manifest into `cli_installed` entries.

    A recorded path is relative to one of two roots: `install` — the directory the
    manifest itself sits in — and `rules` — the CLI's sibling `somnio_rules/`
    directory (`p.join(p.dirname(installDir), 'somnio_rules')` in the CLI). Only
    paths that exist on disk are recorded.
    """
    obj = _read_cli_manifest(Path(manifest_full))
    if obj is None:
        return []
    install_dir = Path(manifest_full).parent
    rules_dir = install_dir.parent / "somnio_rules"
    root_real = os.path.realpath(str(root_path))
    manifest_rel = os.path.relpath(manifest_full, root_real).replace(os.sep, "/")

    entries: list[dict] = []
    for skill, spec in sorted(obj["skills"].items()):
        paths = spec.get("paths") if isinstance(spec, dict) else None
        if not isinstance(paths, list):
            continue
        for item in paths:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            base = rules_dir if item.get("root") == "rules" else install_dir
            full = base / item["path"]
            if not full.exists():
                continue
            rel = os.path.relpath(str(full), root_real).replace(os.sep, "/")
            if rel.startswith("../"):
                continue
            entries.append(
                {"agent": agent, "skill": skill, "path": rel, "manifest": manifest_rel}
            )
    return entries


# --- Validation of the two JSON inputs --------------------------------------


def _validate_source(obj: dict) -> tuple[dict | None, str | None]:
    tier = obj.get("tier")
    if tier not in TIER_ENUM:
        return None, f"source: tier must be one of {sorted(TIER_ENUM)}"
    root = obj.get("root")
    if not isinstance(root, str) or not os.path.isdir(root):
        return None, "source: root is not a directory"
    if not os.path.isdir(os.path.join(root, "agent-rules", "rules")):
        return None, f"source: {root}/agent-rules/rules is not a directory"
    pin = obj.get("pin")
    if not isinstance(pin, str) or not HEX_RE.match(pin):
        return None, "source: pin is not a valid hex rev"
    rev = obj.get("rev")
    if rev is None:
        return None, "source: rev is null"
    if not isinstance(rev, str) or not HEX_RE.match(rev):
        return None, "source: rev is not a valid hex rev"
    stacks = obj.get("stacks")
    if not isinstance(stacks, list) or not all(isinstance(s, str) for s in stacks):
        stacks = []
    return {
        "tier": tier,
        "root": os.path.realpath(root),
        "pin": pin,
        "rev": rev,
        "at_pin": bool(obj.get("at_pin", False)),
        "stacks": stacks,
    }, None


def _read_source_json(path: str) -> tuple[dict | None, str | None]:
    try:
        text = sys.stdin.read() if path == "-" else open(path, "r", encoding="utf-8").read()
    except OSError as exc:
        return None, f"source: cannot read {path}: {exc}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"source: invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "source: top level is not a JSON object"
    return data, None


def _validate_plugin_root(plugin_root: str) -> tuple[dict | None, str | None]:
    proot = Path(plugin_root)
    pj_path = proot / ".claude-plugin" / "plugin.json"
    obj, reason = _load_json_object(str(pj_path))
    if obj is None:
        return None, f"plugin-root: {pj_path}: {reason}"
    version = obj.get("version")
    if not isinstance(version, str) or not version:
        return None, f"plugin-root: {pj_path}: missing string 'version'"
    merge_script = proot / "scripts" / "merge-settings.py"
    if not merge_script.is_file():
        return None, f"plugin-root: {merge_script}: not found"
    return {
        "version": version,
        "merge_script": str(merge_script),
        "root_real": os.path.realpath(str(proot)),
    }, None


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install, plan, or uninstall the Somnio harness assets in a target repository.",
    )
    parser.add_argument("--root", required=True, help="target repository root")
    parser.add_argument("--source", default=None, help="resolver JSON path, or '-' for stdin")
    parser.add_argument("--stacks", default=None, help="comma-separated stack names")
    parser.add_argument("--plugin-root", default=None, help="the plugin directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument(
        "--refresh-hashes",
        action="store_true",
        help="re-record the sha256 of every installed[] path whose bytes changed — rules "
        "after accepted optimize-context-rules rewrites (sha256_pre_optimize is kept), "
        "settings.json after a hook install, and every other managed file (CLAUDE.md, "
        ".gitignore, ship-gate.json, adapter files) hashed whole-file exactly as --apply "
        "hashes it; the only input is the manifest",
    )
    parser.add_argument("--objective", default=OBJECTIVE_DEFAULT)
    parser.add_argument("--commands", default=None, help="JSON array of strings or {command, proves} objects")
    parser.add_argument(
        "--network",
        default=None,
        help="path to a detect_network.py JSON payload; its settings_fragment is merged "
             "into .claude/settings.json and its vendors/gaps are recorded",
    )
    parser.add_argument(
        "--toolchain",
        default=None,
        help="path to a detect_toolchain.py JSON payload; the verification commands its "
             "`commands.settings_fragment` names are merged into .claude/settings.json's "
             "allowlist and recorded, and the ones it refused are recorded with the reason",
    )
    parser.add_argument("--accept-drift", action="store_true")
    parser.add_argument(
        "--adopt-existing",
        action="store_true",
        help="a rule file that is already there and that no manifest claims is adopted as "
        "pre_existing (kept, untouched) instead of raising a conflict",
    )
    parser.add_argument(
        "--take",
        default=None,
        help="comma list of target-relative paths whose sidecar replaces the target (--apply)",
    )
    parser.add_argument(
        "--keep",
        default=None,
        help="comma list of target-relative paths whose sidecar is dropped, target kept (--apply)",
    )
    parser.add_argument(
        "--agents",
        default="claude",
        help="comma-separated agent ids that will work in the target repo (default: claude)",
    )
    parser.add_argument(
        "--skills",
        default=None,
        help="comma-separated CLI skill ids, overriding the set derived from --stacks",
    )
    parser.add_argument(
        "--no-cli-skills",
        action="store_true",
        help="plan and write the rules and adapter files, but never call the somnio CLI",
    )
    parser.add_argument(
        "--cli-version-pin",
        default=None,
        help="the harness's somnio CLI pin (read from install.sh by the skill); "
        "recorded in the manifest and used only for the drift warning",
    )
    parser.add_argument("--plan-out", default=None, help="also write the plan JSON to this path")
    parser.add_argument("--settings-template", default=None)
    parser.add_argument("--schema", default=None)
    parser.add_argument("--json", action="store_true", help="suppress the human summary line on stderr")
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    def refuse(msg: str) -> int:
        print(f"error: {msg}", file=sys.stderr)
        return 2

    if not os.path.isdir(args.root):
        return refuse(f"{args.root} is not a directory")
    root_path = Path(args.root)
    root_real = os.path.realpath(args.root)

    mode = (
        "plan" if args.plan else "apply" if args.apply
        else "uninstall" if args.uninstall else "refresh-hashes"
    )

    # ------------------------------------------------------------------ #
    # --refresh-hashes: the manifest is the only input (issue #24). A rule
    # rewritten by optimize-context-rules keeps its provenance; only the
    # as-written hash moves, so check-5 grades the file the person accepted
    # and --uninstall still owns it.
    # ------------------------------------------------------------------ #
    if mode == "refresh-hashes":
        manifest_path = root_path / ".claude" / "harness.json"
        manifest, reason = _load_json_object(str(manifest_path))
        if manifest is None:
            return refuse(f"{manifest_path}: {reason}")
        installed = manifest.get("installed")
        if not isinstance(installed, list):
            return refuse(f"{manifest_path}: 'installed' is missing or not a list")
        refreshed: list[str] = []
        same: list[str] = []
        missing: list[str] = []
        rejected: list[dict] = []
        # The harness's own deny entries: every `permissions.deny` value the
        # settings template ships, or two anchors when no template resolves.
        harness_deny: list[str] = ["Read(**/.env)", "Edit(**/.env)"]
        template_path, _ = _resolve_asset(
            args.settings_template, "target-settings.json", args.plugin_root
        )
        if template_path is not None:
            template_obj, _ = _load_json_object(template_path)
            template_deny = ((template_obj or {}).get("permissions") or {}).get("deny")
            if isinstance(template_deny, list) and template_deny:
                harness_deny = [str(d) for d in template_deny]
        for entry in installed:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", ""))
            if path == SETTINGS_REL:
                # settings.json is re-recorded too (HN-09): the hook install
                # edits it after apply, which left check-5 failing on a file
                # that still carries every harness deny entry. Refreshed only
                # when the bytes moved, it still parses as a JSON object, and
                # none of the harness deny entries was removed.
                full = root_path / path
                actual = _sha256_of(str(full))
                if actual is None:
                    missing.append(path)
                    continue
                if actual.lower() == str(entry.get("sha256", "")).lower():
                    same.append(path)
                    continue
                settings_obj, why = _load_json_object(str(full))
                if settings_obj is None:
                    rejected.append({"path": path, "reason": why})
                    continue
                deny = (settings_obj.get("permissions") or {}).get("deny")
                deny_list = [str(d) for d in deny] if isinstance(deny, list) else []
                absent = [d for d in harness_deny if d not in deny_list]
                if absent:
                    rejected.append(
                        {"path": path, "reason": f"harness deny entries missing: {', '.join(absent[:3])}"}
                    )
                    continue
                entry["sha256"] = actual
                refreshed.append(path)
                continue
            full = root_path / path
            if _outside(str(full), root_real):
                rejected.append({"path": path, "reason": "outside the target root"})
                continue
            actual = _sha256_of(str(full))
            if actual is None:
                missing.append(path)
                continue
            if actual.lower() == str(entry.get("sha256", "")).lower():
                same.append(path)
                continue
            if not path.startswith(".claude/rules/"):
                # Every other installed path (HN-10b): CLAUDE.md, .gitignore, the
                # adapter files, .claude/ship-gate.json. --apply records each of
                # them as the sha256 of the whole file it wrote (a managed-block
                # file is hashed as the merged file, never as the block alone), so
                # the refresh re-hashes the whole file too. Light sanity checks
                # mirror the ones above: a block-managed file must still carry
                # exactly one marker pair, the ship-gate draft must still parse.
                origin = str(entry.get("origin") or "")
                reason_text: str | None = None
                if origin == "template:CLAUDE.md.template" or origin.startswith(ADAPTER_ORIGIN_PREFIX):
                    reason_text = _marker_pair_problem(full, CLAUDE_BEGIN, CLAUDE_END)
                elif origin == "template:gitignore-block":
                    reason_text = _marker_pair_problem(full, GITIGNORE_BEGIN, GITIGNORE_END)
                elif origin == "template:ship-gate-draft":
                    obj, why = _load_json_object(str(full))
                    if obj is None:
                        reason_text = why
                if reason_text is not None:
                    rejected.append({"path": path, "reason": reason_text})
                    continue
                # The bytes moved, so they are no longer provably all ours: the
                # `created` claim is dropped, exactly as --apply's _created_flag
                # drops it when the sha no longer matches, and --uninstall then
                # strips the block instead of deleting the file.
                entry.pop("created", None)
                entry["sha256"] = actual
                refreshed.append(path)
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError) as exc:
                rejected.append({"path": path, "reason": str(exc)})
                continue
            if not _rule_paths(text):
                rejected.append({"path": path, "reason": "no paths: block list in the frontmatter"})
                continue
            entry.setdefault("sha256_pre_optimize", entry.get("sha256", PLACEHOLDER_SHA))
            entry["sha256"] = actual
            refreshed.append(path)
        if refreshed:
            manifest["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        result = {
            "mode": "refresh-hashes",
            "refreshed": refreshed,
            "unchanged": same,
            "missing": missing,
            "rejected": rejected,
        }
        print(json.dumps(result))
        if not args.json:
            print(
                f"refresh-hashes: {len(refreshed)} refreshed, {len(same)} unchanged, "
                f"{len(missing)} missing, {len(rejected)} rejected — manifest {manifest_path}",
                file=sys.stderr,
            )
        return 0

    # ------------------------------------------------------------------ #
    # --uninstall: the manifest is the only input.
    # ------------------------------------------------------------------ #
    if mode == "uninstall":
        manifest_path = root_path / ".claude" / "harness.json"
        manifest, reason = _load_json_object(str(manifest_path))
        if manifest is None:
            return refuse(f"{manifest_path}: {reason}")
        installed = manifest.get("installed")
        if not isinstance(installed, list):
            return refuse(f"{manifest_path}: 'installed' is missing or not a list")

        managed_block_origins = {"template:CLAUDE.md.template", "template:gitignore-block"}
        removed: list[str] = []
        removed_dirs: list[str] = []
        restored: list[str] = []
        skipped: list[dict] = []
        stripped: list[str] = []
        # The .bak is deleted only when THIS harness created it: a person's own
        # backup of their settings is not ours to remove. The manifest records
        # which it was, so an uninstall never has to guess.
        bak_created = manifest.get("settings_bak_created") is True
        settings_entry_present = any(
            isinstance(e, dict) and e.get("path") == SETTINGS_REL for e in installed
        )

        for entry in installed:
            if not isinstance(entry, dict) or "path" not in entry:
                continue
            rel = str(entry["path"])
            full = root_path / rel
            if _outside(str(full), root_real, None, None):
                return refuse(f"refusing to write outside --root: {full}")
            origin = str(entry.get("origin") or "")
            if origin in managed_block_origins or origin.startswith(ADAPTER_ORIGIN_PREFIX):
                if not full.is_file():
                    continue
                begin, end = (
                    (GITIGNORE_BEGIN, GITIGNORE_END)
                    if origin == "template:gitignore-block"
                    else (CLAUDE_BEGIN, CLAUDE_END)
                )
                try:
                    text = full.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                # The run wrote every byte of this file and nobody has touched
                # it since (the sha still matches): it is not the repo's file
                # with our block inside it, it IS our file — a greenfield
                # CLAUDE.md, .gitignore or AGENTS.md — so it goes whole.
                if entry.get("created") is True:
                    actual = _sha256_of(str(full))
                    declared = str(entry.get("sha256", "")).lower()
                    if actual and re.match(r"^[0-9a-f]{64}$", declared) and actual.lower() == declared:
                        full.unlink()
                        removed.append(rel)
                        continue
                begins = [m.start() for m in re.finditer(re.escape(begin), text)]
                ends = [m.start() for m in re.finditer(re.escape(end), text)]
                if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
                    new_text = text[: begins[0]] + text[ends[0] + len(end):]
                    # A block-managed file whose block was its whole content is
                    # a file this run created — an AGENTS.md, but also the
                    # CLAUDE.md or .gitignore a greenfield run wrote. Stripping
                    # those in place leaves an empty file behind as litter, so
                    # they are unlinked instead. A file with any content of its
                    # own left over is the repo's, and is written back.
                    if not new_text.strip():
                        full.unlink()
                        removed.append(rel)
                    else:
                        # The blank line the block was appended after is ours
                        # too: a stripped file ends the way it ended before.
                        full.write_text(new_text.rstrip("\n") + "\n", encoding="utf-8")
                        stripped.append(rel)
                # else: markers no longer form exactly one pair — leave alone.
            else:
                if not (full.is_file() or full.is_symlink()):
                    continue
                # Never delete content the harness did not write: a file whose
                # bytes have moved on since the manifest recorded them is
                # somebody's edit, reported and left alone.
                declared = str(entry.get("sha256", "")).lower()
                actual = _sha256_of(str(full))
                if actual is None:
                    skipped.append({"path": rel, "reason": "unreadable"})
                    continue
                if not re.match(r"^[0-9a-f]{64}$", declared) or actual.lower() != declared:
                    skipped.append({"path": rel, "reason": "content differs from the manifest sha256"})
                    continue
                if rel == SETTINGS_REL:
                    bak_full = root_path / SETTINGS_BAK_REL
                    if bak_full.is_file():
                        _write_bytes(full, bak_full.read_bytes())
                        restored.append(rel)
                        # The backup existed to make this restore possible; once
                        # it has, ours is litter — and the .gitignore line that
                        # hid it is being stripped in the same run.
                        if bak_created:
                            bak_full.unlink()
                            removed.append(SETTINGS_BAK_REL)
                        continue
                full.unlink()
                removed.append(rel)

        # A .bak this run created with nothing left to restore into (the
        # manifest carries no settings entry at all) is removed too.
        bak_full = root_path / SETTINGS_BAK_REL
        if bak_created and not settings_entry_present and bak_full.is_file():
            bak_full.unlink()
            removed.append(SETTINGS_BAK_REL)

        # Directories the run created: a `.claude/rules/<stack>/` left empty by
        # the removals above held nothing but this harness's rules, so it goes
        # too, and `.claude/rules/` with it when that empties. A directory that
        # still holds anything — an adopted pre_existing rule, a person's file —
        # is never touched, which is the whole test: emptiness is the proof of
        # ownership, and `.claude/` itself when nothing else is left in it.
        stack_dirs = sorted(
            {
                rel.rsplit("/", 1)[0]
                for rel in removed
                if rel.startswith(".claude/rules/") and rel.count("/") == 3
            }
        )

        # The manifest goes before the directory sweep, not after: the manifest
        # is the record of what this harness owns, and `.claude/.harness/` is
        # the evidence of the runs that wrote it. If removing the manifest is
        # refused, the run stops with both still on disk — a half-swept target
        # whose evidence had already been deleted would be unauditable.
        try:
            manifest_path.unlink()
        except OSError as exc:
            return refuse(f"cannot remove {manifest_path}: {exc}")

        # `.claude/.harness/` is this harness's own scratch and evidence — the
        # blast radius the plan declares, gitignored by a block that is being
        # stripped in this same run, so leaving it turns invisible litter into
        # visible litter. It goes whole; unconsumed sidecars inside it are
        # proposals from a harness that no longer exists.
        harness_dir = root_path / ".claude" / ".harness"
        if harness_dir.is_dir() and not _outside(str(harness_dir), root_real, None, None):
            try:
                shutil.rmtree(harness_dir)
                removed_dirs.append(".claude/.harness/")
            except OSError:
                pass

        for dir_rel in stack_dirs + [".claude/rules", ".claude"]:
            candidate = root_path / dir_rel
            try:
                if candidate.is_dir() and not any(candidate.iterdir()):
                    candidate.rmdir()
                    removed_dirs.append(dir_rel + "/")
            except OSError:
                continue

        # The somnio CLI's own manifest owns what it installed: those files are
        # listed and left exactly where they are, with the CLI's own removal
        # command as the hint. harness-init never deletes another tool's files.
        cli_left = [
            e for e in (manifest.get("cli_installed") or [])
            if isinstance(e, dict) and isinstance(e.get("path"), str)
        ]

        out = {
            "mode": "uninstall",
            "removed": sorted(removed),
            "removed_dirs": sorted(removed_dirs),
            "restored": sorted(restored),
            "skipped": sorted(skipped, key=lambda s: s["path"]),
            "stripped": sorted(stripped),
            "cli_left": sorted({e["path"] for e in cli_left}),
            "cli_uninstall_hint": CLI_UNINSTALL_HINT,
            "manifest_removed": True,
        }
        print(json.dumps(out))
        if not args.json:
            print(
                f"uninstall: {len(removed)} file(s) removed, "
                f"{len(removed_dirs)} empty dir(s) removed, {len(restored)} restored, "
                f"{len(skipped)} skipped (edited), {len(stripped)} block(s) stripped, "
                f"{len(out['cli_left'])} CLI-installed path(s) left in place "
                f"({CLI_UNINSTALL_HINT}), manifest removed",
                file=sys.stderr,
            )
        return 0

    # ------------------------------------------------------------------ #
    # --plan / --apply
    # ------------------------------------------------------------------ #
    if not args.source:
        return refuse("--source is required for --plan/--apply")
    if args.stacks is None:
        return refuse("--stacks is required for --plan/--apply")
    if not args.plugin_root:
        return refuse("--plugin-root is required for --plan/--apply")

    plugin_info, reason = _validate_plugin_root(args.plugin_root)
    if plugin_info is None:
        return refuse(reason)

    stacks, bad_stack = _parse_stacks(args.stacks)
    if stacks is None:
        return refuse(f"invalid stack name: {bad_stack}")

    agents, bad_agent = _parse_agents(args.agents)
    if agents is None:
        return refuse(f"unknown agent id: {bad_agent} (known: {', '.join(AGENT_IDS)})")
    if not agents:
        return refuse("--agents resolved to an empty list")

    skill_override, bad_skill = _parse_skill_ids(args.skills)
    if args.skills is not None and skill_override is None:
        return refuse(f"invalid skill id: {bad_skill}")

    commands, commands_provided, reason = _parse_commands(args.commands)
    if reason is not None:
        return refuse(reason)

    take_list, bad_path = _parse_rel_paths(args.take)
    if take_list is None:
        return refuse(f"--take is not a target-relative path: {bad_path}")
    keep_list, bad_path = _parse_rel_paths(args.keep)
    if keep_list is None:
        return refuse(f"--keep is not a target-relative path: {bad_path}")
    if (take_list or keep_list) and mode != "apply":
        return refuse("--take/--keep resolve conflicts and are only valid with --apply")
    both = sorted(set(take_list) & set(keep_list))
    if both:
        return refuse(f"--take and --keep name the same path(s): {', '.join(both)}")
    take_set = set(take_list)
    keep_set = set(keep_list)
    resolved_paths: set[str] = set()

    source_obj, reason = _read_source_json(args.source)
    if source_obj is None:
        return refuse(reason)
    source, reason = _validate_source(source_obj)
    if source is None:
        return refuse(reason)
    source_root_real = source["root"]
    plugin_root_real = plugin_info["root_real"]

    settings_template_path, reason = _resolve_asset(
        args.settings_template, "target-settings.json", args.plugin_root
    )
    if settings_template_path is None:
        return refuse(f"settings template {reason}")

    schema_path, reason = _resolve_asset(args.schema, "harness-manifest.schema.json", args.plugin_root)
    if schema_path is None:
        return refuse(f"schema {reason}")
    schema_obj, reason = _load_json_object(schema_path)
    if schema_obj is None:
        return refuse(f"schema {schema_path}: {reason}")
    schema_required = schema_obj.get("required")
    if not isinstance(schema_required, list):
        return refuse(f"schema {schema_path}: no 'required' list")
    schema_properties = schema_obj.get("properties")
    if not isinstance(schema_properties, dict):
        schema_properties = {}

    claude_template_path = Path(__file__).resolve().parents[1] / "assets" / "CLAUDE.md.template"
    reason = _try_read(str(claude_template_path))
    if reason is not None:
        return refuse(f"CLAUDE.md.template {claude_template_path}: {reason}")
    claude_template_text = claude_template_path.read_text(encoding="utf-8")

    # Existing manifest (must parse if present).
    manifest_path = root_path / ".claude" / "harness.json"
    existing_manifest: dict | None = None
    if manifest_path.is_file():
        existing_manifest, reason = _load_json_object(str(manifest_path))
        if existing_manifest is None:
            return refuse(f"{manifest_path}: {reason}")
    manifest_installed_by_path: dict[str, dict] = {}
    if existing_manifest is not None:
        for entry in existing_manifest.get("installed", []) or []:
            if isinstance(entry, dict) and "path" in entry:
                manifest_installed_by_path[str(entry["path"])] = entry
    # Adoption is a property of the target, not of the invocation that made it:
    # once a run recorded a file as `pre_existing`, every later run reads that
    # back and keeps adopting it, with or without `--adopt-existing`. Without
    # this a plain re-apply (or `--reconfigure`) on an adopted brownfield repo
    # would re-raise every adopted rule as a conflict and offer to overwrite
    # what the first run promised never to touch.
    prev_adopted: set[str] = set()
    if existing_manifest is not None:
        prev_adopted = {
            str(p) for p in (existing_manifest.get("pre_existing") or []) if isinstance(p, str)
        }

    # objective / commands carried forward (HN-10b, F-HN-10-2). A re-apply that
    # omits --commands used to render the CLAUDE.md commands table as TODO and
    # empty the ship-gate candidates; the manifest now remembers both, and a
    # flagless run reads them back. An explicit flag always wins.
    objective = args.objective
    carried_forward: list[str] = []
    if existing_manifest is not None:
        if not commands_provided:
            prev_cmds = _manifest_commands(existing_manifest)
            if prev_cmds is not None:
                commands, commands_provided = prev_cmds, True
                carried_forward.append("commands")
        prev_obj = existing_manifest.get("objective")
        if objective == OBJECTIVE_DEFAULT and isinstance(prev_obj, str) and prev_obj.strip():
            objective = prev_obj
            carried_forward.append("objective")

    # Network policy (HN-11b). An explicit, parseable --network wins; anything
    # else falls back to what the manifest recorded, so a flagless re-apply
    # never drops a target's domains back to the template's baseline. A payload
    # that cannot be read is a reported defect, never a refusal — the detector
    # is advisory by contract.
    network_policy: dict | None = None
    network_defect: str | None = None
    if args.network:
        network_policy, network_defect = _read_network(
            args.network, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    prior_review = None
    prior_pr_review = None
    prior_execution = None
    if existing_manifest is not None:
        rev_block = existing_manifest.get("review")
        if isinstance(rev_block, dict) and rev_block:
            prior_review = rev_block
            carried_forward.append("review")
        # The PR reviewer record (ENG-REV-01) is written by record_pr_review.py
        # into this same file, so it needs the same carry-forward as `review`.
        pr_block = existing_manifest.get("pr_review")
        if isinstance(pr_block, dict) and pr_block:
            prior_pr_review = pr_block
            carried_forward.append("pr_review")
        # The execution record (tracker + autonomy gates) is written by
        # record_execution.py into this same file; same carry-forward, same reason.
        ex_block = existing_manifest.get("execution")
        if isinstance(ex_block, dict) and ex_block:
            prior_execution = ex_block
            carried_forward.append("execution")

    if network_policy is None and existing_manifest is not None:
        network_policy = _network_from_manifest(existing_manifest)
        if network_policy is not None:
            carried_forward.append("network")

    # Declared commands (#403). Same three lanes as the network policy: an
    # explicit parseable payload wins, a missing one falls back to what the
    # manifest recorded, and neither leaves the allowlist at the template's
    # repo-independent baseline rather than at a guess.
    toolchain_policy: dict | None = None
    toolchain_defect: str | None = None
    if args.toolchain:
        toolchain_policy, toolchain_defect = _read_toolchain(
            args.toolchain, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    if toolchain_policy is None and existing_manifest is not None:
        toolchain_policy = _toolchain_from_manifest(existing_manifest)
        if toolchain_policy is not None:
            carried_forward.append("allowlist")

    old_rev = None
    if existing_manifest is not None:
        rs = existing_manifest.get("rules_source")
        if isinstance(rs, dict) and isinstance(rs.get("rev"), str):
            old_rev = rs["rev"]
    drift_active = bool(old_rev) and HEX_RE.match(old_rev) and not _rev_matches(old_rev, source["rev"])

    drifted_paths = set()
    drift_report: list[dict] = []
    if drift_active:
        for path in {
            str(e.get("path", ""))
            for e in (existing_manifest.get("installed", []) or [])
            if isinstance(e, dict) and str(e.get("path", "")).startswith(".claude/rules/")
        }:
            drifted_paths.add(path)
        for path in sorted(drifted_paths):
            drift_report.append({"path": path, "manifest_rev": old_rev, "pin": source["pin"]})

    # ---------------- write group (a): rule files ----------------
    planned: list[dict] = []
    # Paths planned as `overwrite` whose new bytes are the bytes already there.
    # They stay `overwrite` in planned[] — that is the contract verify_harness.py
    # grades — and are counted separately so a re-run reads as the no-op it is.
    unchanged: list[str] = []
    # Paths planned as `keep` (HN-10b): a rewrite this harness accepted, kept
    # because the corpus did not move. Counted in `unchanged` too — nothing is
    # written — and named on their own so the C4 card can say why.
    kept: list[str] = []
    conflicts: list[dict] = []
    pre_existing: list[str] = []
    not_in_scope: list[str] = []
    rule_write_specs: list[dict] = []  # kind "rule" (installed) or "sidecar"

    for stack in sorted(stacks):
        stack_dir = Path(source["root"]) / "agent-rules" / "rules" / stack
        if not stack_dir.is_dir():
            not_in_scope.append(f"rules: skipped ({stack} unserved at {source['pin'][:7]})")
            continue

        somnio_manifest_path = root_path / ".claude" / "rules" / stack / ".somnio-manifest"
        somnio_names: set[str] = set()
        if somnio_manifest_path.is_file():
            try:
                for line in somnio_manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    somnio_names.add(os.path.basename(line))
            except OSError:
                pass

        candidate_files = sorted(
            p.name for p in stack_dir.iterdir() if p.is_file() and p.name.endswith(".md")
        )
        for filename in candidate_files:
            target_rel = f".claude/rules/{stack}/{filename}"
            target_full = root_path / ".claude" / "rules" / stack / filename
            source_full = stack_dir / filename
            sidecar_rel = _sidecar_path(target_rel)
            sidecar_full = root_path / sidecar_rel

            if filename in somnio_names and target_full.is_file():
                pre_existing.append(target_rel)
                continue
            if filename in somnio_names:
                # The CLI manifest names a file that is not there: a stale
                # entry, never a reason to skip the corpus rule (Codex, 2026-09-05).
                not_in_scope.append(
                    f"cli manifest: {target_rel} is named by .somnio-manifest but missing; installed from the corpus"
                )

            # A previous run's conflict, being resolved by this one.
            if target_rel in take_set or target_rel in keep_set:
                if not sidecar_full.is_file():
                    return refuse(f"no sidecar to resolve for {target_rel}: {sidecar_rel} is missing")
                resolved_paths.add(target_rel)
                if target_rel in keep_set:
                    pre_existing.append(target_rel)
                    rule_write_specs.append({"kind": "keep", "target": target_rel, "sidecar": sidecar_rel})
                    continue
                payload, pre_sha = _rule_payload(sidecar_full)
                planned.append({"path": target_rel, "action": "overwrite"})
                rule_write_specs.append(
                    {"kind": "rule", "target": target_rel, "payload": payload, "pre_sha": pre_sha,
                     "stack": stack, "filename": filename, "sidecar": sidecar_rel}
                )
                continue

            payload, pre_sha = _rule_payload(source_full)

            if target_full.exists():
                if drift_active and not args.accept_drift and target_rel in drifted_paths:
                    reason_text = f"drift: manifest rev {old_rev} ≠ source rev {source['rev']}"
                    conflicts.append({"path": target_rel, "reason": reason_text})
                    planned.append({"path": sidecar_rel, "action": "create"})
                    rule_write_specs.append(
                        {"kind": "sidecar", "target": sidecar_rel, "payload": payload}
                    )
                    continue

                old_entry = manifest_installed_by_path.get(target_rel)
                existing_sha = _sha256_of(str(target_full))
                manifest_sha = old_entry.get("sha256") if old_entry else None
                if manifest_sha and existing_sha and str(manifest_sha).lower() == existing_sha.lower():
                    payload_sha = hashlib.sha256(payload).hexdigest()
                    if payload_sha == existing_sha.lower():
                        planned.append({"path": target_rel, "action": "overwrite"})
                        unchanged.append(target_rel)
                        rule_write_specs.append(
                            {"kind": "rule", "target": target_rel, "payload": payload, "pre_sha": pre_sha,
                             "stack": stack, "filename": filename}
                        )
                    elif _source_unmoved(old_entry, pre_sha, source["rev"]):
                        # The file is exactly what the manifest recorded, the
                        # corpus would write something else, and the corpus has
                        # not moved: this is a rewrite the harness accepted
                        # (optimize-context-rules + --refresh-hashes), not a
                        # stale copy. Kept as written (F-HN-10-1); the manifest
                        # entry is re-recorded from the bytes on disk.
                        planned.append({"path": target_rel, "action": "keep"})
                        unchanged.append(target_rel)
                        kept.append(target_rel)
                        rule_write_specs.append(
                            {"kind": "rule", "target": target_rel,
                             "payload": target_full.read_bytes(), "pre_sha": pre_sha,
                             "stack": stack, "filename": filename, "keep": True}
                        )
                    else:
                        # The source moved (rev or verbatim bytes): the newer
                        # corpus wins and the optimize pass re-proposes.
                        planned.append({"path": target_rel, "action": "overwrite"})
                        rule_write_specs.append(
                            {"kind": "rule", "target": target_rel, "payload": payload, "pre_sha": pre_sha,
                             "stack": stack, "filename": filename}
                        )
                elif old_entry is None and existing_sha and hashlib.sha256(payload).hexdigest() == existing_sha.lower():
                    # Not in the manifest, but byte-identical to what the corpus
                    # would write: a rule this harness installed whose manifest
                    # entry was lost (reconfigure carry-forward, HN-09). Recorded
                    # as installed with its hash, never raised as a conflict.
                    planned.append({"path": target_rel, "action": "overwrite"})
                    unchanged.append(target_rel)
                    rule_write_specs.append(
                        {"kind": "rule", "target": target_rel, "payload": payload, "pre_sha": pre_sha,
                         "stack": stack, "filename": filename}
                    )
                elif (args.adopt_existing or target_rel in prev_adopted) and old_entry is None:
                    # A brownfield repo that wrote its own rules before any
                    # harness existed: no manifest claims this file, so there is
                    # nothing to reconcile and nothing to ask. It is adopted —
                    # kept byte for byte, recorded as pre_existing — and the
                    # rest of the corpus installs beside it.
                    pre_existing.append(target_rel)
                else:
                    conflicts.append(
                        {"path": target_rel, "reason": "target exists with content the harness did not write"}
                    )
                    planned.append({"path": sidecar_rel, "action": "create"})
                    rule_write_specs.append(
                        {"kind": "sidecar", "target": sidecar_rel, "payload": payload}
                    )
            else:
                planned.append({"path": target_rel, "action": "create"})
                rule_write_specs.append(
                    {"kind": "rule", "target": target_rel, "payload": payload, "pre_sha": pre_sha,
                     "stack": stack, "filename": filename}
                )

    if args.adopt_existing or prev_adopted:
        # The rest of the repo's own rule tree: files the corpus has no opinion
        # about at all (`.claude/rules/python/house-style.md`). They are adopted
        # by the same rule as the same-named ones — kept, recorded, not graded —
        # because the gate row this flag serves is "this repo already wrote its
        # own rules", and half-adopting them would fail check-3 on a file the
        # run was forbidden to touch.
        #
        # Without the flag this walk still runs, but it adopts nothing new: it
        # only re-records what a previous run already put in `pre_existing`, so
        # the manifest a re-apply writes is the manifest it read.
        rules_tree = root_path / ".claude" / "rules"
        planned_rule_targets = {
            spec["target"] for spec in rule_write_specs if spec["kind"] in ("rule", "keep")
        }
        already = set(pre_existing) | planned_rule_targets | set(manifest_installed_by_path)
        if rules_tree.is_dir():
            for dirpath, dirnames, filenames in os.walk(rules_tree):
                dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
                for filename in sorted(filenames):
                    if not filename.endswith(".md"):
                        continue
                    rel = os.path.relpath(
                        os.path.join(dirpath, filename), str(root_path)
                    ).replace(os.sep, "/")
                    if rel in already or rel.startswith(".claude/.harness/"):
                        continue
                    if not args.adopt_existing and rel not in prev_adopted:
                        continue
                    pre_existing.append(rel)
                    already.add(rel)

    unresolved = sorted((take_set | keep_set) - resolved_paths)
    if unresolved:
        return refuse(
            "--take/--keep name path(s) this run does not install: " + ", ".join(unresolved)
        )

    # Created-ness of a block-managed file: True when this run writes the whole
    # file, and carried forward by a later run only while every byte in it is
    # still ours (the target's sha is exactly what the manifest recorded). A
    # person who writes prose outside the managed block takes ownership back,
    # and --uninstall then strips the block instead of deleting the file.
    def _created_flag(target_rel: str, full: Path) -> bool:
        if not full.is_file():
            return True
        prev = manifest_installed_by_path.get(target_rel)
        if not (prev and prev.get("created") is True):
            return False
        actual = _sha256_of(str(full))
        declared = str(prev.get("sha256", "")).lower()
        return bool(actual and re.match(r"^[0-9a-f]{64}$", declared) and actual.lower() == declared)

    created_flags: dict[str, bool] = {}

    # ---------------- write group (a2): agent adapter rule files ----------------
    # One target file per non-claude agent that has an adapter at the pin. Block
    # agents (codex, copilot, windsurf) get the same managed-block treatment
    # CLAUDE.md gets — same markers, same idempotency, same malformed-marker
    # refusal. cursor's adapter is a per-stack file copy. Every other agent is
    # recorded in not_in_scope and still reaches the CLI skills step below.
    pin7 = source["pin"][:7]
    adapter_specs: list[dict] = []  # {"target", "agent", "text", "origin"}
    for agent in agents:
        if agent == "claude":
            continue  # claude's rules are the copied .claude/rules/ tree above
        spec = ADAPTERS.get(agent)
        if spec is None:
            not_in_scope.append(f"rules: no {agent} adapter at {pin7}")
            continue
        if agent == "cursor":
            for stack in _stacks_in_order(stacks):
                files = _cursor_stack_files(source["root"], stack)
                if not files:
                    not_in_scope.append(
                        f"rules: no cursor adapter content for {stack} at {pin7}"
                    )
                    continue
                text = "\n\n".join(
                    p.read_text(encoding="utf-8", errors="replace") for p in files
                )
                adapter_specs.append(
                    {
                        "target": f".cursor/rules/somnio-{stack}.md",
                        "agent": agent,
                        "text": text if text.endswith("\n") else text + "\n",
                        "origin": (
                            f"adapter-copy:cursor:{source['rev']}:"
                            f"{ADAPTER_SUBPATH}/{ADAPTERS['cursor']['dirname']}/{stack}"
                        ),
                        "mechanism": "copy",
                    }
                )
            continue

        sections: list[str] = []
        for stack in _stacks_in_order(stacks):
            adapter_file = _adapter_stack_file(source["root"], agent, stack)
            if adapter_file is None:
                not_in_scope.append(
                    f"rules: no {agent} adapter content for {stack} at {pin7}"
                )
                continue
            sections.append(adapter_file.read_text(encoding="utf-8", errors="replace").strip())
        if not sections:
            continue
        block = CLAUDE_BEGIN + "\n" + "\n\n".join(sections) + "\n" + CLAUDE_END
        adapter_specs.append(
            {
                "target": spec["target"],
                "agent": agent,
                "block": block,
                "origin": (
                    f"{ADAPTER_ORIGIN_PREFIX}{agent}:{source['rev']}:"
                    f"{ADAPTER_SUBPATH}/{spec['dirname']}"
                ),
                "mechanism": "block",
            }
        )

    adapter_writes: list[dict] = []  # {"target", "text", "origin"}
    for spec in adapter_specs:
        target_rel = spec["target"]
        target_full = root_path / target_rel
        exists = target_full.is_file()
        if spec["mechanism"] == "block":
            if exists:
                try:
                    existing_text = target_full.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    return refuse(f"{target_rel}: {exc}")
                merged = _replace_or_append_block(
                    existing_text, CLAUDE_BEGIN, CLAUDE_END, spec["block"]
                )
                if merged is None:
                    return refuse(f"{target_rel} harness markers are malformed")
                new_text, _action = merged
            else:
                new_text = spec["block"] + "\n"
            # `bytes` is the rendered block, not the merged file: it is what
            # the C4 card shows, and a 117 KB adapter block must be visible
            # before a person says yes.
            created_flags[target_rel] = _created_flag(target_rel, target_full)
            planned.append({
                "path": target_rel,
                "action": "merge" if exists else "create",
                "bytes": len(spec["block"].encode("utf-8")),
            })
            adapter_writes.append(
                {"target": target_rel, "text": new_text, "origin": spec["origin"]}
            )
            continue

        # copy mechanism: the rule overwrite matrix, without a sidecar — a file
        # the harness did not write is reported and left alone.
        if exists:
            old_entry = manifest_installed_by_path.get(target_rel)
            existing_sha = _sha256_of(str(target_full))
            manifest_sha = old_entry.get("sha256") if old_entry else None
            if not (manifest_sha and existing_sha and str(manifest_sha).lower() == existing_sha.lower()):
                pre_existing.append(target_rel)
                continue
            planned.append({
                "path": target_rel, "action": "overwrite",
                "bytes": len(spec["text"].encode("utf-8")),
            })
            if hashlib.sha256(spec["text"].encode("utf-8")).hexdigest() == existing_sha.lower():
                unchanged.append(target_rel)
        else:
            planned.append({
                "path": target_rel, "action": "create",
                "bytes": len(spec["text"].encode("utf-8")),
            })
        adapter_writes.append(
            {"target": target_rel, "text": spec["text"], "origin": spec["origin"]}
        )

    # ---------------- write group (b): .claude/settings.json ----------------
    settings_target = root_path / ".claude" / "settings.json"
    settings_exists = settings_target.is_file()
    settings_obj = None
    settings_reason = None
    if settings_exists:
        settings_obj, settings_reason = _load_json_object(str(settings_target))
    if settings_exists and settings_obj is None:
        return refuse(f"{SETTINGS_REL} is not a JSON object: {settings_reason}")
    preserved_before: dict = {}
    if settings_obj is not None:
        for key in PRESERVE_KEYS:
            if key in settings_obj:
                preserved_before[key] = settings_obj[key]
    # The .bak is the pre-harness original: written once, on the run that first
    # takes ownership of settings.json, and never overwritten afterwards.
    bak_exists = (root_path / SETTINGS_BAK_REL).exists()
    write_bak = settings_exists and not bak_exists and SETTINGS_REL not in manifest_installed_by_path
    # Ownership of the .bak, carried across re-applies: the run that wrote it
    # says so, and every later run that still sees it repeats the claim, so a
    # no-op re-apply produces the same manifest and --uninstall still knows
    # whose backup it is deleting.
    prev_bak_created = bool(existing_manifest and existing_manifest.get("settings_bak_created"))
    settings_bak_created = write_bak or (prev_bak_created and bak_exists)
    if write_bak:
        planned.append({"path": SETTINGS_BAK_REL, "action": "create"})
    planned.append({"path": SETTINGS_REL, "action": "merge" if settings_exists else "create"})

    # ---------------- write group (c): CLAUDE.md ----------------
    # The placeholder-describing comments inside the block ("<!-- {{RULES_MAP}}:
    # a Markdown table… -->") address the template's author; substituting into
    # them rendered every table twice, once inside a comment (real-repo UAT of 2026-09-04, finding F-5). Stripped
    # before the block is sliced, so the create path and the merge path agree.
    claude_template_text = _PLACEHOLDER_COMMENT_RE.sub("", claude_template_text)
    if claude_template_text.count(CLAUDE_BEGIN) != 1 or claude_template_text.count(CLAUDE_END) != 1:
        return refuse("CLAUDE.md template: an unterminated placeholder comment swallowed a harness marker")
    claude_begin_idx = claude_template_text.index(CLAUDE_BEGIN)
    claude_end_idx = claude_template_text.index(CLAUDE_END) + len(CLAUDE_END)
    claude_managed_template = claude_template_text[claude_begin_idx:claude_end_idx]

    claude_target = root_path / "CLAUDE.md"
    claude_exists = claude_target.is_file()
    if claude_exists:
        try:
            claude_existing_text = claude_target.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return refuse(f"CLAUDE.md: {exc}")
    else:
        claude_existing_text = ""
    if claude_exists and _replace_or_append_block(
        claude_existing_text, CLAUDE_BEGIN, CLAUDE_END, claude_managed_template
    ) is None:
        return refuse("CLAUDE.md harness markers are malformed")
    created_flags["CLAUDE.md"] = _created_flag("CLAUDE.md", claude_target)
    planned.append({"path": "CLAUDE.md", "action": "merge" if claude_exists else "create"})

    # ---------------- write group (d): .gitignore ----------------
    # Before the block is composed: which of this run's own paths would the
    # target's ignore rules drop (issue #25)? A re-includable one gets a `!`
    # line inside the managed block; one under an excluded parent directory
    # is a named gap, because git never consults a negation below it.
    harness_paths = [s["target"] for s in rule_write_specs if s["kind"] == "rule"]
    harness_paths += [s["target"] for s in adapter_writes]
    harness_paths += [SETTINGS_REL, ".claude/ship-gate.json", ".claude/harness.json"]
    ignored: list[dict] = []
    gitignore_negations: list[str] = []
    gaps_by_pattern: dict[str, list[str]] = {}
    for hit in _check_ignored(root_real, sorted(set(harness_paths))):
        by = f"{hit['source']}:{hit['line']}:{hit['pattern']}"
        lines = _negation_for(hit["path"], hit["pattern"])
        if lines is None:
            ignored.append({"path": hit["path"], "by": by, "action": "gap"})
            gaps_by_pattern.setdefault(by, []).append(hit["path"])
        else:
            ignored.append({"path": hit["path"], "by": by, "action": "negate", "negation": lines})
            gitignore_negations.extend(lines)
    for by, paths in sorted(gaps_by_pattern.items()):
        not_in_scope.append(
            f"gitignore: {len(paths)} path(s) ignored by {by} ({', '.join(paths[:3])}"
            f"{', …' if len(paths) > 3 else ''}); an excluded parent directory cannot be "
            "re-included — unignore it by hand before committing"
        )
    gitignore_target = root_path / ".gitignore"
    gitignore_exists = gitignore_target.is_file()
    # A re-run sees nothing ignored (the previous block's negations already
    # apply) and must not drop them: whatever `!` lines the managed block
    # already carries are kept.
    if gitignore_exists:
        try:
            _prior = gitignore_target.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            _prior = ""
        _b, _e = _prior.find(GITIGNORE_BEGIN), _prior.find(GITIGNORE_END)
        if 0 <= _b < _e:
            for line in _prior[_b:_e].splitlines():
                if line.startswith("!"):
                    gitignore_negations.append(line.strip())
    gitignore_block_text = _gitignore_block(gitignore_negations)
    if gitignore_exists:
        try:
            gitignore_existing_text = gitignore_target.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return refuse(f".gitignore: {exc}")
    else:
        gitignore_existing_text = ""
    if gitignore_exists and _replace_or_append_block(
        gitignore_existing_text, GITIGNORE_BEGIN, GITIGNORE_END, gitignore_block_text
    ) is None:
        return refuse(".gitignore harness markers are malformed")
    created_flags[".gitignore"] = _created_flag(".gitignore", gitignore_target)
    planned.append({"path": ".gitignore", "action": "merge" if gitignore_exists else "create"})

    # ---------------- write group (e): .claude/ship-gate.json ----------------
    ship_gate_rel = ".claude/ship-gate.json"
    ship_gate_target = root_path / ".claude" / "ship-gate.json"
    ship_gate_exists = ship_gate_target.is_file()
    ship_gate_text = json.dumps(
        {
            "version": 1,
            "required": [],
            "_candidates": [c["command"] for c in commands],
            "_note": "drafted by harness-init; the TL commits this file — docs/DEV_HARNESS.md",
        },
        indent=2,
    ) + "\n"
    ship_gate_action: str | None
    if not ship_gate_exists:
        ship_gate_action = "create"
        planned.append({"path": ship_gate_rel, "action": "create"})
    else:
        old_entry = manifest_installed_by_path.get(ship_gate_rel)
        existing_sha = _sha256_of(str(ship_gate_target))
        manifest_sha = old_entry.get("sha256") if old_entry else None
        if manifest_sha and existing_sha and str(manifest_sha).lower() == existing_sha.lower():
            ship_gate_action = "overwrite"
            planned.append({"path": ship_gate_rel, "action": "overwrite"})
            if hashlib.sha256(ship_gate_text.encode("utf-8")).hexdigest() == existing_sha.lower():
                unchanged.append(ship_gate_rel)
        else:
            ship_gate_action = None
            pre_existing.append(ship_gate_rel)

    # ---------------- write group (g): the CLI skills step ----------------
    # Planned, never listed in planned[]: those files are written by the somnio
    # CLI and owned by its own .somnio-skills.json manifest, so they are reported
    # under the manifest's cli_installed[] instead — check-1 grades planned[]
    # paths, and check-6 grades installed[], neither of which owns these.
    cli_enabled = not args.no_cli_skills
    cli_skill_ids = skill_override if skill_override is not None else _derive_skills(stacks)
    cli_exe, cli_binary, cli_probe_reason = _probe_somnio()
    cli_pin = args.cli_version_pin or None
    cli_drift = bool(cli_binary and cli_pin and cli_binary != cli_pin)
    cli_skills_block = {
        "agents": list(agents),
        "skills": list(cli_skill_ids),
        "binary": cli_binary,
        "pin": cli_pin,
        "drift": cli_drift,
        "enabled": cli_enabled,
    }
    for stack in sorted(stacks):
        if stack not in STACK_SKILLS:
            not_in_scope.append(f"cli skills: {stack} has no catalog skill (rules only)")
    if not cli_enabled:
        not_in_scope.append("cli skills: skipped (--no-cli-skills)")
    elif cli_binary is None:
        for agent in agents:
            not_in_scope.append(f"cli skills ({agent}): {cli_probe_reason}")
    elif not cli_skill_ids:
        not_in_scope.append("cli skills: the derived skill set is empty")

    # ---------------- containment gate over every planned path ----------------
    for entry in planned:
        full = root_path / entry["path"]
        if _outside(str(full), root_real, source_root_real, plugin_root_real):
            return refuse(f"refusing to write outside --root: {full}")

    if toolchain_defect is not None:
        not_in_scope.append(f"toolchain: {args.toolchain}: {toolchain_defect}")
    if network_defect is not None:
        # The detector is advisory: an unreadable payload is a named gap, not a
        # stop, and it never blocks the manifest's recorded policy from carrying.
        not_in_scope.append(f"network: {args.network}: {network_defect}")
    conflicts.sort(key=lambda c: c["path"])
    pre_existing.sort()
    not_in_scope.sort()

    # ---------------- manifest shape, graded in the plan phase ----------------
    # Everything but the hashes is knowable before a byte is written, so it is
    # graded here: --apply cannot refuse (exit 2) after its first write.
    allowed_paths = {e["path"] for e in planned} | set(BLAST_RADIUS)

    def build_manifest(entries: list[dict], cli_entries: list[dict] | None = None) -> dict:
        manifest = {
            "schema_version": 1,
            "generated_by": f"somnio-engineering-ai@{plugin_info['version']}",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rules_source": {
                "repo": UPSTREAM_REPO,
                "rev": source["rev"],
                "path": RULES_SUBPATH,
                "tier": source["tier"],
            },
            "stacks": sorted(set(stacks)),
            "installed": entries,
            "pre_existing": sorted(pre_existing),
            "not_in_scope": sorted(not_in_scope),
            "agents": list(agents),
            "settings_bak_created": settings_bak_created,
            "cli_installed": list(cli_entries or []),
        }
        if cli_binary is not None:
            manifest["cli_version"] = cli_binary
        if cli_pin is not None:
            manifest["cli_pin"] = cli_pin
        # Recorded only when they carry information: the default objective
        # sentinel and an omitted --commands leave the keys out, so a manifest
        # written before HN-10b stays byte-identical on a no-op re-apply.
        if objective != OBJECTIVE_DEFAULT:
            manifest["objective"] = objective
        if commands_provided:
            manifest["commands"] = [
                {"command": c["command"], "proves": c["proves"]} for c in commands
            ]
        network_record = _network_record(network_policy)
        if network_record is not None:
            manifest["network"] = network_record
        allowlist_record = _toolchain_record(toolchain_policy)
        if allowlist_record is not None:
            manifest["allowlist"] = allowlist_record
        # ADR-0005 D-1's registration record, and D-13's decline record, are
        # written by setup_roborev.py into this same file. build_manifest
        # rebuilds the manifest from scratch, so a --reconfigure that did not
        # carry the block forward would silently unregister a repository on
        # disk while roborev kept reviewing it -- and would erase the answer
        # check-7 grades. Carried verbatim: nothing here may edit a consent
        # record, only preserve the one a person gave.
        if prior_review is not None:
            manifest["review"] = prior_review
        # Same reason, second record: a re-run that dropped `pr_review` would
        # send every PR watch back to waiting for a bot the person said is not
        # there. Carried verbatim; only record_pr_review.py changes it.
        if prior_pr_review is not None:
            manifest["pr_review"] = prior_pr_review
        if prior_execution is not None:
            manifest["execution"] = prior_execution
        return manifest

    def manifest_entries(sha_of: dict[str, str], pre_of: dict[str, str]) -> list[dict]:
        entries: list[dict] = []
        for spec in rule_write_specs:
            if spec["kind"] != "rule":
                continue
            target = spec["target"]
            entry = {
                "path": target,
                "origin": (
                    f"{UPSTREAM_REPO}:{source['rev']}:"
                    f"{RULES_SUBPATH}/{spec['stack']}/{spec['filename']}"
                ),
                "source_rev": source["rev"],
                "sha256": sha_of.get(target, PLACEHOLDER_SHA),
                "sha256_pre_optimize": pre_of.get(target, PLACEHOLDER_SHA),
            }
            entries.append(entry)
        for spec in adapter_writes:
            entry = {
                "path": spec["target"],
                "origin": spec["origin"],
                "source_rev": source["rev"],
                "sha256": sha_of.get(spec["target"], PLACEHOLDER_SHA),
            }
            if created_flags.get(spec["target"]):
                entry["created"] = True
            entries.append(entry)
        for target, origin in (
            (SETTINGS_REL, "template:target-settings.json"),
            ("CLAUDE.md", "template:CLAUDE.md.template"),
            (".gitignore", "template:gitignore-block"),
        ):
            entry = {
                "path": target,
                "origin": origin,
                "source_rev": source["rev"],
                "sha256": sha_of.get(target, PLACEHOLDER_SHA),
            }
            if created_flags.get(target):
                entry["created"] = True
            entries.append(entry)
        if ship_gate_action is not None:
            entries.append(
                {
                    "path": ship_gate_rel,
                    "origin": "template:ship-gate-draft",
                    "source_rev": source["rev"],
                    "sha256": sha_of.get(ship_gate_rel, PLACEHOLDER_SHA),
                }
            )
        return entries

    validation_error = _validate_manifest(
        build_manifest(manifest_entries({}, {})), schema_required, schema_properties, allowed_paths
    )
    if validation_error is not None:
        return refuse(validation_error)

    unchanged.sort()
    # Which planned rules would never lazy-load here (issue #24): every
    # `paths:` pattern of the file as it will be written matches no tracked
    # file. Reported, never a stop — the optimize step in Step 7 owns the fix.
    ls = _run_git(root_real, "ls-files")
    tracked = [line for line in ls.stdout.splitlines() if line] if ls.returncode == 0 else []
    zero_match: list[dict] = []
    for spec in rule_write_specs:
        if spec["kind"] != "rule":
            continue
        patterns = _rule_paths(spec["payload"].decode("utf-8", errors="replace"))
        if not patterns:
            continue
        regexes = [_glob_to_regex(p) for p in patterns]
        if not any(rx.match(f) for rx in regexes for f in tracked):
            zero_match.append({"path": spec["target"], "patterns": patterns})
    kept.sort()
    plan_obj = {
        "planned": planned,
        "unchanged": unchanged,
        "kept": kept,
        "carried_forward": carried_forward,
        "ignored": ignored,
        "zero_match": zero_match,
        "tracked_files": len(tracked),
        "blast_radius": BLAST_RADIUS,
        "preserve": PRESERVE_KEYS,
        "preserved_before": preserved_before,
        "conflicts": conflicts,
        "pre_existing": pre_existing,
        "drift": drift_report,
        "not_in_scope": not_in_scope,
        "cli_skills": cli_skills_block,
        "network": _network_plan_block(network_policy),
        "allowlist": _toolchain_plan_block(toolchain_policy),
        "mode": mode,
    }

    if args.plan_out:
        plan_out_path = Path(args.plan_out)
        if not plan_out_path.parent.is_dir():
            return refuse(f"--plan-out parent directory does not exist: {plan_out_path.parent}")
        plan_out_path.write_text(json.dumps(plan_obj, indent=2) + "\n", encoding="utf-8")

    if mode == "plan":
        print(json.dumps(plan_obj))
        if not args.json:
            print(
                f"plan: {len(planned)} write(s)"
                f"{_unchanged_note(unchanged, kept)}, "
                f"{len(conflicts)} conflict(s), "
                f"{len(pre_existing)} pre-existing, {len(not_in_scope)} skipped"
                f"{f', {len(ignored)} git-ignored' if ignored else ''}"
                f"{f', {len(zero_match)} rule(s) match no tracked file' if zero_match else ''}"
                f"{_carried_note(carried_forward, commands)}"
                f"{_network_note(network_policy)}{_toolchain_note(toolchain_policy)} — "
                f"source {source['tier']} rev {source['rev'][:7]} at_pin={str(source['at_pin']).lower()}",
                file=sys.stderr,
            )
        return 0

    # ------------------------------------------------------------------ #
    # --apply: compute every payload first, then write.
    # ------------------------------------------------------------------ #

    # (b) settings.json — the merge runs before the first write, so a failing
    # merge is still a "nothing was written" refusal.
    overlay = str(settings_target) if settings_exists else os.devnull

    def _merge(base: str, over: str, stdin_text: str | None = None) -> tuple[str | None, str]:
        """One merge-settings.py call. `-` on either side reads stdin, which is
        how the three-way merge below chains without a temporary file."""
        res = subprocess.run(
            [sys.executable, plugin_info["merge_script"], base, over],
            input=stdin_text, capture_output=True, text=True, check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None, (res.stderr.splitlines() or ["no output"])[0]
        return res.stdout, ""

    # Four documents, one rule, applied three times: the template is the base,
    # the detected network fragment overlays it, the repo's own DECLARED
    # commands overlay that, and the repo's existing settings overlay all of
    # them — so the repo still wins on anything it tuned, and the allowlists
    # come out as the union of all four (HN-11b, extended by #403). No fragment
    # collapses this to the two-way merge it has always been.
    base_text, why = _merge(settings_template_path, os.devnull)
    if base_text is None:
        return refuse(f"merge-settings.py failed: {why}")
    # merge-settings.py reads at most one side from stdin, so each fragment
    # overlays through a file of its own while the accumulated base stays on
    # stdin. The temporary directory is this process's, never the target's.
    fragments = [
        ("network", (network_policy or {}).get("fragment") or {}),
        ("commands", (toolchain_policy or {}).get("fragment") or {}),
    ]
    with tempfile.TemporaryDirectory(prefix="harness-init-fragment.") as frag_dir:
        for name, fragment in fragments:
            if not fragment:
                continue
            frag_path = os.path.join(frag_dir, f"{name}.json")
            try:
                Path(frag_path).write_text(json.dumps(fragment), encoding="utf-8")
            except OSError as exc:
                return refuse(f"could not stage the {name} fragment: {exc}")
            seeded, why = _merge("-", frag_path, base_text)
            if seeded is None:
                return refuse(f"merge-settings.py failed: {why}")
            base_text = seeded
    merged_text, why = _merge("-", overlay, base_text)
    if merged_text is None:
        return refuse(f"merge-settings.py failed: {why}")
    try:
        merged = json.loads(merged_text)
    except ValueError:
        return refuse("merge-settings.py failed: stdout is not valid JSON")
    if not isinstance(merged, dict):
        return refuse("merge-settings.py failed: stdout is not a JSON object")
    # The template documents itself in `_comment*` keys; none of them travel
    # into the target's settings.json.
    for key in [k for k in merged if isinstance(k, str) and k.startswith("_comment")]:
        merged.pop(key, None)
    missing = [k for k in preserved_before if k not in merged]
    if missing:
        return refuse(f"merge dropped preserved key(s): {', '.join(missing)}")
    settings_new_text = json.dumps(merged, indent=2) + "\n"

    # (c) CLAUDE.md
    # Sorted, the way the manifest records `stacks` (F-HN-10-5): the block must
    # not change with the order the flag happened to list them in.
    stacks_text = ", ".join(sorted(set(stacks))) if stacks else "none detected"
    installed_rules = [
        {
            "path": s["target"],
            "stack": s["stack"],
            "loads_when": _frontmatter_summary(s["payload"].decode("utf-8", errors="replace")),
        }
        for s in rule_write_specs
        if s["kind"] == "rule"
    ]
    # Adopted and CLI-owned rules are part of the map too (real-repo UAT of 2026-09-04, finding F-6): a reader
    # of CLAUDE.md sees every rule file that loads, not only the ones this
    # run wrote. They are marked, never re-hashed, never graded.
    adopted_rules = []
    for rel in sorted(set(pre_existing)):
        if not rel.startswith(".claude/rules/"):
            continue
        parts = rel.split("/")
        stack_name = parts[2] if len(parts) > 3 else NO_STACK_DIR
        try:
            adopted_text = (root_path / rel).read_text(encoding="utf-8", errors="replace")
            loads_when = _frontmatter_summary(adopted_text.lstrip("\ufeff"))
        except OSError:
            loads_when = "— (unreadable)"
        adopted_rules.append({
            "path": rel,
            "stack": f"{stack_name} (pre-existing)",
            "loads_when": loads_when,
        })
    map_rows = sorted(installed_rules + adopted_rules, key=lambda r: r["path"])
    if map_rows:
        rules_map_lines = ["| path | stack | loads when |", "|:--|:--|:--|"]
        for r in map_rows:
            rules_map_lines.append(
                f"| {_md_cell(r['path'])} | {_md_cell(r['stack'])} | {_md_cell(r['loads_when'])} |"
            )
        rules_map_text = "\n".join(rules_map_lines)
    else:
        rules_map_text = "_No rules installed._"
    if commands_provided:
        commands_lines = ["| command | what it proves |", "|:--|:--|"]
        for c in commands:
            commands_lines.append(f"| {c['command']} | {c['proves'] or '—'} |")
        commands_text = "\n".join(commands_lines)
    else:
        commands_text = "TODO"

    if not claude_exists:
        # Everything outside the managed block is template scaffolding for this
        # repository, not prose for the target: the comments go, the substitution
        # runs afterwards so an objective that happens to contain "<!--" survives.
        claude_head = _strip_template_comments(claude_template_text[:claude_begin_idx])
        claude_tail = _strip_template_comments(claude_template_text[claude_end_idx:])
        claude_new_text = (
            (claude_head + claude_managed_template + claude_tail)
            .replace("{{OBJECTIVE}}", objective)
            .replace("{{STACKS}}", stacks_text)
            .replace("{{RULES_MAP}}", rules_map_text)
            .replace("{{COMMANDS}}", commands_text)
        )
    else:
        rendered_block = (
            claude_managed_template
            .replace("{{STACKS}}", stacks_text)
            .replace("{{RULES_MAP}}", rules_map_text)
            .replace("{{COMMANDS}}", commands_text)
        )
        merged_result = _replace_or_append_block(claude_existing_text, CLAUDE_BEGIN, CLAUDE_END, rendered_block)
        if merged_result is None:
            return refuse("CLAUDE.md harness markers are malformed")
        claude_new_text, _action = merged_result

    # (d) .gitignore
    if not gitignore_exists:
        gitignore_new_text = gitignore_block_text + "\n"
    else:
        merged_result = _replace_or_append_block(
            gitignore_existing_text, GITIGNORE_BEGIN, GITIGNORE_END, gitignore_block_text
        )
        if merged_result is None:
            return refuse(".gitignore harness markers are malformed")
        gitignore_new_text, _action = merged_result

    # (f) .claude/harness.json — validated against the real hashes, still
    # before the first write.
    sha_of: dict[str, str] = {}
    pre_of: dict[str, str] = {}
    for spec in rule_write_specs:
        if spec["kind"] == "rule":
            sha_of[spec["target"]] = hashlib.sha256(spec["payload"]).hexdigest()
            pre_of[spec["target"]] = spec["pre_sha"]
    for spec in adapter_writes:
        sha_of[spec["target"]] = hashlib.sha256(spec["text"].encode("utf-8")).hexdigest()
    sha_of[SETTINGS_REL] = hashlib.sha256(settings_new_text.encode("utf-8")).hexdigest()
    sha_of["CLAUDE.md"] = hashlib.sha256(claude_new_text.encode("utf-8")).hexdigest()
    sha_of[".gitignore"] = hashlib.sha256(gitignore_new_text.encode("utf-8")).hexdigest()
    if ship_gate_action is not None:
        sha_of[ship_gate_rel] = hashlib.sha256(ship_gate_text.encode("utf-8")).hexdigest()

    installed = manifest_entries(sha_of, pre_of)
    manifest_obj = build_manifest(installed)
    validation_error = _validate_manifest(manifest_obj, schema_required, schema_properties, allowed_paths)
    if validation_error is not None:
        return refuse(validation_error)

    manifest_target = root_path / ".claude" / "harness.json"
    if _outside(str(manifest_target), root_real, source_root_real, plugin_root_real):
        return refuse(f"refusing to write outside --root: {manifest_target}")

    # ---------------- writes ----------------
    for spec in rule_write_specs:
        if spec["kind"] == "keep" or spec.get("keep"):
            continue
        _write_bytes(root_path / spec["target"], spec["payload"])
    # A resolved conflict's sidecar is consumed: it exists to be decided once.
    for spec in rule_write_specs:
        sidecar = spec.get("sidecar")
        if sidecar:
            sidecar_full = root_path / sidecar
            if sidecar_full.is_file():
                sidecar_full.unlink()

    for spec in adapter_writes:
        _write_text(root_path / spec["target"], spec["text"])

    if write_bak:
        _write_bytes(root_path / SETTINGS_BAK_REL, settings_target.read_bytes())
    _write_text(settings_target, settings_new_text)
    _write_text(claude_target, claude_new_text)
    _write_text(gitignore_target, gitignore_new_text)
    if ship_gate_action is not None:
        _write_text(ship_gate_target, ship_gate_text)

    # ---------------- the CLI skills step ----------------
    # One call per agent (`--agent` is single-valued), always `--project` and
    # always with cwd = the target root, so the CLI writes nowhere else. This is
    # `skills install`, never `rules install`: it copies catalog skills and
    # commands and regenerates no adapter (ADR-0003 D-1/D-2 hold). Its own
    # .somnio-skills.json stays the owner of what it wrote; harness-init only
    # records the paths. A failing agent is a recorded gap, never a failed run:
    # the exit code stays the plan's (0, or 1 for rule conflicts), because 0/1/2
    # already mean success / conflicts / nothing-written and a fourth meaning
    # would break that contract.
    cli_installed: list[dict] = []
    if cli_enabled and cli_binary is not None and cli_skill_ids:
        cli_env = dict(os.environ)
        cli_env["GIT_TERMINAL_PROMPT"] = "0"
        seen_manifests = _scan_cli_manifests(root_path)
        for agent in agents:
            cmd = [
                cli_exe, "skills", "install", "--agent", agent, "--project",
                "--skills", ",".join(cli_skill_ids),
            ]
            failure: str | None = None
            try:
                proc = subprocess.run(
                    cmd, cwd=root_real, env=cli_env, capture_output=True,
                    text=True, check=False, timeout=CLI_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                failure = f"timed out after {CLI_TIMEOUT_S}s"
            except OSError as exc:
                failure = str(exc)
            else:
                if proc.returncode != 0:
                    first = (
                        (proc.stderr or proc.stdout or "").strip().splitlines() or ["no output"]
                    )[-1]
                    failure = f"exit {proc.returncode}: {first[:160]}"
            # The rescan happens whether the call succeeded or not. A call that
            # timed out or exited nonzero can still have written a manifest, and
            # those bytes belong to THIS agent: attributing them here is what
            # keeps the next agent from inheriting them.
            after = _scan_cli_manifests(root_path, seen_manifests)
            touched = [p for p, text in after.items() if seen_manifests.get(p) != text]
            for manifest_full in sorted(touched):
                cli_installed.extend(
                    _cli_entries_from_manifest(root_path, manifest_full, agent)
                )
            seen_manifests = after
            if failure is not None:
                not_in_scope.append(f"cli skills ({agent}): {failure}")
                continue
            if not touched:
                not_in_scope.append(
                    f"cli skills ({agent}): the CLI reported success but wrote no manifest"
                )

    not_in_scope.sort()
    plan_obj["not_in_scope"] = not_in_scope
    # --plan-out is rewritten here, not only before the CLI step: the gaps the
    # step records are exactly what the verifier's check-9 grades, and a
    # plan file written before the loop would carry none of them.
    if args.plan_out:
        Path(args.plan_out).write_text(json.dumps(plan_obj, indent=2) + "\n", encoding="utf-8")
    manifest_obj = build_manifest(installed, cli_installed)
    # A re-apply that changed nothing must leave the manifest byte-identical:
    # a fresh generated_at is the only difference a no-op run would otherwise
    # produce, and a provenance record that churns on every run cannot be read
    # as "this is when the harness last changed".
    if existing_manifest is not None:
        prev_stamp = existing_manifest.get("generated_at")
        same = {k: v for k, v in manifest_obj.items() if k != "generated_at"} == {
            k: v for k, v in existing_manifest.items() if k != "generated_at"
        }
        if same and isinstance(prev_stamp, str) and GENERATED_AT_RE.match(prev_stamp):
            manifest_obj["generated_at"] = prev_stamp
    _write_text(manifest_target, json.dumps(manifest_obj, indent=2) + "\n")

    print(json.dumps(plan_obj))
    if not args.json:
        print(
            f"apply: {len(installed) - len(kept)} written"
            f"{f', {len(kept)} kept (accepted rewrites)' if kept else ''}, "
            f"{len(cli_installed)} CLI path(s) recorded, "
            f"{len(conflicts)} conflict(s), {len(pre_existing)} pre-existing, "
            f"{len(not_in_scope)} skipped"
            f"{_carried_note(carried_forward, commands)}"
            f"{_network_note(network_policy)}{_toolchain_note(toolchain_policy)} — manifest .claude/harness.json",
            file=sys.stderr,
        )
    return 1 if conflicts else 0


def _check_network_block(block: object) -> str | None:
    """The optional `network` key's shape. Absent is valid; present and
    malformed is a refusal, like every other manifest key."""
    if not isinstance(block, dict):
        return "manifest: network must be an object when present"
    extra = set(block) - {"vendors", "domains", "excluded_commands", "gaps", "detected_at"}
    if extra:
        return f"manifest: network has key(s) outside the schema: {', '.join(sorted(extra))}"
    for key, inner in (("vendors", ("id", "confidence")), ("gaps", ("id", "severity"))):
        items = block.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            return f"manifest: network.{key} must be a list when present"
        for item in items:
            if not isinstance(item, dict) or set(item) != set(inner):
                return f"manifest: each network.{key} entry must have exactly {', '.join(inner)}"
            if any(not isinstance(item[k], str) or not item[k].strip() for k in inner):
                return f"manifest: network.{key} entries must carry non-empty strings"
    for key in ("domains", "excluded_commands"):
        values = block.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or any(
            not isinstance(v, str) or not v.strip() for v in values
        ):
            return f"manifest: network.{key} must be a list of non-empty strings when present"
    detected_at = block.get("detected_at")
    if detected_at is not None and (
        not isinstance(detected_at, str) or not GENERATED_AT_RE.match(detected_at)
    ):
        return "manifest: network.detected_at is not RFC 3339 UTC"
    return None


def _check_allowlist_block(block: object) -> str | None:
    """The optional `allowlist` key's shape (#403). Absent is valid; present
    and malformed is a refusal, like every other manifest key."""
    if not isinstance(block, dict):
        return "manifest: allowlist must be an object when present"
    extra = set(block) - {"runner", "runner_source", "commands", "refused", "detected_at"}
    if extra:
        return f"manifest: allowlist has key(s) outside the schema: {', '.join(sorted(extra))}"
    for key in ("runner", "runner_source"):
        value = block.get(key)
        if value is not None and not isinstance(value, str):
            return f"manifest: allowlist.{key} must be a string when present"
    commands = block.get("commands")
    if commands is not None and (
        not isinstance(commands, list)
        or any(not isinstance(c, str) or not c.strip() for c in commands)
    ):
        return "manifest: allowlist.commands must be a list of non-empty strings when present"
    refused = block.get("refused")
    if refused is not None:
        if not isinstance(refused, list):
            return "manifest: allowlist.refused must be a list when present"
        for item in refused:
            if not isinstance(item, dict) or set(item) != {"command", "why"}:
                return "manifest: each allowlist.refused entry must have exactly command, why"
            if not isinstance(item["command"], str) or not item["command"].strip():
                return "manifest: allowlist.refused entries must carry a non-empty command"
            if not isinstance(item["why"], str):
                return "manifest: allowlist.refused entries must carry a string why"
    detected_at = block.get("detected_at")
    if detected_at is not None and (
        not isinstance(detected_at, str) or not GENERATED_AT_RE.match(detected_at)
    ):
        return "manifest: allowlist.detected_at is not RFC 3339 UTC"
    return None


def _validate_manifest(
    manifest: dict, schema_required: list, schema_properties: dict, allowed_paths: set[str]
) -> str | None:
    shape_error = _check_manifest_shape(manifest, schema_required)
    if shape_error is not None:
        return shape_error
    extra_top = [k for k in manifest if schema_properties and k not in schema_properties]
    if extra_top:
        return f"manifest has key(s) outside the schema: {', '.join(extra_top)}"

    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        return "manifest: schema_version must be an int >= 1"
    generated_by = manifest.get("generated_by")
    if not isinstance(generated_by, str) or not GENERATED_BY_RE.match(generated_by):
        return "manifest: generated_by does not match ^somnio-engineering-ai@.+$"
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not GENERATED_AT_RE.match(generated_at):
        return "manifest: generated_at is not RFC 3339 UTC"

    rules_source = manifest.get("rules_source")
    if not isinstance(rules_source, dict) or set(rules_source) != {"repo", "rev", "path", "tier"}:
        return "manifest: rules_source must have exactly repo, rev, path, tier"
    if not HEX_RE.match(str(rules_source.get("rev", ""))):
        return "manifest: rules_source.rev is not a valid hex rev"
    if rules_source.get("tier") not in TIER_ENUM:
        return "manifest: rules_source.tier is not in the enum"

    if "objective" in manifest and (
        not isinstance(manifest["objective"], str) or not manifest["objective"].strip()
    ):
        return "manifest: objective must be a non-empty string when present"
    if "commands" in manifest and _manifest_commands(manifest) is None:
        return "manifest: commands must be a list of {command, proves} objects when present"
    if "network" in manifest:
        network_error = _check_network_block(manifest["network"])
        if network_error is not None:
            return network_error
    if "allowlist" in manifest:
        allowlist_error = _check_allowlist_block(manifest["allowlist"])
        if allowlist_error is not None:
            return allowlist_error

    installed = manifest.get("installed")
    if not isinstance(installed, list):
        return "manifest: installed must be a list"
    seen_paths: set[str] = set()
    required_entry_keys = {"path", "origin", "source_rev", "sha256"}
    optional_entry_keys = {"sha256_pre_optimize", "created"}
    for entry in installed:
        if not isinstance(entry, dict) or not required_entry_keys <= set(entry) or (
            set(entry) - required_entry_keys - optional_entry_keys
        ):
            return (
                "manifest: an installed[] entry must have path, origin, source_rev, sha256 "
                "and at most sha256_pre_optimize"
            )
        created = entry.get("created")
        if created is not None and created is not True:
            return f"manifest: installed[].created must be true when present, for {entry['path']!r}"
        pre = entry.get("sha256_pre_optimize")
        if pre is not None and (not isinstance(pre, str) or not re.match(r"^[0-9a-f]{64}$", pre)):
            return f"manifest: installed[].sha256_pre_optimize is malformed for {entry['path']!r}"
        path = entry["path"]
        if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
            return f"manifest: installed[].path is not POSIX-relative: {path!r}"
        if not isinstance(entry["sha256"], str) or not re.match(r"^[0-9a-f]{64}$", entry["sha256"]):
            return f"manifest: installed[].sha256 is malformed for {path!r}"
        if not isinstance(entry["source_rev"], str) or not HEX_RE.match(entry["source_rev"]):
            return f"manifest: installed[].source_rev is malformed for {path!r}"
        if path in seen_paths:
            return f"manifest: installed[].path is duplicated: {path!r}"
        seen_paths.add(path)
        if path not in allowed_paths:
            return f"manifest: installed[].path outside planned ∪ blast_radius: {path!r}"
    return None


if __name__ == "__main__":
    sys.exit(main())
