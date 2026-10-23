#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0,<7"]
# ///
"""The acceptance gate: no acceptance criterion reaches a PR without a status.

WHY THIS EXISTS. PRs were merged with acceptance criteria nobody had checked,
including ones that could have been verified on the dev's machine or against
develop before the PR opened (reported 2026-10-21). Not every criterion can be met
when a PR opens -- some need a deploy, production, a device or another ticket --
and that is fine. What is not fine is a criterion nobody looked at. So the gate
does not demand that everything passes; it demands that nothing is IGNORED:

  * every criterion ends PASS (with evidence a script can check) or DEFERRED
    (with a reason from a closed list and when it will be verified);
  * a criterion that can be verified before the PR (tier local or device) may be
    deferred only with the name of the person who agreed to it;
  * PENDING (never looked at) and FAIL stop the ship.

The verdict is graded against the worktree, never against the matrix's claims
about itself (business rule 4): a PASS whose evidence file does not exist is not
a PASS, and a `command` is RUN by the gate in the worktree -- the matrix's `exit`
is ignored -- and only when it is one of the repository's own gate commands
(execution_config.py's GATE_*), so `true` proves nothing. A PASS needs at least
one command or file; a test name and a URL only corroborate.

Limit, stated rather than implied: `--body` is the ticket text the session saved
in Step 1. The gate defends against edits to the matrix, not to that saved copy.

    uv run acceptance_gate.py extract --body <ticket.md> --out <matrix.json>
    uv run acceptance_gate.py grade   --matrix <matrix.json> --body <ticket.md> --root <worktree> [--json]
    uv run acceptance_gate.py render  --matrix <matrix.json>

extract  reads `## Acceptance Criteria` (ids `AC-n` when present), the `## Tests`
         lines that name an AC (local evidence) and the `## UAT` rows (their tier
         and the ACs they mention) into a matrix whose rows all start PENDING.
grade    prints `ACCEPTANCE ok met=<n> deferred=<n>` or one `ACCEPTANCE refuse
         <reason> <ids>` line per problem.
render   prints the PR-body section: met, deferred (why, when, who agreed).

Matrix row: {"id", "text", "tiers": [...], "hints": [...], "status":
PENDING|PASS|DEFERRED|FAIL, "evidence": [...], "defer": {...}|null}.
Evidence item: {"kind": "command", "command": "<a GATE_* command>"} ·
{"kind": "file", "path": "<relative to the worktree>"} ·
{"kind": "test", "path": "<test file>", "name": "<test title>"} ·
{"kind": "url", "url": "<PR comment, artifact, UAT report>"}.
Defer: {"reason": <one of DEFER_REASONS>, "when": "<text>", "confirmed_by": "<person>"}.

exit: 0 ok / written · 1 refused · 2 usage or unreadable input
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

TIERS = ("local", "device", "develop", "production", "manual", "unmapped")
# Tiers a dev can verify before the PR opens. Deferring one of these is a choice
# a person makes, on the record.
PRE_PR_TIERS = {"local", "device", "unmapped"}
DEFER_REASONS = ("needs-deploy", "needs-production", "needs-device", "blocked-by-dependency",
                 "pending-decision", "manual-qa")
# Reasons that are facts about the world, not choices: a blocker that has not
# shipped, a decision that is not made. They need no confirmation even on a local row.
_FACT_REASONS = {"blocked-by-dependency", "pending-decision"}
STATUSES = ("PENDING", "PASS", "DEFERRED", "FAIL")

_AC_HEAD = re.compile(r"^#{1,4}\s*(acceptance criteria|criterios de aceptaci[oó]n)\b", re.I)
_TESTS_HEAD = re.compile(r"^#{1,4}\s*tests?\b", re.I)
_UAT_HEAD = re.compile(r"^#{1,4}\s*uat\b", re.I)
_ANY_HEAD = re.compile(r"^#{1,2}\s+\S")
_UAT_ROW = re.compile(r"^#{2,4}\s*(UAT-\d+)\b(.*)$", re.I)
_AC_ID = re.compile(r"\bAC-(\d+)\b", re.I)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+)$")


def tier_of(text: str) -> str:
    t = text.lower()
    if re.search(r"\bt2\b|\blive\b|production|\bprod\b", t):
        return "production"
    # Device before develop: a T3 row runs the app on a simulator AGAINST acme-dev,
    # and it is verifiable before the PR; reading it as develop would let it be
    # deferred with no one's word.
    if re.search(r"\bt3\b|simulator|emulator|agent-device|\bdevice\b", t):
        return "device"
    if re.search(r"t1a-dev|\bt1b\b|acme-dev|\bdevelop\b|\bstaging\b|\bdev env", t):
        return "develop"
    if re.search(r"\bt0\b|\bt1a\b|\blocal\b|unit|integration|inject\(|tests?/", t):
        return "local"
    if re.search(r"\bmanual\b|\bqa\b", t):
        return "manual"
    return "unmapped"


def _sections(lines: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"ac": [], "tests": [], "uat": []}
    cur = None
    for ln in lines:
        if _AC_HEAD.match(ln):
            cur = "ac"
            continue
        if _TESTS_HEAD.match(ln):
            cur = "tests"
            continue
        if _UAT_HEAD.match(ln) and not _UAT_ROW.match(ln):
            cur = "uat"
            continue
        if _ANY_HEAD.match(ln) and not _UAT_ROW.match(ln):
            cur = None
            continue
        if cur:
            out[cur].append(ln)
    return out


def extract(body: str) -> list[dict]:
    sec = _sections(body.splitlines())
    rows: list[dict] = []
    for ln in sec["ac"]:
        # Top-level items only: an indented sub-bullet belongs to the criterion above
        # it. Counting it as a criterion of its own shifted every later auto id.
        m = _ITEM.match(ln) if not ln.startswith((" ", "\t")) else None
        if not m:
            if rows and ln.strip():
                rows[-1]["text"] += " " + ln.strip().lstrip("-*+ ")
            continue
        text = m.group(1).strip()
        idm = re.match(r"^\**\s*AC-(\d+)\b\**\s*[—:.\-–]*\s*", text, re.I)
        rows.append({"id": f"AC-{idm.group(1)}" if idm else None, "text": text, "tiers": [],
                     "hints": [], "status": "PENDING", "evidence": [], "defer": None})
    used = {r["id"] for r in rows if r["id"]}
    n = 0
    for r in rows:  # an unnumbered criterion takes the next id no other criterion holds
        if r["id"] is None:
            n += 1
            while f"AC-{n}" in used:
                n += 1
            r["id"] = f"AC-{n}"
            used.add(r["id"])
    by_id = {r["id"].upper(): r for r in rows}
    for ln in sec["tests"]:
        for n in _AC_ID.findall(ln):
            r = by_id.get(f"AC-{n}")
            if r is not None:
                r["hints"].append(("tests: " + ln.strip().lstrip("-* "))[:200])
                if "local" not in r["tiers"]:
                    r["tiers"].append("local")
    block: list[str] = []
    for ln in sec["uat"] + ["## UAT-0 end"]:
        if _UAT_ROW.match(ln):
            if block:
                head, text = block[0], " ".join(block)
                tier = tier_of(head)
                for n in sorted(set(_AC_ID.findall(text))):
                    r = by_id.get(f"AC-{n}")
                    if r is not None:
                        r["hints"].append(("uat: " + head.lstrip("# ").strip())[:200])
                        if tier not in r["tiers"]:
                            r["tiers"].append(tier)
            block = [ln]
        elif block:
            block.append(ln.strip())
    for r in rows:
        if not r["tiers"]:
            r["tiers"] = [tier_of(r["text"])]
    return rows


def _gate_commands(root: Path) -> set[str]:
    """The repository's own gate commands, read by the one reader. A command
    evidence item is accepted only when it is one of these."""
    try:
        import execution_config  # noqa: PLC0415 - sibling script, loaded on demand
        r, _problem = execution_config.resolve(root.resolve(), None)
    except Exception:  # noqa: BLE001 - no reader, no accepted command
        return set()
    return {c for c in ((r or {}).get("gate_commands") or {}).values() if c and c != "GAP"}


class _Runner:
    """Runs each accepted command once, in the worktree. The exit the matrix
    claims is never read."""

    def __init__(self, root: Path):
        self.root, self.allowed, self.done = root, None, {}

    def check(self, cmd: str) -> str | None:
        if self.allowed is None:
            self.allowed = _gate_commands(self.root)
        if cmd not in self.allowed:
            return (f"`{cmd}` is not one of this repository's gate commands "
                    f"(execution_config.py GATE_*); run one of those, or cite a file")
        if cmd not in self.done:
            print(f"acceptance-gate: running `{cmd}` in {self.root}", file=sys.stderr)
            try:
                rc = subprocess.run(cmd, shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=3600).returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            self.done[cmd] = rc
        rc = self.done[cmd]
        return None if rc == 0 else f"`{cmd}` exited {rc} when the gate ran it, not 0"


def _evidence_ok(item: object, root: Path, runner: _Runner) -> str | None:
    if not isinstance(item, dict):
        return "evidence item is not an object"
    kind = item.get("kind")
    if kind == "command":
        cmd = str(item.get("command") or "").strip()
        if not cmd:
            return "command evidence names no command"
        return runner.check(cmd)
    if kind in ("file", "test"):
        p = str(item.get("path") or "")
        if not p or p.startswith("/") or ".." in Path(p).parts:
            return f"{kind} evidence path {p!r} must be relative to the worktree"
        if not (root / p).is_file():
            return f"{kind} evidence {p} is not a file in the worktree"
        if kind == "test":
            name = str(item.get("name") or "").strip()
            if not name:
                return f"test evidence {p} names no test"
            try:
                if name not in (root / p).read_text(encoding="utf-8", errors="replace"):
                    return f"test {name!r} is not in {p}"
            except OSError as e:
                return f"test evidence {p} is unreadable ({e})"
        return None
    if kind == "url":
        return None if re.match(r"^https?://\S+$", str(item.get("url") or "")) \
            else "url evidence is not an http(s) URL"
    return f"evidence kind {kind!r} is not command, file, test or url"


def grade(rows: list[dict], root: Path, body: str) -> list[tuple[str, str]]:
    """[(reason, detail)] — empty when the matrix passes.

    `body` is the ticket itself, re-read: the matrix is written by the session
    being graded, so the criteria and their pre-PR tiers come from the ticket,
    never from the matrix's own claims. A criterion dropped from the matrix is the
    cheapest way to ignore one, and a tier relabelled from local to develop the
    cheapest way to skip the confirmation."""
    problems: list[tuple[str, str]] = []
    extracted = extract(body)
    source = {r["id"]: r for r in extracted}
    if len(source) != len(extracted):
        problems.append(("duplicate-id", "the ticket numbers two criteria the same; "
                                         "renumber them in the ticket"))
    runner = _Runner(root)
    if not source:
        return [("no-criteria", "the ticket has no acceptance criteria section; a ticket "
                                "without AC is not a hardened spec")]
    ids = {str(r.get("id")) for r in rows}
    for rid in source:
        if rid not in ids:
            problems.append(("missing", f"{rid} is in the ticket and not in the matrix: "
                                        f"{source[rid]['text'][:80]}"))
    seen: set[str] = set()
    for r in rows:
        rid = str(r.get("id") or "?")
        if rid in seen:
            problems.append(("duplicate-id", rid))
        seen.add(rid)
        status = r.get("status")
        tiers = set(r.get("tiers") or []) | set((source.get(rid) or {}).get("tiers") or [])
        if rid not in source:
            problems.append(("unknown-id", f"{rid} is not a criterion of the ticket"))
        if status not in STATUSES:
            problems.append(("bad-status", f"{rid} status {status!r}"))
        elif status == "PENDING":
            problems.append(("ignored", f"{rid} was never checked: {str(r.get('text'))[:80]}"))
        elif status == "FAIL":
            problems.append(("failed", f"{rid} fails: {str(r.get('text'))[:80]}"))
        elif status == "PASS":
            ev = r.get("evidence") or []
            if not any(isinstance(e, dict) and e.get("kind") in ("command", "file") for e in ev):
                problems.append(("no-evidence", f"{rid} is PASS with no command or file the "
                                                "gate can check (a test name or URL only "
                                                "corroborates)"))
            for item in ev:
                why = _evidence_ok(item, root, runner)
                if why:
                    problems.append(("bad-evidence", f"{rid}: {why}"))
        elif status == "DEFERRED":
            d = r.get("defer") if isinstance(r.get("defer"), dict) else {}
            reason = d.get("reason")
            if reason not in DEFER_REASONS:
                problems.append(("bad-defer", f"{rid} defer reason {reason!r} is not one of "
                                              + ", ".join(DEFER_REASONS)))
                continue
            if not str(d.get("when") or "").strip():
                problems.append(("bad-defer", f"{rid} is deferred without saying when it "
                                              "will be verified"))
            if tiers & PRE_PR_TIERS and reason not in _FACT_REASONS \
                    and not str(d.get("confirmed_by") or "").strip():
                problems.append(("unconfirmed-defer",
                                 f"{rid} can be verified before the PR ({'/'.join(sorted(tiers & PRE_PR_TIERS))}) "
                                 f"and is deferred ({reason}) with no confirmed_by"))
    return problems


def render(rows: list[dict]) -> str:
    met = [r for r in rows if r.get("status") == "PASS"]
    deferred = [r for r in rows if r.get("status") == "DEFERRED"]
    out = ["## Acceptance criteria", ""]
    out.append(f"{len(met)} of {len(rows)} met at this head; {len(deferred)} deferred.")
    if met:
        out += ["", "| AC | Criterion | Evidence |", "|:--|:--|:--|"]
        for r in met:
            ev = "; ".join(
                (f"`{e.get('command')}` (run by the gate, exit 0)" if e.get("kind") == "command" else
                 f"`{e.get('path')}`" + (f" — {e.get('name')}" if e.get("kind") == "test" else "")
                 if e.get("kind") in ("file", "test") else str(e.get("url")))
                for e in r.get("evidence") or [])
            out.append(f"| {r['id']} | {_cell(r['text'])} | {_cell(ev)} |")
    if deferred:
        out += ["", "Not met yet — each is verified later, as stated:", "",
                "| AC | Criterion | Why not now | When | Agreed by |", "|:--|:--|:--|:--|:--|"]
        for r in deferred:
            d = r.get("defer") or {}
            out.append(f"| {r['id']} | {_cell(r['text'])} | {d.get('reason')} | "
                       f"{_cell(str(d.get('when') or ''))} | {d.get('confirmed_by') or '—'} |")
    return "\n".join(out) + "\n"


def _cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")[:160]


def _read_matrix(p: str) -> list[dict] | None:
    try:
        obj = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"acceptance-gate: {p} is unreadable ({e})", file=sys.stderr)
        return None
    rows = obj.get("criteria") if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        print(f"acceptance-gate: {p} holds no list of criteria", file=sys.stderr)
        return None
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--body", required=True)
    e.add_argument("--out", required=True)
    g = sub.add_parser("grade")
    g.add_argument("--matrix", required=True)
    g.add_argument("--root", required=True)
    g.add_argument("--body", required=True, help="the ticket body the matrix was extracted from")
    g.add_argument("--json", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--matrix", required=True)
    a = ap.parse_args()

    if a.cmd == "extract":
        try:
            body = Path(a.body).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"acceptance-gate: {a.body} is unreadable ({exc})", file=sys.stderr)
            return 2
        rows = extract(body)
        Path(a.out).write_text(json.dumps({"criteria": rows}, indent=2) + "\n", encoding="utf-8")
        tiers: dict[str, int] = {}
        for row in rows:
            for t in row["tiers"]:
                tiers[t] = tiers.get(t, 0) + 1
        print(f"extracted {len(rows)} criteria → {a.out} "
              f"({', '.join(f'{k}={v}' for k, v in sorted(tiers.items())) or 'none'})")
        return 0 if rows else 1

    rows = _read_matrix(a.matrix)
    if rows is None:
        return 2
    if a.cmd == "render":
        sys.stdout.write(render(rows))
        return 0
    root = Path(a.root)
    if not root.is_dir():
        print(f"acceptance-gate: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        body = Path(a.body).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"acceptance-gate: {a.body} is unreadable ({exc})", file=sys.stderr)
        return 2
    problems = grade(rows, root, body)
    if a.json:
        print(json.dumps({"ok": not problems,
                          "problems": [{"reason": p, "detail": d} for p, d in problems]}))
    elif problems:
        for p, d in problems:
            print(f"ACCEPTANCE refuse {p} {d}")
    else:
        met = sum(1 for x in rows if x.get("status") == "PASS")
        print(f"ACCEPTANCE ok met={met} deferred={len(rows) - met}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
