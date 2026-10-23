#!/usr/bin/env python3
"""Extract the one argument ruflo-hook.sh passes on, from a Claude Code hook payload.

Usage: ruflo-hook-arg.py <command|file_path>   (payload JSON on stdin)

`file_path` prints tool_input.file_path. `command` prints a ROUTING SUMMARY of
tool_input.command, never the command: the program's basename, plus its
subcommand when the program is a known subcommand-style CLI (`git commit`,
`npm run`, `docker compose`). No argument, flag or value is ever printed.

Why a summary and not redaction: the value lands in ruflo's argv (readable by
`ps`) and in its learning store under .claude-flow/ in a client repository. A
pattern-based redactor was broken twice in review by ordinary commands
(`tool --verbose --password x`, `sshpass -p x`, a token-only URL). A summary
that carries no arguments has nothing to leak. Prints nothing when there is
nothing to summarise; exit 0 always. Stdlib only.
"""
import json
import os
import re
import shlex
import sys

# CLIs whose second word is a subcommand, not user data. Anything else is
# summarised as the program alone.
SUBCOMMAND_CLIS = {
    "git", "gh", "npm", "pnpm", "yarn", "bun", "deno", "npx", "uv", "pip", "pip3",
    "poetry", "cargo", "go", "dart", "flutter", "fvm", "melos", "docker", "podman",
    "kubectl", "helm", "terraform", "gcloud", "aws", "az", "firebase", "make",
    "brew", "ruflo", "roborev", "somnio", "claude", "agent-device", "xcrun",
}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9:-]{0,31}$")


def summarize(command: str) -> str:
    # Only the first simple command: stop at the first separator or pipe.
    first = re.split(r"\s*(?:&&|\|\||[;|&\n])\s*", command.strip(), maxsplit=1)[0]
    try:
        words = shlex.split(first)
    except ValueError:  # unbalanced quotes: fall back to whitespace
        words = first.split()
    while words and _ASSIGN.match(words[0]):  # FOO=bar cmd -> cmd
        words.pop(0)
    if not words:
        return ""
    program = os.path.basename(words[0])
    if not _PROGRAM.match(program):
        return ""
    if program in SUBCOMMAND_CLIS and len(words) > 1 and _SUBCOMMAND.match(words[1]):
        return f"{program} {words[1]}"
    return program


def main() -> int:
    field = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        ti = json.load(sys.stdin).get("tool_input") or {}
    except Exception:
        ti = {}
    value = ti.get(field) if isinstance(ti, dict) else None
    if not isinstance(value, str):
        return 0
    sys.stdout.write(summarize(value) if field == "command" else value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
