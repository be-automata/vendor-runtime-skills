#!/usr/bin/env python3
"""Deep-merge two Claude Code settings JSON documents.

    merge-settings.py BASE OVERLAY

Prints the merged JSON to stdout. BASE and OVERLAY are file paths; either may
be "-" to read from stdin (only one may be "-"). A missing, empty, or invalid
file is treated as {} so the merge never crashes on a half-written settings
file — the other side is used as-is.

Merge rule (one rule, applied recursively):
  - dict + dict  -> keys unioned; shared keys merged recursively
  - list + list  -> overlay entries first, then base entries not already present
                    (a union that preserves the repo's own additions and
                    appends whatever the base contributes, e.g. deny globs)
  - type clash where base is a dict/list -> base wins (the committed/authored
    shape beats a malformed value on the overlay side)
  - anything else -> overlay wins (a scalar the overlay set is an explicit choice)

Two callers share this:
  - project-init.sh: base = committed HEAD settings, overlay = ruflo init output,
    so the repo's committed posture survives init's wholesale replace.
  - apply-settings.sh: base = the shipped target-settings template, overlay =
    the repo's existing settings, so the template is laid down without
    clobbering values the repo already tuned.
  (harness-init's settings step calls apply-settings.sh, so it shares the rule.)

Post-merge normalization of permission rules (applied once, after the merge):
  Claude Code >= 2.1.261 matches file-editing tools against `Edit(<path>)`
  rules only; a `Write(<path>)` rule is dead and the CLI prints one warning per
  entry at every session start (observed 2026-09-06 on three real target repos):

    Permission deny rule (.claude/settings.json): Write(**/.env) is not matched
    by file permission checks — only Edit(path) rules are. Use Edit(**/.env)
    instead (Edit rules cover all file-editing tools).

  Because the list rule above is a union with the overlay winning, an old
  `Write(X)` entry in a repo's settings would survive every merge. So every
  `permissions.allow`, `permissions.deny` and `permissions.ask` list is
  normalized: `Write(X)` is dropped when `Edit(X)` is already in the same list,
  otherwise it is rewritten to `Edit(X)` (same intent; Edit covers every editing
  tool), deduplicating when the rewrite collides with a later entry. Order is
  otherwise preserved; non-`Write(` strings and non-string entries are left
  untouched; lists outside `permissions` are never touched.
"""
import json
import sys


def merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = {k: merge(v, over[k]) if k in over else v for k, v in base.items()}
        out.update({k: v for k, v in over.items() if k not in base})
        return out
    if isinstance(base, list) and isinstance(over, list):
        return over + [x for x in base if x not in over]
    if isinstance(base, (dict, list)) and not isinstance(over, type(base)):
        return base
    return over


PERMISSION_LISTS = ("allow", "deny", "ask")


def normalize_write_rules(rules):
    """Rewrite dead Write(X) permission rules to Edit(X) inside one list."""
    if not isinstance(rules, list):
        return rules
    out = []
    for r in rules:
        if isinstance(r, str) and r.startswith("Write(") and r.endswith(")"):
            r = "Edit(" + r[len("Write("):]
            # Edit twin already in the list (earlier, later, or produced by a
            # previous rewrite): drop the Write and keep the twin's position.
            if r in rules or r in out:
                continue
        out.append(r)
    return out


def normalize_permissions(doc):
    perms = doc.get("permissions") if isinstance(doc, dict) else None
    if not isinstance(perms, dict):
        return doc
    for key in PERMISSION_LISTS:
        if isinstance(perms.get(key), list):
            perms[key] = normalize_write_rules(perms[key])
    return doc


def load(path):
    try:
        text = sys.stdin.read() if path == "-" else open(path).read()
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: merge-settings.py BASE OVERLAY\n")
        return 2
    if argv[1] == "-" and argv[2] == "-":
        sys.stderr.write("merge-settings.py: only one argument may be '-'\n")
        return 2
    merged = normalize_permissions(merge(load(argv[1]), load(argv[2])))
    json.dump(merged, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
