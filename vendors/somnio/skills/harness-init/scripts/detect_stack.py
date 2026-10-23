#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Stack detection for /harness-init: base languages + frameworks with evidence.

Read-only: this script writes nothing to <root> or anywhere else. It answers
one question deterministically — what stacks does this repository use? — by
checking a fixed set of manifest, config and dependency signals, then prints
one JSON object. Base languages are always reported when their signal fires;
frameworks are reported in addition, never instead.

Usage:
    uv run detect_stack.py <root>
    uv run detect_stack.py --list-served

`--list-served` answers the greenfield question instead of detecting: it needs no
root and prints {"served": [{"name", "skills"}, ...]} — the ten stacks the pinned
corpus carries rules for and, per stack, the CLI catalog skill pair that exists
for it (an empty list when the stack gets rules only).

stdout: on success, a single JSON object {"stacks": [...]} (see the signal
        table in references/detection-signals.md); on error, nothing.
stderr: on success, a one-line summary; on error, one line beginning
        "error:".
exit:   0 stacks emitted (possibly an empty list) · 2 not a directory / usage
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

# _common.py ships beside this script and owns the constants more than one of
# these scripts needs: the canonical stack order and the directory names a
# bounded walk must never descend into. Imported, never copied — two copies
# would be two answers.
# Importing a sibling module byte-compiles it unless this is set: a plugin
# cache (or a rules source) is read-only by contract, and a __pycache__/ dir
# appearing beside these scripts is a write nobody asked for. The env var
# PYTHONDONTWRITEBYTECODE=1 on every SKILL.md invocation covers the
# interpreter; this flag covers the import below when it is not set.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import EXCLUDED_DIRS, STACK_ORDER  # noqa: E402

# The ten stacks the pinned corpus carries a rules directory for, in the order
# the greenfield question offers them. `--list-served` prints this table so the
# skill can render "the list of the rules we have" without reading the corpus,
# and it is the ONE authority on `served`: a repo scan that called a stack
# served while --list-served omitted it (`node` did, until HI-17) offers the
# person a stack nothing can install. Extend by PR, together with SERVED_SKILLS
# below and install_assets.py's copy.
SERVED_STACKS = ["python", "django", "fastapi", "flask", "typescript",
                 "nestjs", "react", "dart", "flutter", "functions"]

# Everything a scan can emit that SERVED_STACKS does not carry — `node`, `go`,
# `rust`, `jvm` — is `served: false`: the language is read, the pin just carries
# no rules for it. The authoritative served set at the pin is still resolved
# downstream by resolve_rules_source.py; this script never reads the corpus.

# Stacks whose name also has a `<stack>_plan` / `<stack>_health` pair in the
# somnio CLI's skills catalog. A stack absent from this map gets rules only.
SERVED_SKILLS = {
    "python": ["python_plan", "python_health"],
    "nestjs": ["nestjs_plan", "nestjs_health"],
    "react": ["react_plan", "react_health"],
    "flutter": ["flutter_plan", "flutter_health"],
}

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _rel_posix(path: str, root: str) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _add(stacks: dict, name: str, confidence: str, evidence: str) -> None:
    served = name in SERVED_STACKS
    entry = stacks.get(name)
    if entry is None:
        stacks[name] = {
            "name": name,
            "confidence": confidence,
            "served": served,
            "evidence": [evidence],
        }
        return
    if evidence not in entry["evidence"]:
        entry["evidence"].append(evidence)
    if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[entry["confidence"]]:
        entry["confidence"] = confidence


def _find_tsconfig(root: str) -> str | None:
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else len(Path(rel).parts)
        if depth >= 2:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        if "tsconfig.json" in filenames:
            return _rel_posix(os.path.join(dirpath, "tsconfig.json"), root)
    return None


def _requirements_files(root: str) -> list[str]:
    return sorted(
        name for name in os.listdir(root)
        if fnmatch.fnmatch(name, "requirements*.txt") and os.path.isfile(os.path.join(root, name))
    )


def _python_manifest_files(root: str) -> list[str]:
    files = []
    for name in ("pyproject.toml", "requirements.txt"):
        if os.path.isfile(os.path.join(root, name)):
            files.append(name)
    for name in _requirements_files(root):
        if name not in files:
            files.append(name)
    return files


def _file_declares(root: str, filename: str, package: str) -> bool:
    try:
        with open(os.path.join(root, filename), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    needle = package.lower()
    for line in text.splitlines():
        if needle in line.lower():
            return True
    return False


def _detect_python(root: str, stacks: dict) -> None:
    manifests = []
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        manifests.append("pyproject.toml")
    if os.path.isfile(os.path.join(root, "setup.py")):
        manifests.append("setup.py")
    manifests.extend(_requirements_files(root))

    for name in manifests:
        _add(stacks, "python", "high", name)

    dep_files = _python_manifest_files(root)  # pyproject.toml + requirements*.txt only
    django_via_deps = False
    for framework in ("fastapi", "django", "flask"):
        for filename in dep_files:
            if _file_declares(root, filename, framework):
                _add(stacks, framework, "high", filename)
                _add(stacks, "python", "high", filename)
                if framework == "django":
                    django_via_deps = True
                break

    if not django_via_deps and os.path.isfile(os.path.join(root, "manage.py")):
        _add(stacks, "django", "medium", "manage.py")
        _add(stacks, "python", "medium", "manage.py")


def _load_package_json(root: str) -> dict | None:
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_WORKSPACE_FILES = ("pnpm-workspace.yaml", "turbo.json", "lerna.json", "nx.json")


def _workspace_marker(root: str) -> str | None:
    """The file that declares `root` a monorepo workspace, or None."""
    for name in _WORKSPACE_FILES:
        if os.path.isfile(os.path.join(root, name)):
            return name
    pkg = _load_package_json(root)
    if pkg is not None and "workspaces" in pkg:
        return "package.json:workspaces"
    return None


def _dep_evidence(pkg: dict, key: str) -> str | None:
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(pkg.get(section), dict) and key in pkg[section]:
            return f"package.json:{section}.{key}"
    return None


def _detect_node(root: str, stacks: dict) -> None:
    if not os.path.isfile(os.path.join(root, "package.json")):
        return
    _add(stacks, "node", "medium", "package.json")

    pkg = _load_package_json(root)
    if pkg is None:
        return  # unparsable: node already emitted from existence; dep-based rules skipped

    nestjs_evidence = _dep_evidence(pkg, "@nestjs/core")
    if nestjs_evidence:
        _add(stacks, "nestjs", "high", nestjs_evidence)
        _add(stacks, "node", "high", nestjs_evidence)

    for key in ("react", "next"):
        react_evidence = _dep_evidence(pkg, key)
        if react_evidence:
            _add(stacks, "react", "high", react_evidence)
            _add(stacks, "node", "high", react_evidence)


# A pubspec that names Flutter anywhere it can: the SDK dependency
# (`sdk: flutter`), the top-level `flutter:` section, or the
# `environment.flutter` constraint. Line-wise, like detect_network's pubspec
# scan — no YAML dependency for three patterns.
# Block style (`flutter:` key, `sdk: flutter`) and flow style
# (`dependencies: {flutter: {sdk: flutter}}`) are both valid YAML for one thing.
_FLUTTER_PUBSPEC = re.compile(r"^\s*flutter:|\bsdk:\s*[\"']?flutter\b|[{,]\s*flutter\s*:")


def _pubspec_names_flutter(path: str) -> bool | None:
    """True/False for a readable pubspec, None when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    return any(_FLUTTER_PUBSPEC.search(line) for line in lines)


_NOT_WORKSPACE_MEMBERS = {"example", "examples", "test", "tests", "fixtures", "test_fixtures", "doc", "docs"}


def _nested_flutter_pubspec(root: str) -> str | None:
    """The first pubspec.yaml under root (depth 1-3) that names Flutter.

    A melos / pub-workspace root usually names no Flutter itself; its apps and
    packages under packages/ or apps/ do. Reading only the root would call a
    Flutter monorepo pure Dart."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else len(Path(rel).parts)
        # example/ and test fixtures of a pure-Dart package are often Flutter
        # apps; they are demos of the package, not members of a workspace.
        dirnames[:] = [] if depth >= 3 else sorted(
            d for d in dirnames if d not in EXCLUDED_DIRS and d not in _NOT_WORKSPACE_MEMBERS)
        if depth and "pubspec.yaml" in filenames:
            path = os.path.join(dirpath, "pubspec.yaml")
            if _pubspec_names_flutter(path):
                return _rel_posix(path, root)
    return None


def _pubspec_is_pure_dart(root: str) -> bool:
    """True only when pubspec.yaml is readable and names Flutter nowhere.

    `dart` (Effective Dart, CLIs/servers/packages) and `flutter` are separate
    stacks in the corpus. An unreadable pubspec keeps the pre-dart answer,
    `flutter`, so a read error never re-routes a Flutter app's rules."""
    return _pubspec_names_flutter(os.path.join(root, "pubspec.yaml")) is False


def _detect_base_signals(root: str, stacks: dict) -> None:
    tsconfig = _find_tsconfig(root)
    if tsconfig is not None:
        # Only a tsconfig.json at the root declares the project. Any nested one
        # (tools/x/, scripts/, or a package dir when the root has none) is
        # reported at low confidence so the card shows it and the person decides
        # (real-repo UAT of 2026-09-04, finding F-3: a Flutter app got a
        # root-wide typescript rule from tools/firestore_observer/tsconfig.json).
        nested = Path(tsconfig).parent != Path(".")
        _add(stacks, "typescript", "low" if nested else "high", tsconfig)

    if os.path.isfile(os.path.join(root, "pubspec.yaml")):
        pure = _pubspec_is_pure_dart(root)
        nested = _nested_flutter_pubspec(root) if pure else None
        if not pure:
            _add(stacks, "flutter", "high", "pubspec.yaml")
        elif nested:
            _add(stacks, "flutter", "high", f"pubspec.yaml (workspace member {nested})")
        else:
            _add(stacks, "dart", "high", "pubspec.yaml")

    # A bare functions/ directory is not evidence of Firebase Functions — the
    # name is common. Require firebase.json, or a manifest inside functions/.
    if os.path.isfile(os.path.join(root, "firebase.json")):
        # firebase.json alone can be hosting, firestore rules or emulators;
        # the functions stack is medium only when a functions/ dir exists.
        # A `functions` key in firebase.json declares the codebase even when
        # its source dir is not named functions/ ("source": "api").
        has_functions_dir = os.path.isdir(os.path.join(root, "functions"))
        declares_functions = False
        try:
            with open(os.path.join(root, "firebase.json"), encoding="utf-8", errors="replace") as f:
                fb = json.load(f)
            declares_functions = isinstance(fb, dict) and "functions" in fb
        except (OSError, json.JSONDecodeError):
            pass
        _add(
            stacks, "functions",
            "medium" if (has_functions_dir or declares_functions) else "low",
            "firebase.json",
        )
    if os.path.isfile(os.path.join(root, "functions", "package.json")):
        _add(stacks, "functions", "medium", "functions/package.json")

    if os.path.isfile(os.path.join(root, "go.mod")):
        _add(stacks, "go", "high", "go.mod")

    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        _add(stacks, "rust", "high", "Cargo.toml")

    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if os.path.isfile(os.path.join(root, name)):
            _add(stacks, "jvm", "high", name)


def _sort_key(name: str) -> int:
    try:
        return STACK_ORDER.index(name)
    except ValueError:
        return len(STACK_ORDER)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit detected stacks (base languages + frameworks) with confidence and evidence.",
    )
    parser.add_argument("root", nargs="?", help="path to the candidate repository root")
    parser.add_argument(
        "--list-served",
        action="store_true",
        help="print the served stacks and their catalog skill pair instead of detecting; "
        "no root is needed",
    )
    args = parser.parse_args()

    if args.list_served:
        served = [
            {"name": name, "skills": SERVED_SKILLS.get(name, [])}
            for name in SERVED_STACKS
        ]
        print(json.dumps({"served": served}))
        with_skills = sum(1 for s in served if s["skills"])
        print(
            f"{len(served)} served stack(s), {with_skills} with a catalog skill pair",
            file=sys.stderr,
        )
        return 0

    if args.root is None:
        print("error: root is required unless --list-served is given", file=sys.stderr)
        return 2

    root_arg = args.root
    if not os.path.isdir(root_arg):
        print(f"error: {root_arg} is not a directory", file=sys.stderr)
        return 2

    root_real = os.path.realpath(root_arg)

    stacks: dict = {}
    _detect_python(root_real, stacks)
    _detect_node(root_real, stacks)
    _detect_base_signals(root_real, stacks)

    # Framework evidence outranks a missing root tsconfig: a pnpm/turbo
    # workspace keeps tsconfig.json under packages/*, and a nestjs/react
    # signal read from the root package.json says the language is the
    # project's, not a helper's.
    ts = stacks.get("typescript")
    if ts is not None and ts["confidence"] == "low":
        for fw in ("nestjs", "react"):
            if fw in stacks and stacks[fw]["confidence"] == "high":
                _add(stacks, "typescript", "medium", f"{fw} evidence: {stacks[fw]['evidence'][0]}")
                break
    # A workspace root explains a nested tsconfig the same way: pnpm/turbo/
    # lerna/nx (or a package.json `workspaces` key) keep every tsconfig under
    # packages/*, so the language is the project's, not a helper's (HN-09).
    if ts is not None and ts["confidence"] == "low":
        workspace_file = _workspace_marker(root_real)
        if workspace_file is not None:
            _add(stacks, "typescript", "medium", f"workspace: {workspace_file}")

    # `default` is the machine value SKILL.md lifts into STACKS[]: a low-
    # confidence stack is shown on the card but installs only if the person
    # adds it — prose alone enforced nothing (adversarial review, 2026-09-05).
    for entry in stacks.values():
        entry["default"] = entry["confidence"] != "low"

    ordered = sorted(stacks.values(), key=lambda entry: _sort_key(entry["name"]))
    payload = {"stacks": ordered}

    print(json.dumps(payload))
    print(f"{len(ordered)} stack(s) detected", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
