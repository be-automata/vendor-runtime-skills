#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Repo-state evidence + greenfield/brownfield/ambiguous verdict for /harness-init.

Read-only: this script writes nothing to <root> or anywhere else. It answers
one question deterministically — is this repository greenfield, brownfield,
or genuinely ambiguous? — from git history, a source-file census, manifest
presence and an existing-harness scan, then prints one JSON evidence payload.

Usage:
    uv run detect_repo_state.py <root>

stdout: on success, a single JSON object (see the payload contract in
        references/detection-signals.md); on error, nothing.
stderr: on success, one line "<verdict>: <reason>"; on error, one line
        beginning "error:"; warnings (e.g. a permission-denied subdirectory)
        are also printed here and do not affect the exit code.
exit:   0 verdict emitted · 2 not a repository / subdirectory / usage
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

# _common.py ships beside this script and owns the pruned-directory set and the
# git runner. Imported, never copied — two copies would be two answers.
# Importing a sibling module byte-compiles it unless this is set: a plugin
# cache (or a rules source) is read-only by contract, and a __pycache__/ dir
# appearing beside these scripts is a write nobody asked for. The env var
# PYTHONDONTWRITEBYTECODE=1 on every SKILL.md invocation covers the
# interpreter; this flag covers the import below when it is not set.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import EXCLUDED_DIRS, GIT_MISSING_MSG, GIT_MISSING_RC, _run_git  # noqa: E402

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Pruned only at the repository root: linked worktrees are copies of this same
# tree. Extend by PR.
TOPLEVEL_EXCLUDED_DIRS = {"worktrees"}

# Extensions counted toward source_files. Extend by PR.
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".dart",
                      ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".cs",
                      ".vue", ".svelte", ".scala"}

# Manifest filenames that count as stack evidence (depth <= 2). Extend by PR.
MANIFEST_NAMES = {"pyproject.toml", "setup.py", "package.json", "pubspec.yaml",
                   "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
                   "build.gradle.kts", "firebase.json"}
# plus the glob requirements*.txt

# Directory basenames counted as test_dirs (depth <= 2). Extend by PR.
TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "e2e"}

# Below this many source files, a repository cannot plausibly have accreted
# conventions worth reading — see references/detection-signals.md. Extend by PR.
BROWNFIELD_SOURCE_FLOOR = 25


def _rel_posix(path: str, root: str) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _depth(path: str, root: str) -> int:
    rel = os.path.relpath(path, root)
    if rel == ".":
        return 0
    return len(Path(rel).parts)


def _describe_commit(root: str, rev: str) -> dict | None:
    res = _run_git(root, "log", "-1", "--format=%H%x09%aI", rev)
    line = res.stdout.strip()
    if not line or "\t" not in line:
        return None
    sha, date = line.split("\t", 1)
    return {"sha": sha, "date": date}


def _collect_git(root: str, head_sha: str | None) -> dict:
    """`head_sha` is the HEAD main already resolved; None means HEAD does not
    resolve, which is the zero-commit repository."""
    if head_sha is None:
        return {
            "commit_count": 0,
            "first_commit": None,
            "last_commit": None,
            "tracked_files": _count_tracked(root),
            "origin_head": _origin_head(root),
        }

    count_res = _run_git(root, "rev-list", "--count", "HEAD")
    commit_count = int(count_res.stdout.strip() or "0")

    # The root commit, read as a bounded lookup instead of walking the whole
    # history with `log --reverse`. A history with several roots lists them in
    # the same traversal order `git log` uses, so the last root printed is the
    # one `log --reverse` would have shown first.
    roots = _run_git(root, "rev-list", "--max-parents=0", "HEAD").stdout.split()
    first_commit = _describe_commit(root, roots[-1]) if roots else None
    last_commit = _describe_commit(root, head_sha)

    return {
        "commit_count": commit_count,
        "first_commit": first_commit,
        "last_commit": last_commit,
        "tracked_files": _count_tracked(root),
        "origin_head": _origin_head(root),
    }


def _count_tracked(root: str) -> int:
    res = _run_git(root, "ls-files")
    if res.returncode != 0:
        return 0
    lines = [line for line in res.stdout.splitlines() if line]
    return len(lines)


def _origin_head(root: str) -> str | None:
    res = _run_git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if res.returncode != 0:
        return None
    value = res.stdout.strip()
    return value or None


def _walk(root: str) -> dict:
    source_by_ext: dict[str, int] = {}
    manifests: list[str] = []
    test_dirs: list[str] = []
    warnings: list[str] = []

    def _on_error(exc: OSError) -> None:
        relpath = _rel_posix(exc.filename or root, root)
        warnings.append(f"warning: skipped {relpath}: permission denied")

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_error):
        dir_depth = _depth(dirpath, root)
        pruned = set(EXCLUDED_DIRS)
        if dir_depth == 0:
            # A top-level worktrees/ holds checkouts of this same repository:
            # counting them would multiply the census by the worktree count.
            pruned |= TOPLEVEL_EXCLUDED_DIRS
        dirnames[:] = sorted(d for d in dirnames if d not in pruned)

        for dirname in list(dirnames):
            if dirname in TEST_DIR_NAMES and dir_depth + 1 <= 2:
                test_dirs.append(_rel_posix(os.path.join(dirpath, dirname), root))

        file_depth = dir_depth
        for filename in filenames:
            ext = Path(filename).suffix
            if ext in SOURCE_EXTENSIONS:
                source_by_ext[ext] = source_by_ext.get(ext, 0) + 1

            if file_depth <= 2:
                is_manifest = filename in MANIFEST_NAMES or fnmatch.fnmatch(
                    filename, "requirements*.txt"
                )
                if is_manifest:
                    manifests.append(_rel_posix(os.path.join(dirpath, filename), root))

    total = sum(source_by_ext.values())

    for warning in warnings:
        print(warning, file=sys.stderr)

    return {
        "source_files": {"total": total, "by_extension": dict(sorted(source_by_ext.items()))},
        "manifests": sorted(set(manifests)),
        "test_dirs": sorted(set(test_dirs)),
    }


def _collect_ci_workflows(root: str) -> list[str]:
    found: list[str] = []
    workflows_dir = os.path.join(root, ".github", "workflows")
    if os.path.isdir(workflows_dir):
        for name in os.listdir(workflows_dir):
            if name.endswith(".yml") or name.endswith(".yaml"):
                found.append(_rel_posix(os.path.join(workflows_dir, name), root))
    gitlab_ci = os.path.join(root, ".gitlab-ci.yml")
    if os.path.isfile(gitlab_ci):
        found.append(_rel_posix(gitlab_ci, root))
    return sorted(found)


def _collect_docs(root: str) -> list[str]:
    found: list[str] = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if os.path.isfile(full) and name.endswith(".md"):
            found.append(_rel_posix(full, root))
    docs_dir = os.path.join(root, "docs")
    if os.path.isdir(docs_dir):
        for name in os.listdir(docs_dir):
            full = os.path.join(docs_dir, name)
            if os.path.isfile(full) and name.endswith(".md"):
                found.append(_rel_posix(full, root))
    return sorted(found)


def _count_md_recursive(dirpath: str) -> int:
    if not os.path.isdir(dirpath):
        return 0
    count = 0
    for dirpath2, dirnames, filenames in os.walk(dirpath):
        for filename in filenames:
            if filename.endswith(".md"):
                count += 1
    return count


def _count_skills(claude_dir: str) -> int:
    skills_dir = os.path.join(claude_dir, "skills")
    if not os.path.isdir(skills_dir):
        return 0
    count = 0
    for name in os.listdir(skills_dir):
        skill_dir = os.path.join(skills_dir, name)
        if os.path.isdir(skill_dir) and os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
            count += 1
    return count


def _collect_harness(root: str) -> dict:
    claude_dir = os.path.join(root, ".claude")
    return {
        "claude_md": os.path.isfile(os.path.join(root, "CLAUDE.md")),
        "agents_md": os.path.isfile(os.path.join(root, "AGENTS.md")),
        "settings_json": os.path.isfile(os.path.join(claude_dir, "settings.json")),
        "rules_count": _count_md_recursive(os.path.join(claude_dir, "rules")),
        "agents": _count_md_recursive(os.path.join(claude_dir, "agents")),
        "commands": _count_md_recursive(os.path.join(claude_dir, "commands")),
        "skills": _count_skills(claude_dir),
        "ship_gate": os.path.isfile(os.path.join(claude_dir, "ship-gate.json")),
        "harness_json": os.path.isfile(os.path.join(claude_dir, "harness.json")),
        "loop_config": os.path.isfile(os.path.join(root, ".loop", "config.yaml")),
    }


def _verdict(git_info: dict, source_files: dict, manifests: list[str]) -> tuple[str, str]:
    commit_count = git_info["commit_count"]
    total = source_files["total"]
    has_manifest = bool(manifests)

    if commit_count == 0:
        return "greenfield", "no resolvable HEAD (commit_count=0)"

    if has_manifest and total >= BROWNFIELD_SOURCE_FLOOR:
        return "brownfield", (
            f"{len(manifests)} manifest(s) present and {total} source files "
            f">= floor {BROWNFIELD_SOURCE_FLOOR}"
        )

    if total == 0:
        return "greenfield", "0 source files"

    if not has_manifest and commit_count <= 1:
        return "greenfield", f"no stack manifest and commit_count={commit_count} <= 1"

    return "ambiguous", (
        f"{total} source files, {len(manifests)} manifest(s), "
        f"commit_count={commit_count} match no other branch"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit repo-state evidence and a greenfield/brownfield/ambiguous verdict.",
    )
    parser.add_argument("root", help="path to the candidate repository root")
    args = parser.parse_args()

    root_arg = args.root
    # One spawn answers both questions. `rev-parse` still prints the toplevel
    # when HEAD does not resolve — it just exits non-zero and echoes "HEAD"
    # back — so the toplevel line, not the exit code, decides whether this is a
    # repository at all, and a second line that is not a sha means "no commits
    # yet", exactly what `rev-parse --verify -q HEAD` used to decide.
    rev_parse = _run_git(root_arg, "rev-parse", "--show-toplevel", "HEAD")
    if rev_parse.returncode == GIT_MISSING_RC and rev_parse.stderr == GIT_MISSING_MSG:
        print(f"error: {GIT_MISSING_MSG}; cannot read the repository state", file=sys.stderr)
        return 2
    rev_lines = rev_parse.stdout.splitlines()
    toplevel_line = rev_lines[0].strip() if rev_lines else ""
    if not toplevel_line:
        print(f"error: {root_arg} is not inside a git repository", file=sys.stderr)
        return 2
    head_sha = rev_lines[1].strip() if len(rev_lines) > 1 else ""
    head_sha = head_sha if SHA_RE.match(head_sha) else None

    toplevel_real = os.path.realpath(toplevel_line)
    root_real = os.path.realpath(root_arg)
    if toplevel_real != root_real:
        print(
            "error: root is a subdirectory of a git repository; pass the repository root",
            file=sys.stderr,
        )
        return 2

    git_info = _collect_git(root_real, head_sha)
    walk_info = _walk(root_real)
    ci_workflows = _collect_ci_workflows(root_real)
    docs = _collect_docs(root_real)
    harness = _collect_harness(root_real)

    verdict, reason = _verdict(git_info, walk_info["source_files"], walk_info["manifests"])

    payload = {
        "root": root_real,
        "git": {
            "toplevel": toplevel_real,
            "commit_count": git_info["commit_count"],
            "first_commit": git_info["first_commit"],
            "last_commit": git_info["last_commit"],
            "tracked_files": git_info["tracked_files"],
            "origin_head": git_info["origin_head"],
        },
        "source_files": walk_info["source_files"],
        "manifests": walk_info["manifests"],
        "ci_workflows": ci_workflows,
        "test_dirs": walk_info["test_dirs"],
        "harness": harness,
        "docs": docs,
        "verdict": verdict,
        "reason": reason,
    }

    print(json.dumps(payload))
    print(f"{verdict}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
