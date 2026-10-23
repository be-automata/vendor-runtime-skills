#!/usr/bin/env python3
"""Keep client names out of this public repository without writing them down.

`banned-tokens.json` maps sha256(lowercased token) to a neutral placeholder.
A token is a run of ASCII letters and digits, so "name-core", "name.example.cloud"
and "NAME-12" all yield the token "name". The names themselves appear nowhere
in the repository, so they cannot be searched or indexed from it.

  scrub.py --check DIR...  list every hit (by path and line, never the token)
                           and exit 1 when there is one
  scrub.py --fix DIR       delete files whose path holds a banned token, drop
                           every line that names a deleted file, then replace
                           the remaining tokens with their placeholders
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z0-9]+")
BANNED_FILE = Path(__file__).resolve().parent / "banned-tokens.json"


def load_banned() -> dict[str, str]:
    with open(BANNED_FILE, encoding="utf-8") as f:
        return json.load(f)["tokens"]


def digest(token: str) -> str:
    return hashlib.sha256(token.lower().encode("ascii")).hexdigest()


def placeholder(token: str, base: str) -> str:
    if len(token) > 1 and token.isupper():
        return base.upper()
    if token[0].isupper():
        return base[0].upper() + base[1:]
    return base


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            yield Path(dirpath, name)


def read_text(path: Path) -> str | None:
    if path.is_symlink():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def has_banned(text: str, banned: dict[str, str]) -> bool:
    return any(digest(t) in banned for t in TOKEN.findall(text))


def check(roots: list[Path], banned: dict[str, str]) -> int:
    hits = 0
    for root in roots:
        for path in walk(root):
            rel = path.relative_to(root).as_posix()
            if has_banned(rel, banned):
                print(f"{root.name}/{rel}: banned token in the path")
                hits += 1
            text = read_text(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if has_banned(line, banned):
                    print(f"{root.name}/{rel}:{number}: banned token")
                    hits += 1
    return hits


def fix(root: Path, banned: dict[str, str]) -> None:
    removed = []
    for path in list(walk(root)):
        if has_banned(path.relative_to(root).as_posix(), banned):
            removed.append(path.name)
            path.unlink()

    def swap(match: re.Match) -> str:
        token = match.group()
        base = banned.get(digest(token))
        return token if base is None else placeholder(token, base)

    for path in walk(root):
        text = read_text(path)
        if text is None:
            continue
        lines = text.splitlines(keepends=True)
        kept = [line for line in lines if not any(name in line for name in removed)]
        new = TOKEN.sub(swap, "".join(kept))
        if new != text:
            path.write_text(new, encoding="utf-8")
    for name in removed:
        print(f"removed a file named with a banned token ({len(name)} chars)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--fix", action="store_true")
    parser.add_argument("dirs", nargs="+", type=Path)
    args = parser.parse_args()
    banned = load_banned()
    if args.fix:
        if len(args.dirs) != 1:
            parser.error("--fix takes exactly one directory")
        fix(args.dirs[0], banned)
        return 0
    hits = check(args.dirs, banned)
    if hits:
        print(f"scrub: {hits} hit(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
