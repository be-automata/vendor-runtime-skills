#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Verification gate for /harness-init — grades the repository, not the plan.

Read-mostly, single-artifact-write: runs fourteen named checks against a target
repository that a harness install already wrote into, re-reading the write
plan, the manifest, the rules, settings.json, an optional context-rules audit
report, and the working-tree status, then prints a PASS/DEGRADED verdict and
writes exactly one file — a timestamped evidence record under
<root>/.claude/.harness/. Nothing else under --root is ever written.

Usage:
    uv run verify_harness.py --root <target repo> --plan <write-plan.json>
        [--audit-report <path>] [--schema <path>] [--plugin-root <dir>]
        [--evidence-dir <dir>] [--json]

The manifest schema is resolved by the same ``_common.py`` helper
``install_assets.py`` calls: an explicit ``--schema`` wins, else ``templates/``
three levels above this script (the repository layout), else
``<plugin-root>/templates/``.

Check 10 grades the recorded network policy (HN-11b): every domain the
manifest's ``network`` block records must still be in
``.claude/settings.json``'s ``sandbox.network.allowedDomains``, and every
recorded exclusion in ``sandbox.excludedCommands``. It skips when no policy was
recorded, and names the settings path and the missing values when one is gone.
Gaps are never graded — they are recorded precisely because nothing applied
them.

Check 11 grades the PR reviewer record (ENG-REV-01): which reviewers this
repository's PRs wait for must be on the record as the manifest's ``pr_review``
block -- ``automata`` (+ collaborators), ``automata_only`` (the bot's verdict,
collaborators' reviews reported and not worked), ``collaborators`` or ``none``.
Any answer passes; silence fails, the D-13
shape check-7 already applies to the roborev decision. It skips under
``--rules-only``, which runs no onboarding question.

Checks 12-14 grade one root cause (#403): `harness-init` templating what it
should have read, and leaving the non-Claude agent config to the session
model's improvisation. Check 12 grades every script-shaped `Bash(...)` allow
rule in `.claude/settings.json` against the script names the repository really
declares — from the repository, never from the manifest, because a target
harnessed before this existed has no record and is exactly the one that needs
grading. Check 13 refuses a machine-local absolute path in any harness-owned
config a hook executes (prose is not graded: an absolute path in documentation
can be a server's real layout). Check 14 WARNS — never fails — when
harness-owned content cites a harness path the repository does not have: some
of that content is vendored verbatim from the pinned corpus (ADR-0003) which
this area consumes read-only, so a rule written for a Firebase Functions repo
citing `.claude/rules/functions/architecture.md` is a NAMED GAP, not a defect
to be silently rewritten.

Check 9 grades the CLI skills step: every path the manifest's ``cli_installed``
records must exist and must be listed by the somnio CLI's own
``.somnio-skills.json`` — the CLI's manifest, not harness-init's claim, is the
authority. It skips when the step was disabled or the binary was absent, and
fails when an enabled step with a binary present recorded nothing.

stdout: on PASS/DEGRADED, without --json: one line per check
        ("check-N  <status>  <detail>") then a verdict line. With --json: a
        single JSON object (the evidence payload) and nothing else. On
        cannot-verify (exit 2): nothing.
stderr: with --json, the verdict line mirrors here so a piping caller still
        sees the outcome. On exit 2, exactly one line beginning "error:".
exit:   0 PASS · 1 DEGRADED · 2 cannot verify (no evidence file written)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# _common.py ships beside this script and owns every helper this grader shares
# with the writer, install_assets.py — the frontmatter splitter above all: the
# grader must apply the delimiter rule the writer applied, not a copy of it.
# Importing a sibling module byte-compiles it unless this is set: a plugin
# cache (or a rules source) is read-only by contract, and a __pycache__/ dir
# appearing beside these scripts is a write nobody asked for. The env var
# PYTHONDONTWRITEBYTECODE=1 on every SKILL.md invocation covers the
# interpreter; this flag covers the import below when it is not set.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    classify_execution,
    classify_pr_review,
    classify_review,
    GIT_MISSING_MSG,
    GIT_MISSING_RC,
    _check_manifest_shape,
    _load_json_object,
    _outside,
    _resolve_asset,
    _run_git,
    _sha256_of,
    _split_frontmatter,
    _summarize,
)
# check-11 grades the target's allowlist against the repository's real script
# names, and it must grade against the SAME reading that seeded them (#403).
# One definition, two callers — a second copy here would be a second answer.
from detect_toolchain import declared_commands as _declared_commands  # noqa: E402

# Preserve-key default when the write plan omits "preserve". Extend by PR.
DEFAULT_PRESERVE_KEYS = ["extraKnownMarketplaces", "enabledPlugins"]

CHECK_IDS = [f"check-{n}" for n in range(1, 15)]

_STALE_GLOB_MENTION_RE = re.compile(r"stale.{0,3}glob", re.IGNORECASE)
_NO_STALE_GLOB_RE = re.compile(r"no\s+stale.{0,3}glob", re.IGNORECASE)


MANIFEST_SCHEMA_NAME = "harness-manifest.schema.json"


def _norm_path(path: str) -> str:
    if path.startswith("./"):
        path = path[2:]
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return path


def _check_1(root: str, plan: dict) -> dict:
    planned = plan.get("planned", [])
    missing: list[str] = []
    for entry in planned:
        path = entry.get("path", "")
        full = os.path.join(root, path)
        ok = False
        try:
            if os.path.isdir(full):
                os.listdir(full)
                ok = True
            else:
                with open(full, "rb") as f:
                    f.read(1)
                ok = True
        except OSError:
            ok = False
        if not ok:
            missing.append(path)

    if missing:
        return {
            "id": "check-1",
            "status": "fail",
            "detail": f"missing/unreadable: {_summarize(missing, ', ')} "
            f"({len(missing)} of {len(planned)})",
        }
    return {
        "id": "check-1",
        "status": "pass",
        "detail": f"{len(planned)} planned path(s) present and readable",
    }


def _check_2(root: str, plan: dict) -> dict:
    settings_path = os.path.join(root, ".claude", "settings.json")
    settings, reason = _load_json_object(settings_path)
    if settings is None:
        return {"id": "check-2", "status": "fail", "detail": f"settings.json: {reason}"}

    deny = (settings.get("permissions") or {}).get("deny")
    if not isinstance(deny, list) or len(deny) == 0:
        return {
            "id": "check-2",
            "status": "fail",
            "detail": "permissions.deny is absent, not a list, or empty",
        }

    preserve = plan.get("preserve")
    if not isinstance(preserve, list):
        preserve = DEFAULT_PRESERVE_KEYS
    preserved_before = plan.get("preserved_before")
    if not isinstance(preserved_before, dict):
        preserved_before = None

    missing_keys: list[str] = []
    present_keys: list[str] = []
    for key in preserve:
        if preserved_before is not None and key in preserved_before:
            if key in settings:
                present_keys.append(key)
            else:
                missing_keys.append(key)
        else:
            if key in settings:
                present_keys.append(key)

    if missing_keys:
        return {
            "id": "check-2",
            "status": "fail",
            "detail": f"deny list has {len(deny)} entr(ies); "
            f"preserved key(s) missing: {', '.join(missing_keys)}",
        }
    return {
        "id": "check-2",
        "status": "pass",
        "detail": f"deny list has {len(deny)} entr(ies); "
        f"preserved: {', '.join(present_keys) if present_keys else 'none'}",
    }


def _frontmatter_has_lazy_paths(fm_lines: list[str]) -> tuple[bool, str]:
    """Returns (ok, reason). ok is False if globs: present or paths: absent/not-list."""
    has_globs = False
    paths_idx = None
    for i, line in enumerate(fm_lines):
        if line.startswith("globs:") or re.match(r"^globs\s*:", line):
            has_globs = True
        if paths_idx is None and (line.startswith("paths:") or re.match(r"^paths\s*:", line)):
            paths_idx = i

    if has_globs:
        return False, "globs: key present (eager anti-pattern)"

    if paths_idx is None:
        return False, "no paths: key"

    rest = fm_lines[paths_idx].split(":", 1)[1].strip()
    if rest.startswith("["):
        return True, "inline list"

    if rest == "":
        for j in range(paths_idx + 1, len(fm_lines)):
            line = fm_lines[j]
            if line.strip() == "":
                continue
            if re.match(r"^\s*-\s", line):
                return True, "block list"
            return False, "paths: value is not a list"
        return False, "paths: has no items"

    return False, "paths: value is not a list"


def _check_3(root: str, manifest: dict) -> dict:
    """Every rule file under .claude/rules/ lazy-loads — except the ones the run
    adopted. A rule the manifest lists under ``pre_existing`` is the
    repository's own file, kept byte for byte on purpose (``--adopt-existing``);
    grading it here would fail a brownfield install for a file the run was
    forbidden to rewrite. Those files are counted and named, never graded —
    ``optimize-context-rules`` in Step 7 is what offers to fix them.

    The exemption is a manifest claim, and this check does not trust it blindly:
    a true pre-run existence signal does not exist post-hoc — nothing on disk
    can prove a file was already there before the run — so the bound is
    structural instead. A path cannot be both adopted and installed: the harness
    either wrote those bytes or promised not to touch them. An overlap between
    ``pre_existing`` and ``installed[]`` is therefore a manifest that has been
    edited to launder a rule past this check, and it fails here. What the
    exemption still cannot verify is a path claimed as adopted that appears in
    neither list — that one is taken at its word, by construction."""
    adopted = {
        str(p) for p in (manifest.get("pre_existing") or [])
        if isinstance(p, str) and p.startswith(".claude/rules/")
    }
    installed_paths = {
        str(e["path"]) for e in (manifest.get("installed") or [])
        if isinstance(e, dict) and isinstance(e.get("path"), str)
    }
    overlap = sorted(adopted & installed_paths)
    if overlap:
        return {
            "id": "check-3",
            "status": "fail",
            "detail": "pre_existing and installed[] overlap: " + ", ".join(overlap),
        }
    rules_dir = os.path.join(root, ".claude", "rules")
    if not os.path.isdir(rules_dir):
        return {
            "id": "check-3",
            "status": "pass",
            "detail": "no .claude/rules directory — 0 rule files examined",
        }

    failures: list[str] = []
    examined = 0
    adopted_seen = 0
    for dirpath, _dirnames, filenames in sorted(os.walk(rules_dir)):
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in adopted and rel not in installed_paths:
                adopted_seen += 1
                continue
            examined += 1
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as exc:
                failures.append(f"{rel}: cannot read ({exc})")
                continue

            split = _split_frontmatter(text)
            if split is None:
                failures.append(f"{rel}: no frontmatter")
                continue
            ok, reason = _frontmatter_has_lazy_paths(split[0])
            if not ok:
                failures.append(f"{rel}: {reason}")

    if failures:
        return {
            "id": "check-3",
            "status": "fail",
            "detail": _summarize(failures),
        }
    detail = f"{examined} rule file(s) scoped with a paths: list"
    if adopted_seen:
        detail += f"; {adopted_seen} adopted rule file(s) not graded"
    return {"id": "check-3", "status": "pass", "detail": detail}


def _check_4(audit_report_path: str | None) -> dict:
    if audit_report_path is None:
        return {"id": "check-4", "status": "skip", "detail": "no audit report supplied"}

    try:
        with open(audit_report_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "id": "check-4",
            "status": "fail",
            "detail": f"cannot read {audit_report_path}: {exc}",
        }

    deciding_line = None
    extracted: list[int] = []
    no_findings_sentence = False

    for line in text.splitlines():
        if _NO_STALE_GLOB_RE.search(line):
            no_findings_sentence = True
            if deciding_line is None:
                deciding_line = line

        for match in _STALE_GLOB_MENTION_RE.finditer(line):
            value = None
            before_seg = line[: match.start()]
            before_match = re.search(r"(\d+)\s+$", before_seg)
            if before_match:
                value = int(before_match.group(1))
            else:
                after_seg = line[match.end():]
                after_match = re.match(r"[^0-9\n]{0,20}(\d+)", after_seg)
                if after_match:
                    value = int(after_match.group(1))
            if value is not None:
                extracted.append(value)
                if deciding_line is None:
                    deciding_line = line

    failed = any(v != 0 for v in extracted)
    passed = (bool(extracted) and all(v == 0 for v in extracted)) or (
        no_findings_sentence and not extracted
    )

    detail_line = deciding_line or "no stale-glob mention found"
    detail_line = detail_line.strip()[:200]

    if failed:
        return {"id": "check-4", "status": "fail", "detail": detail_line}
    if passed:
        return {"id": "check-4", "status": "pass", "detail": detail_line}
    return {
        "id": "check-4",
        "status": "fail",
        "detail": "no stale-glob mention with an adjacent count, and no "
        "'no stale-glob findings' sentence",
    }


def _check_5(root: str, manifest: dict, schema_path: str) -> dict:
    schema, schema_reason = _load_json_object(schema_path)
    if schema is None:
        return {
            "id": "check-5",
            "status": "fail",
            "detail": f"schema {schema_path}: {schema_reason}",
        }
    required = schema.get("required")
    if not isinstance(required, list):
        return {
            "id": "check-5",
            "status": "fail",
            "detail": f"schema {schema_path}: no 'required' list",
        }

    # The two shape rules the writer applies to the manifest it emits are the
    # same two graded here — one implementation, in _common.py. The hash arm
    # below is deliberately *not* shared: the grader re-hashes the files
    # itself rather than trusting what the writer recorded.
    shape_error = _check_manifest_shape(manifest, required)
    if shape_error is not None:
        return {"id": "check-5", "status": "fail", "detail": shape_error}

    installed = manifest.get("installed", [])
    if not isinstance(installed, list):
        installed = []

    mismatches: list[str] = []
    matched = 0
    for entry in installed:
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            mismatches.append("<malformed installed entry>")
            continue
        path = entry["path"]
        declared = str(entry["sha256"]).lower()
        actual = _sha256_of(os.path.join(root, path))
        if actual is None:
            mismatches.append(f"{path}: file missing or unreadable")
        elif actual.lower() != declared:
            mismatches.append(f"{path}: hash mismatch")
        else:
            matched += 1

    if mismatches:
        return {"id": "check-5", "status": "fail", "detail": _summarize(mismatches)}

    return {
        "id": "check-5",
        "status": "pass",
        "detail": f"required keys present, no verdict key, "
        f"{matched}/{len(installed)} installed hash(es) match",
    }


def _check_6(plan: dict, manifest: dict) -> dict:
    planned = plan.get("planned", [])
    blast_radius = plan.get("blast_radius")
    if not isinstance(blast_radius, list):
        blast_radius = []

    allowed = {_norm_path(p.get("path", "")) for p in planned} | {
        _norm_path(p) for p in blast_radius
    }
    installed = manifest.get("installed", [])
    if not isinstance(installed, list):
        installed = []
    installed_paths = {_norm_path(e.get("path", "")) for e in installed if isinstance(e, dict)}
    planned_paths = {_norm_path(p.get("path", "")) for p in planned}
    changed = installed_paths | planned_paths

    extra = installed_paths - allowed
    if extra:
        return {
            "id": "check-6",
            "status": "fail",
            "detail": _summarize(
                [f"{p} (outside plan ∪ blast_radius)" for p in sorted(extra)], ", "
            ),
        }

    return {
        "id": "check-6",
        "status": "pass",
        "detail": f"{len(changed)} changed path(s) within an allowed set of {len(allowed)}",
    }


def _run_check(check_id: str, fn, *args) -> dict:
    """Run a check function; an unexpected OSError/ValueError becomes that
    check's failure instead of crashing the run (a check never raises)."""
    try:
        return fn(*args)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"id": check_id, "status": "fail", "detail": f"unexpected error: {exc}"}


def _check_7(manifest: dict, rules_only: bool) -> dict:
    """The roborev registration DECISION (ENG-REV-05, ADR-0005 D-13).

    This used to be an unconditional `skip  advisory, run separately`, and the
    cost of that was measured on acme-core (2026-09-20): a full run finished at
    verdict PASS on a repository where the registration question had never been
    put to anyone, and nothing in the ten check lines said so. An unregistered
    repository and one nobody was ever asked about graded identically.

    What is graded is the ANSWER, never the outcome: a decline passes exactly as
    a registration does. The only failure is silence.

    `--rules-only` is the one mode that legitimately has no answer: it installs
    rule files and runs no registration step, so grading it against D-13 would
    make every rules-only run DEGRADED on the absence the mode guarantees. The
    caller states the mode; this check never infers it.
    """
    state, problem = classify_review(manifest.get("review"))
    if rules_only:
        return {
            "id": "check-7",
            "status": "skip",
            "detail": (
                "--rules-only: no registration step runs in this mode, so there is no "
                "decision to grade. A later full run is where the question gets put."
                + ("" if state == "absent" else f" (a prior {state} record is on file)")
            ),
        }
    if state == "absent":
        return {
            "id": "check-7",
            "status": "fail",
            "detail": (
                f"no registration decision on record: {problem}. Fix: setup_roborev.py <root> "
                "--apply --activate --who <name> --payload-ack, or --decline --who <name> "
                "--reason <why>"
            ),
        }
    if problem is not None:
        return {"id": "check-7", "status": "fail", "detail": problem}
    review = manifest["review"]
    who = str(review.get("who")).strip()
    if state == "declined":
        reason = str(review.get("reason")).strip()
        return {
            "id": "check-7",
            "status": "pass",
            "detail": f"registration declined and recorded by {who} — {reason}",
        }
    return {
        "id": "check-7",
        "status": "pass",
        "detail": f"registered on the record by {who} (roborev {review.get('pin')})",
    }


def _check_11(manifest: dict, rules_only: bool) -> dict:
    """The PR reviewer record (ENG-REV-01, business rule 5).

    Every PR flow of the harness waits for a verdict, and which verdict depends on
    whether Automata reviews this repository. A repository where it does not, and
    that nobody asked, waited for the bot forever. Graded like check-7: the ANSWER,
    never which answer -- "collaborators" passes exactly as "automata" does, and
    the only failure is silence (or a malformed record).
    """
    state, problem = classify_pr_review(manifest.get("pr_review"))
    if rules_only:
        return {
            "id": "check-11",
            "status": "skip",
            "detail": (
                "--rules-only: no onboarding question runs in this mode, so there is no PR "
                "reviewer answer to grade"
                + ("" if state == "absent" else f" (a prior {state} record is on file)")
            ),
        }
    if state == "absent":
        return {
            "id": "check-11",
            "status": "fail",
            "detail": (
                f"{problem}. Fix: record_pr_review.py <root> --record "
                "automata|automata_only|collaborators|none --who <name>"
            ),
        }
    if problem is not None:
        return {"id": "check-11", "status": "fail", "detail": problem}
    block = manifest["pr_review"]
    who = str(block.get("who")).strip()
    if state == "none":
        return {
            "id": "check-11",
            "status": "pass",
            "detail": f"no review watch: PRs merge on green CI (recorded by {who})",
        }
    if state == "automata":
        return {
            "id": "check-11",
            "status": "pass",
            "detail": f"PRs wait for {block.get('bot_login', 'automata-ai-bot')} and collaborators "
                      f"(recorded by {who})",
        }
    if state == "automata_only":
        return {
            "id": "check-11",
            "status": "pass",
            "detail": f"PRs wait for {block.get('bot_login', 'automata-ai-bot')} only; "
                      f"collaborators' reviews are reported, not worked "
                      f"(recorded by {who})",
        }
    return {
        "id": "check-11",
        "status": "pass",
        "detail": f"PRs wait for collaborators only, {block.get('min_approvals', 1)} approval(s) "
                  f"(recorded by {who})",
    }


def _check_8(root: str, manifest: dict) -> dict:
    """The working-tree status, recorded — and, since issue #25, graded: an
    installed path that git would ignore can never reach the commit the
    hand-over asks for, so it fails here instead of vanishing from porcelain."""
    res = _run_git(root, "status", "--porcelain")
    if res.returncode == GIT_MISSING_RC and res.stderr == GIT_MISSING_MSG:
        return (
            {"id": "check-8", "status": "fail", "detail": GIT_MISSING_MSG},
            f"git status unavailable: {GIT_MISSING_MSG}",
        )
    if res.returncode == 0:
        git_status = res.stdout
    else:
        stderr_line = (res.stderr.splitlines() or [""])[0]
        git_status = f"git status failed (rc={res.returncode}): {stderr_line}"
    lines = [line for line in res.stdout.splitlines() if line] if res.returncode == 0 else []
    installed = manifest.get("installed", []) if isinstance(manifest, dict) else []
    paths = sorted(
        {str(e["path"]) for e in installed if isinstance(e, dict) and "path" in e}
        | {".claude/harness.json"}
    )
    ignored: list[str] = []
    if paths and res.returncode == 0:
        chk = _run_git(root, "check-ignore", "-v", "--no-index", "--", *paths)
        if chk.returncode == 0:
            for line in chk.stdout.splitlines():
                head, sep, path = line.partition("\t")
                pattern = head.split(":", 2)[-1] if sep else ""
                if sep and not pattern.startswith("!"):
                    ignored.append(f"{path} (by {head})")
    if ignored:
        return {
            "id": "check-8",
            "status": "fail",
            "detail": f"{len(ignored)} installed path(s) git-ignored, never reach a commit: "
            + _summarize(ignored),
        }, git_status
    return {
        "id": "check-8",
        "status": "pass",
        "detail": f"{len(lines)} line(s) of porcelain status recorded; no installed path git-ignored",
    }, git_status


def _check_10(root: str, manifest: dict) -> dict:
    """The recorded network policy, graded against the settings file on disk —
    never against the plan's claim that it merged one."""
    block = manifest.get("network")
    if not isinstance(block, dict):
        return {
            "id": "check-10",
            "status": "skip",
            "detail": "manifest records no network policy",
        }
    domains = [d for d in (block.get("domains") or []) if isinstance(d, str)]
    excluded = [c for c in (block.get("excluded_commands") or []) if isinstance(c, str)]
    gaps = [g for g in (block.get("gaps") or []) if isinstance(g, dict)]
    gap_note = f"; {len(gaps)} recorded gap(s), applied by nobody" if gaps else ""
    if not domains and not excluded:
        return {
            "id": "check-10",
            "status": "skip",
            "detail": f"the recorded network policy seeds no domain and no exclusion{gap_note}",
        }
    settings_path = os.path.join(root, ".claude", "settings.json")
    settings, reason = _load_json_object(settings_path)
    if settings is None:
        return {"id": "check-10", "status": "fail", "detail": f"settings.json: {reason}"}
    sandbox = settings.get("sandbox")
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    net = sandbox.get("network")
    net = net if isinstance(net, dict) else {}
    allowed = net.get("allowedDomains")
    allowed = allowed if isinstance(allowed, list) else []
    excluded_now = sandbox.get("excludedCommands")
    excluded_now = excluded_now if isinstance(excluded_now, list) else []

    problems: list[str] = []
    for domain in domains:
        if domain not in allowed:
            problems.append(
                f".claude/settings.json: sandbox.network.allowedDomains no longer has {domain}"
            )
    for command in excluded:
        if command not in excluded_now:
            problems.append(
                f".claude/settings.json: sandbox.excludedCommands no longer has {command}"
            )
    if problems:
        return {"id": "check-10", "status": "fail", "detail": _summarize(problems)}
    return {
        "id": "check-10",
        "status": "pass",
        "detail": f"{len(domains)} recorded domain(s) and {len(excluded)} exclusion(s) "
        f"still in .claude/settings.json{gap_note}",
    }


# --- Checks 11-13: the harness told the repository about itself (#403) ------
# One root cause, three graders. `harness-init` used to TEMPLATE what it should
# have READ, and it left the target's non-Claude agent config to whatever the
# session model improvised. On acme-core that produced, in one run: an
# allowlist naming five scripts that do not exist, `.codex/hooks.json` with the
# author's home directory baked into the command a hook executes, and prose
# citing a rule file the run never installed.
#
# Each is graded against the TREE, never against the plan's claim or the
# manifest's record — the same rule check-10 follows, and for the same reason:
# a record is an answer, not ground truth about the world.

# Where a machine-local path is a defect rather than documentation. An absolute
# path in prose can be a server's real layout, correctly written down; an
# absolute path in a config a hook EXECUTES is a file that works on exactly one
# machine. Only the second is graded.
EXECUTABLE_CONFIG_SUFFIXES = (".json", ".toml", ".yaml", ".yml")
HOME_PATH_RE = re.compile(r"(?<![\w/])(?:/Users/|/home/|/root/|[A-Za-z]:\\Users\\)[^\s\"'`,;)\]}]+")
# Paths under these roots are the machine's own furniture, not the target's.
SYSTEM_HOME_PREFIXES = ("/home/runner/", "/root/.cache/")

# Files whose content is patterns, not references. Never graded by check-14.
IGNORE_PATTERN_FILES = {".gitignore", ".dockerignore", ".eslintignore", ".prettierignore"}

# A reference to a harness-owned file, as it appears inside harness-owned
# content. Anchored on the agent directories so ordinary prose about a `.claude`
# concept is not read as a path.
REPO_REF_RE = re.compile(r"(?<![\w./-])(\.(?:claude|codex|cursor|agents|github)/[A-Za-z0-9._/-]+\.[A-Za-z0-9]+)")


def _graded_files(root: str, manifest: dict) -> list[str]:
    """Every harness-owned file this run is responsible for: the manifest's
    `installed[]` paths, plus the agent config files a run writes or a session
    model improvises beside them. Paths that do not exist are simply absent —
    check-1 is what grades a missing write."""
    rels: list[str] = []
    for entry in manifest.get("installed") or []:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            rels.append(entry["path"])
    rels.extend([
        ".claude/settings.json", "AGENTS.md", ".roborev.toml",
        ".codex/hooks.json", ".codex/config.toml",
        ".github/copilot-instructions.md", ".windsurfrules",
    ])
    for sub in (".codex/hooks", ".claude/hooks"):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d))[:50]:
                rels.append(f"{sub}/{name}")
    out: list[str] = []
    for rel in rels:
        if rel in out:
            continue
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            out.append(rel)
    return out


def _read_text(path: str, cap: int = 400000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


def _check_12(root: str, manifest: dict) -> dict:
    """Every script-shaped allow rule in the target's settings names a script
    the repository actually declares.

    Graded from the repository, not from the manifest's `allowlist` record: a
    repo harnessed before #403 has no record at all and is exactly the repo
    whose allowlist needs grading."""
    settings_path = os.path.join(root, ".claude", "settings.json")
    settings, reason = _load_json_object(settings_path)
    if settings is None:
        return {"id": "check-12", "status": "skip", "detail": f"settings.json: {reason}"}
    perms = settings.get("permissions")
    perms = perms if isinstance(perms, dict) else {}
    allow = [r for r in (perms.get("allow") or []) if isinstance(r, str)]

    declared = _declared_commands(root)
    known = {s["command"] for s in declared["scripts"]}
    runners = {"pnpm", "npm", "yarn", "bun", "make"}
    graded, unknown = [], []
    for rule in allow:
        if not (rule.startswith("Bash(") and rule.endswith(")")):
            continue
        command = rule[5:-1].strip()
        head = command.split(" ", 1)[0]
        if head not in runners or "*" in command:
            continue
        graded.append(command)
        if command not in known:
            unknown.append(command)
    if not graded:
        return {
            "id": "check-12",
            "status": "skip",
            "detail": "no script-shaped allow rules in .claude/settings.json",
        }
    if unknown:
        return {
            "id": "check-12",
            "status": "fail",
            "detail": _summarize([
                f".claude/settings.json: permissions.allow names `{c}`, which this repository "
                f"does not declare"
                for c in unknown
            ] + [
                f"the repository declares {len(known)} command(s); "
                f"{declared['runner_source'] or 'no lockfile'} names the runner"
            ]),
        }
    return {
        "id": "check-12",
        "status": "pass",
        "detail": f"{len(graded)} script allow rule(s), all declared by the repository "
        f"({declared['runner_source'] or 'no lockfile'})",
    }


def _check_13(root: str, manifest: dict) -> dict:
    """No machine-local absolute path in a config the harness owns.

    A hook command carrying `/Users/<someone>/...` works on one machine and
    fails silently everywhere else, which is the worst shape a hook can take:
    installed, committed, and inert for every teammate."""
    problems: list[str] = []
    scanned = 0
    for rel in _graded_files(root, manifest):
        if not rel.endswith(EXECUTABLE_CONFIG_SUFFIXES):
            continue
        scanned += 1
        text = _read_text(os.path.join(root, rel))
        for match in HOME_PATH_RE.findall(text):
            if match.startswith(SYSTEM_HOME_PREFIXES):
                continue
            problems.append(f"{rel}: machine-local absolute path `{match}`")
    if not scanned:
        return {"id": "check-13", "status": "skip", "detail": "no harness-owned config files"}
    if problems:
        return {
            "id": "check-13",
            "status": "fail",
            "detail": _summarize(sorted(set(problems))[:8] + [
                "use ${CLAUDE_PROJECT_DIR}/${CODEX_PROJECT_DIR:-$PWD} or a repo-relative path"
            ]),
        }
    return {
        "id": "check-13",
        "status": "pass",
        "detail": f"{scanned} harness-owned config file(s), no machine-local path",
    }


def _check_14(root: str, manifest: dict) -> dict:
    """Harness-owned content does not cite a harness path that is not there.

    WARN, never FAIL, and the distinction is the point. Some of this content is
    vendored verbatim from the pinned somnio-ai-tools corpus (ADR-0003), which
    this area consumes read-only and does not modify (business rule 17). A
    TypeScript rule written for a Firebase Functions repository cites
    `.claude/rules/functions/architecture.md`; installed into a Fastify
    repository that file does not exist, and the honest answer is to NAME the
    gap, not to rewrite a vendored file behind the vendor's back or to fail a
    run over content that is correct upstream."""
    problems: list[str] = []
    scanned = 0
    for rel in _graded_files(root, manifest):
        # An ignore pattern is not a reference. `.gitignore` names paths
        # precisely because they are not supposed to be in the repository —
        # `.claude/settings.json.bak` is the whole point of that line.
        if os.path.basename(rel) in IGNORE_PATTERN_FILES:
            continue
        text = _read_text(os.path.join(root, rel))
        if not text:
            continue
        scanned += 1
        for ref in dict.fromkeys(REPO_REF_RE.findall(text)):
            if ref == rel or os.path.exists(os.path.join(root, ref)):
                continue
            problems.append(f"{rel}: cites `{ref}`, which this repository does not have")
    if not scanned:
        return {"id": "check-14", "status": "skip", "detail": "no harness-owned files"}
    if problems:
        return {
            "id": "check-14",
            "status": "warn",
            "detail": _summarize(sorted(set(problems))[:8] + [
                "vendored content may cite rules this stack does not install (ADR-0003 D-3); "
                "authored content citing a missing path is a defect"
            ]),
        }
    return {
        "id": "check-14",
        "status": "pass",
        "detail": f"{scanned} harness-owned file(s), every cited harness path exists",
    }


def _check_15(manifest: dict) -> dict:
    """The execution record (ENG-EXEC-01/05): which tracker holds this repository's
    tickets, and the autonomy gates its owners granted.

    A WARN when absent, never a failure: dev-execution and loop-mode default to Jira
    and ask only when the repo names another tracker, so silence here costs one
    question later, not a wrong run. A malformed record FAILS, because both
    execution skills refuse to read it and would stop on it.
    """
    state, problem = classify_execution(manifest.get("execution"))
    if state == "absent":
        return {
            "id": "check-15",
            "status": "warn",
            "detail": (
                f"{problem}; /dev-execution and /loop-mode default to Jira unless the repo names another tracker. Fix: "
                "record_execution.py <root> --record jira|youtrack|github|local --who <name>"
            ),
        }
    if problem is not None:
        return {"id": "check-15", "status": "fail", "detail": problem}
    block = manifest["execution"]
    t = block["tracker"]
    where = t["type"] + (f" {t['project_key']}" if t.get("project_key") else "")
    gates = "autonomy gates recorded" if block.get("gates") else "no autonomy gates (runs ask)"
    return {
        "id": "check-15",
        "status": "pass",
        "detail": f"tickets in {where}; {gates} (recorded by {str(block['who']).strip()})",
    }


def _check_9(root: str, manifest: dict, plan: dict) -> dict:
    """The CLI skills step, graded against the repository and against the CLI's
    own manifest — never against harness-init's claim that it ran.

    An empty ``cli_installed`` is a ``skip`` only when the step was not supposed
    to write anything: it was disabled (``--no-cli-skills``) or there was no
    binary to call. A run that had both an enabled step and a binary and still
    recorded nothing is a **fail** — the gap text comes from the manifest, which
    names why each agent's call produced nothing.
    """
    entries = manifest.get("cli_installed")
    if not isinstance(entries, list) or not entries:
        cli_plan = plan.get("cli_skills") if isinstance(plan, dict) else None
        cli_plan = cli_plan if isinstance(cli_plan, dict) else {}
        if cli_plan.get("enabled") and cli_plan.get("binary"):
            gaps = [
                str(n) for n in (manifest.get("not_in_scope") or [])
                if isinstance(n, str) and n.startswith("cli skills")
            ]
            detail = (
                "; ".join(gaps) if gaps
                else "the CLI skills step was enabled with a binary present but "
                "recorded no cli_installed path and named no gap"
            )
            return {"id": "check-9", "status": "fail", "detail": detail}
        return {
            "id": "check-9",
            "status": "skip",
            "detail": "manifest has no cli_installed entries — the CLI skills step "
            "did not run, or installed nothing",
        }

    cli_manifests: dict[str, set] = {}
    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("<malformed cli_installed entry>")
            continue
        path = entry.get("path")
        skill = entry.get("skill")
        manifest_rel = entry.get("manifest")
        if not isinstance(path, str) or not isinstance(skill, str) or not isinstance(manifest_rel, str):
            problems.append("<cli_installed entry missing path/skill/manifest>")
            continue
        if not os.path.exists(os.path.join(root, path)):
            problems.append(f"{path}: missing on disk")
            continue
        if manifest_rel not in cli_manifests:
            obj, reason = _load_json_object(os.path.join(root, manifest_rel))
            skills = obj.get("skills") if isinstance(obj, dict) else None
            if not isinstance(skills, dict):
                cli_manifests[manifest_rel] = set()
                problems.append(f"{manifest_rel}: {reason or 'no skills object'}")
                continue
            cli_manifests[manifest_rel] = set(skills)
        if skill not in cli_manifests[manifest_rel]:
            problems.append(f"{path}: {manifest_rel} does not list {skill}")

    if problems:
        return {"id": "check-9", "status": "fail", "detail": _summarize(problems)}
    return {
        "id": "check-9",
        "status": "pass",
        "detail": f"{len(entries)} CLI-installed path(s) present and listed by "
        f"{len(cli_manifests)} CLI manifest(s)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a /harness-init run against the target repository "
        "it wrote into.",
    )
    parser.add_argument("--root", required=True, help="target repository root")
    parser.add_argument("--plan", required=True, help="path to the write-plan JSON")
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="the run installed rules only, so check-7 has no registration decision and "
             "check-11 no PR reviewer answer to grade (both govern onboarding, not a "
             "rule-file copy)",
    )
    parser.add_argument(
        "--audit-report",
        default=None,
        help="path to the optimize-context-rules --audit-only report",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="path to harness-manifest.schema.json (else resolved, see the module docstring)",
    )
    parser.add_argument(
        "--plugin-root",
        default=None,
        help="the plugin directory, the last step of the schema resolution chain",
    )
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help="directory to write the evidence file into (default: <root>/.claude/.harness)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the evidence object to stdout instead of the human summary",
    )
    args = parser.parse_args()

    root_arg = args.root
    if not os.path.isdir(root_arg):
        print(f"error: {root_arg} is not a directory", file=sys.stderr)
        return 2
    root = os.path.realpath(root_arg)

    schema_path, schema_reason = _resolve_asset(
        args.schema, MANIFEST_SCHEMA_NAME, args.plugin_root
    )
    if schema_path is None:
        print(f"error: schema {schema_reason}", file=sys.stderr)
        return 2

    plan, plan_reason = _load_json_object(args.plan)
    if plan is None:
        print(f"error: {args.plan}: {plan_reason}", file=sys.stderr)
        return 2
    planned = plan.get("planned")
    if not isinstance(planned, list):
        print(f"error: {args.plan}: 'planned' is missing or not a list", file=sys.stderr)
        return 2
    for entry in planned:
        if not isinstance(entry, dict) or "path" not in entry:
            print(
                f"error: {args.plan}: a 'planned' entry is not an object with a 'path'",
                file=sys.stderr,
            )
            return 2

    manifest_path = os.path.join(root, ".claude", "harness.json")
    manifest, manifest_reason = _load_json_object(manifest_path)
    if manifest is None:
        print(f"error: {manifest_path}: {manifest_reason}", file=sys.stderr)
        return 2

    evidence_dir = args.evidence_dir or os.path.join(root, ".claude", ".harness")
    # The shared gate treats the root itself as outside, because nothing a write
    # plan names is ever the root; an --evidence-dir that *is* the root has
    # always been allowed here, so it is spelled out rather than lost.
    if os.path.realpath(evidence_dir) != root and _outside(evidence_dir, root):
        print(
            f"error: refusing to write evidence outside --root: {evidence_dir}",
            file=sys.stderr,
        )
        return 2
    try:
        os.makedirs(evidence_dir, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot write evidence to {evidence_dir}: {exc}", file=sys.stderr)
        return 2

    checks: list[dict] = []
    checks.append(_run_check("check-1", _check_1, root, plan))
    checks.append(_run_check("check-2", _check_2, root, plan))
    checks.append(_run_check("check-3", _check_3, root, manifest))
    checks.append(_run_check("check-4", _check_4, args.audit_report))
    checks.append(_run_check("check-5", _check_5, root, manifest, schema_path))
    checks.append(_run_check("check-6", _check_6, plan, manifest))
    checks.append(_run_check("check-7", _check_7, manifest, args.rules_only))
    check8, git_status = _check_8(root, manifest)
    checks.append(check8)
    checks.append(_run_check("check-9", _check_9, root, manifest, plan))
    checks.append(_run_check("check-10", _check_10, root, manifest))
    checks.append(_run_check("check-11", _check_11, manifest, args.rules_only))
    checks.append(_run_check("check-12", _check_12, root, manifest))
    checks.append(_run_check("check-13", _check_13, root, manifest))
    checks.append(_run_check("check-14", _check_14, root, manifest))
    checks.append(_run_check("check-15", _check_15, manifest))

    failed_ids = [c["id"] for c in checks if c["status"] == "fail"]
    verdict = "DEGRADED" if failed_ids else "PASS"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = {
        "verdict": verdict,
        "checks": checks,
        "git_status": git_status,
        "plan_path": os.path.realpath(args.plan),
        "manifest_path": os.path.realpath(manifest_path),
        "timestamp": timestamp,
    }

    evidence_file = os.path.join(evidence_dir, f"init-{timestamp}.json")
    with open(evidence_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(evidence, indent=2))
        f.write("\n")

    if args.json:
        print(json.dumps(evidence))
        if failed_ids:
            print(f"verdict: DEGRADED (failed: {', '.join(failed_ids)})", file=sys.stderr)
        else:
            print("verdict: PASS", file=sys.stderr)
    else:
        for c in checks:
            print(f"{c['id']}  {c['status']}  {c['detail']}")
        if failed_ids:
            print(f"verdict: DEGRADED (failed: {', '.join(failed_ids)})")
        else:
            print("verdict: PASS")

    return 1 if failed_ids else 0


if __name__ == "__main__":
    sys.exit(main())
