#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Resolve the pinned somnio-ai-tools rules corpus for /harness-init.

Read-only: this script writes nothing, anywhere. It never clones, fetches,
checks out, or runs either write the corpus forbids — the CLI's rules installer
and the adapter generator under agent-rules/scripts/ (ADR-0003 D-1/D-2). The
only subprocess it runs is `git rev-parse HEAD`
against an already present checkout.

Usage:
    uv run resolve_rules_source.py [--plugin-root DIR] [--allow-working-copy] [--json]

stdout: on success one JSON object; on any error, nothing.
stderr: on success one human summary line (suppressed by `--json`); on error
        one line beginning "error:".
exit:   0 resolved · 2 cannot read the pin · 3 hard gate (no tier resolved)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# _common.py ships beside this script and owns the git runner.
# Importing a sibling module byte-compiles it unless this is set: a plugin
# cache (or a rules source) is read-only by contract, and a __pycache__/ dir
# appearing beside these scripts is a write nobody asked for. The env var
# PYTHONDONTWRITEBYTECODE=1 on every SKILL.md invocation covers the
# interpreter; this flag covers the import below when it is not set.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import _run_git  # noqa: E402

PIN_RE = re.compile(r'^SOMNIO_AI_TOOLS_COMMIT="([0-9a-f]{40})"')   # mirrors doctor.sh's grep -E
RULES_SUBPATH = "agent-rules/rules"      # the existence probe, per install.sh 1d and doctor.sh
OWNED_TEMPLATE = ".somnio/somnio-ai-tools-{pin}"          # ADR-0003 D-6
PUB_CACHE_TEMPLATE = ".pub-cache/git/somnio-ai-tools-{pin}"
SIBLING_NAME = "somnio-ai-tools"


def _plugin_root(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[3] / "plugins" / "somnio-engineering-ai"


def _read_pin(plugin_root: Path) -> str | None:
    try:
        with open(plugin_root / "scripts" / "install.sh", encoding="utf-8", errors="replace") as f:
            for line in f:
                match = PIN_RE.match(line)
                if match:
                    return match.group(1)
    except OSError:
        return None
    return None


def _candidates(pin: str, plugin_root: Path, allow_working_copy: bool) -> list[tuple[str, Path]]:
    home = Path.home()
    candidates: list[tuple[str, Path]] = []

    env_root = os.environ.get("SOMNIO_AI_TOOLS_ROOT")
    if env_root:
        candidates.append(("env", Path(env_root)))

    candidates.append(("owned-clone", home / OWNED_TEMPLATE.format(pin=pin)))
    candidates.append(("pub-cache", home / PUB_CACHE_TEMPLATE.format(pin=pin)))

    if allow_working_copy:
        repo_root = plugin_root.parent.parent
        candidates.append(("working-copy", repo_root.parent / SIBLING_NAME))

    return candidates


def _has_rules(root: Path) -> bool:
    return (root / RULES_SUBPATH).is_dir()


def _rev(root: Path) -> str | None:
    result = _run_git(str(root), "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    rev = result.stdout.strip()
    return rev if rev else None


def _stacks(root: Path) -> list[str]:
    return sorted(p.name for p in (root / RULES_SUBPATH).iterdir() if p.is_dir())


def _rules_files(root: Path) -> int:
    return sum(1 for _ in (root / RULES_SUBPATH).rglob("*.md"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the pinned somnio-ai-tools rules corpus across four tiers.",
    )
    parser.add_argument("--plugin-root", default=None, help="override the plugin root directory")
    parser.add_argument("--allow-working-copy", action="store_true",
                         help="also consider the working-copy sibling tier (requires confirmation upstream)")
    parser.add_argument("--json", action="store_true", help="suppress the human summary line on stderr")
    args = parser.parse_args()

    plugin_root = _plugin_root(args.plugin_root)

    pin = _read_pin(plugin_root)
    if pin is None:
        install_sh = plugin_root / "scripts" / "install.sh"
        if not install_sh.is_file():
            print(f"error: cannot read {install_sh}", file=sys.stderr)
        else:
            print(f"error: no SOMNIO_AI_TOOLS_COMMIT pin in {install_sh}", file=sys.stderr)
        return 2

    for tier, root in _candidates(pin, plugin_root, args.allow_working_copy):
        if not _has_rules(root):
            continue

        resolved_root = os.path.realpath(root)
        rev = _rev(Path(resolved_root))
        at_pin = rev == pin
        stacks = _stacks(Path(resolved_root))
        rules_files = _rules_files(Path(resolved_root))

        payload = {
            "tier": tier,
            "root": resolved_root,
            "pin": pin,
            "rev": rev,
            "at_pin": at_pin,
            "stacks": stacks,
            "rules_files": rules_files,
        }

        print(json.dumps(payload))
        if not args.json:
            suffix = ""
            if tier == "pub-cache":
                suffix = " (fallback)"
            elif tier == "working-copy":
                suffix = " — may be stale"
            rev_display = rev if rev else "None"
            print(
                f"{tier}{suffix}: {len(stacks)} stacks, {rules_files} rule files at "
                f"{resolved_root} (rev {rev_display}, at_pin={str(at_pin).lower()})",
                file=sys.stderr,
            )
        return 0

    print(
        f"error: somnio-ai-tools rules source not found — run /somnio-engineering-ai:install "
        f"to clone somnio-ai-tools at {pin[:7]} (ADR-0003 D-4)",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
