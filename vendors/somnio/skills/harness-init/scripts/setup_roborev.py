#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Per-repository roborev setup for /harness-init (ENG-REV-05, ADR-0005).

WHY THIS EXISTS. `/somnio-engineering-ai:install` puts the pinned roborev
binary on the machine. Nothing put it to work in a *repository*: every step
below was done by hand in the pilot, and each one has a trap that cost a day to
find. A teammate asked to "set up roborev in your repo" would reproduce those
traps one at a time. This script is that knowledge, executable.

THE TRAPS IT ENCODES, each traced to where it was learned:
  1. `roborev agent-hook install` with no --agent writes NINE agent configs
     (Codex, Copilot, Cursor, Gemini, Qwen, Grok, Droid, Hermes, Claude Code).
     Measured 2026-09-18. Always --agent claude.
  2. The post-commit hook must point at the harness GUARD, never the binary, or
     ADR-0005 D-6's [[hooks]] activation blocker is bypassed by the one path
     that matters.
  3. `core.hooksPath` may point at a TRACKED directory (it does in the harness
     repo). roborev honours the setting, so its hooks land as untracked files
     one `git add -A` from being committed — turning a per-machine registration
     into one every developer inherits on clone. Pilot day zero, #45.
  4. A linked worktree does NOT inherit the main checkout's .roborev.toml, and
     upstream's 3s hook timeout is too short there — reviews queue nothing,
     silently. hook_timeout_seconds = 30. Pilot, #45.
  5. `[[hooks]]` is never written by us and is an activation blocker if present.
  6. `agent-hook dump` prints the whole settings file, secrets included. Never
     pipe it anywhere a log will see.
  7. `roborev install-hook` writes THREE hooks: post-commit, post-rewrite and
     pre-push. A repo can already own any of them. Measured on acme-frontend:
     a TRACKED 69KB .githooks/pre-push, inherited by every developer on clone.
     Overwriting that is not a local change — it is a commit against their repo.

THE SUPERSEDE RULE (area decision, 2026-09-18). The harness takes over the hook
path so it works from day one, and the developer keeps what was there:
  * hook ABSENT          -> install ours. Purely additive.
  * present, UNTRACKED   -> back it up beside itself, supersede, and print how to
                            fold the old body back in. Machine-local, reversible.
  * present, TRACKED     -> REFUSE to write. A tracked hook is the team's, and
                            replacing it is a pull request against their
                            repository, not a setup step. Report it with the
                            integration path and carry on.
  * pre-push is stricter, because it is the hook a team is most likely to
    already own (acme-frontend: a 69KB gate running format + tests). It flushes
    a pending post-commit batch so a plain `git push` from outside the execution
    skills does not leave up to N-1 commits unreviewed; ROBOREV LAST still does
    the synchronous flush-and-wait those skills rely on. Ours is installed only
    where nothing better exists (area decision, 2026-09-18):
      - ABSENT                        -> install ours.
      - TRACKED                       -> never written, like any tracked hook.
      - UNTRACKED and REPLACEABLE     -> back up, supersede. Replaceable is
        measurable, never a judgement of quality: the body is empty or only
        `exit 0`/`true`/`:` (it does nothing), or it is exactly roborev's own
        generated template (ours is that same flush, routed through the guard).
      - UNTRACKED and anything else   -> LEFT ALONE, with the one line to add.
        Whether a custom gate is "better" than a flush is not something a script
        can grade; a hook that does real work is assumed to be there on purpose.

WHAT IT REFUSES TO DO. Registration is unattended per-commit egress of this
repository's source to a model provider (ADR-0005 D-1, as narrowed by Q-2: what
it grants is an agent CLI with READ ACCESS TO THE WORKING TREE, not a bounded
diff). That is a person's decision about a client's code, so --activate demands
--who and --payload-ack and writes the record that makes the decision auditable.
Without them the script prepares everything and stops.

Usage:
    setup_roborev.py <repo> --check
    setup_roborev.py <repo> --apply
    setup_roborev.py <repo> --apply --activate --who "<name/email>" --payload-ack

stdout: one JSON object. stderr: human-readable lines.
Exit: 0 ok · 1 error · 2 work to do, or refused (a `refused` key says which).
      Only REPOSITORY findings decide it. A finding with scope=machine (Layer 2 in
      ~/.claude/settings.json) is reported and never turns a registered repo into 2.
      No fourth code: 0/1/2 already mean success/conflicts/nothing-written to
      harness-init's pipeline, and a new meaning would break that gate.
"""
from __future__ import annotations
import argparse, functools, json, os, re, shlex, shutil, subprocess, sys
from pathlib import Path

# Set BEFORE the first local import, and in the script rather than at every call
# site: importing a sibling makes CPython write `scripts/__pycache__/` into the
# plugin, which init-test's P2 lane forbids and which a person running this by
# hand would leave behind too. The other scripts get this from
# PYTHONDONTWRITEBYTECODE=1 in their documented invocation; this one is also run
# bare, so it owns the guarantee.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import classify_review  # noqa: E402

HOOKS_RE = re.compile(r'^[ \t]*\[\[?[ \t]*"?hooks"?[ \t]*\]\]?[ \t]*(#.*)?$', re.M)
ROBOREV_HOOKS = ("post-commit", "post-rewrite", "pre-push")
# The subcommand each hook invokes. `post-rewrite` is NOT a roborev subcommand;
# the hook of that name calls `remap`, which re-attributes findings across a
# rewrite (ADR-0005 Q-6). Both need the widened guard allowlist (PR #73).
HOOK_OPS = {"post-commit": "post-commit", "post-rewrite": "remap --quiet",
            "pre-push": "post-commit --flush-push"}

# A pre-push body we may replace. Lines are compared after stripping comments and
# blank lines. INERT: it does nothing. The TEMPLATE is roborev 0.67.0's generated
# pre-push ("hook v3"), line for line; any other version, or the template wrapped
# around an original body (what `install-hook` does to an existing pre-push), has
# extra lines and is therefore left alone. Conservative on purpose.
INERT_LINES = {"exit 0", "exit", "true", ":"}
ROBOREV_PREPUSH_V3 = [
    re.compile(r'^ROBOREV="[^"$`]*"$'),
    re.compile(r'^if \[ ! -x "\$ROBOREV" \]; then$'),
    re.compile(r'^ROBOREV=\$\(command -v roborev 2>/dev/null\)$'),
    re.compile(r'^\[ -z "\$ROBOREV" \] \|\| \[ ! -x "\$ROBOREV" \] && exit 0$'),
    re.compile(r'^fi$'),
    re.compile(r'^"\$ROBOREV" post-commit --flush-push 2>/dev/null \|\| true$'),
]


def prepush_kind(text: str) -> str:
    """inert | roborev-template | substantive. Only the first two are replaceable."""
    body = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    if all(ln in INERT_LINES for ln in body):
        return "inert"
    if (len(body) == len(ROBOREV_PREPUSH_V3)
            and all(rx.match(ln) for rx, ln in zip(ROBOREV_PREPUSH_V3, body))):
        return "roborev-template"
    return "substantive"


def sh(*args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


PLUGIN_NAME = "somnio-engineering-ai"


def _is_plugin_root(d: Path) -> bool:
    """A plugin root is the directory holding `.claude-plugin/plugin.json`. That
    marker is present in BOTH layouts this script runs from — the source-repo
    checkout and the installed plugin — which is exactly why it, and not a
    directory name, is the test."""
    f = d / ".claude-plugin" / "plugin.json"
    try:
        name = json.loads(f.read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        return False
    # The NAME is checked, not just the marker's presence. The old resolver
    # required a literal `plugins/somnio-engineering-ai/` path segment, so it
    # could not be satisfied by a neighbouring plugin; a marker-only test drops
    # that guarantee, and what this decides is which binary a git hook executes
    # on every commit (ADR-0005 D-6).
    return name == PLUGIN_NAME


def _installed_plugin_roots() -> list[Path]:
    """Plugin roots Claude Code has recorded as installed. Last resort, and the
    reason it exists: CLAUDE_PLUGIN_ROOT is NOT set in an ordinary session, so a
    resolver that depends on it fails on the default path for everyone."""
    out: list[Path] = []
    rows: list[tuple[str, str]] = []
    for home_dir in (".claude", ".claude-icloud"):
        reg = Path.home() / home_dir / "plugins" / "installed_plugins.json"
        try:
            data = json.loads(reg.read_text())
        except (OSError, ValueError):
            continue
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            continue
        for key, installs in plugins.items():
            if str(key).split("@", 1)[0] != PLUGIN_NAME or not isinstance(installs, list):
                continue
            for inst in installs:
                if not isinstance(inst, dict) or not isinstance(inst.get("installPath"), str):
                    continue
                rows.append((str(inst.get("lastUpdated") or ""), inst["installPath"]))
    # Newest first, across BOTH registries: a machine keeps every version it has
    # ever cached, and the guard the hook blesses must be the one the session is
    # running. Sorting per-file put every ~/.claude row ahead of every
    # ~/.claude-icloud row regardless of date.
    seen: set[str] = set()
    for _, path in sorted(rows, reverse=True):
        if path not in seen:
            seen.add(path)
            out.append(Path(path))
    return out


@functools.cache
def plugin_root() -> Path | None:
    """The somnio-engineering-ai plugin root, resolved the way the skill does.

    The bug this replaces (acme-core, 2026-09-20, plugin 0.9.27): the old form
    walked this script's ancestors looking for a CHILD directory
    `plugins/somnio-engineering-ai/`. That shape exists only in the source-repo
    checkout. From the installed plugin the script sits at
    `<root>/skills/harness-init/scripts/` with the guard at `<root>/scripts/`
    and no `plugins/` dir anywhere up the tree — so on the default path the
    guard reported missing and the pin read back empty, for everyone. It only
    ever worked when CLAUDE_PLUGIN_ROOT was exported by hand.

    Order: the session's own answer, then this script's tree (installed layout,
    then checkout layout), then the installed-plugins registry."""
    cands: list[Path] = []
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        cands.append(Path(env))
    here = Path(__file__).resolve()
    for par in here.parents:
        cands.append(par)                                  # installed plugin
        cands.append(par / "plugins" / PLUGIN_NAME)        # source checkout
    for c in cands:
        if _is_plugin_root(c):
            return c.resolve()
    # The registry is read only when the two tiers above missed. Building it
    # eagerly parsed two JSON files on every call, on the common path, for a
    # case that never reaches them there.
    for c in _installed_plugin_roots():
        if _is_plugin_root(c):
            return c.resolve()
    return None


def find_guard() -> Path | None:
    """The harness-owned guard (ADR-0005 D-6), resolved from the plugin root —
    never from PATH, because PATH is the caller's and this decides what the hook
    blesses."""
    root = plugin_root()
    if root is None:
        return None
    c = root / "scripts" / "roborev-guard"
    if c.is_file() and os.access(c, os.X_OK):
        return c.resolve()
    return None


def pinned_roborev_version() -> str:
    """The pin, READ from the installer rather than written as a literal (rule
    15): a literal here would date itself silently, and D-1's record is supposed
    to say which version the approval was given against."""
    root = plugin_root()
    if root is None:
        return ""
    f = root / "scripts" / "install.sh"
    try:
        mt = re.search(r'ROBOREV_VERSION="([^"]+)"', f.read_text(errors="replace"))
    except OSError:
        return ""
    return mt.group(1) if mt else ""


def classify_hook(repo: Path, hooks_dir: Path, name: str) -> dict:
    """absent | untracked | tracked — the three cases the supersede rule splits on."""
    f = hooks_dir / name
    if not f.is_file():
        return {"hook": name, "state": "absent", "path": str(f)}
    try:
        rel = str(f.relative_to(repo))
    except ValueError:
        text = f.read_text(errors="replace")
        out = {"hook": name, "state": "untracked", "path": str(f), "size": f.stat().st_size,
               "ours": "roborev-guard" in text}
        if name == "pre-push":
            out["kind"] = prepush_kind(text)
        return out
    rc, _, _ = sh("git", "ls-files", "--error-unmatch", rel, cwd=repo)
    text = f.read_text(errors="replace")
    out = {"hook": name, "state": "tracked" if rc == 0 else "untracked",
           "path": str(f), "rel": rel, "size": f.stat().st_size,
           "ours": "roborev-guard" in text}
    if name == "pre-push":
        out["kind"] = prepush_kind(text)
    return out


def writable_hooks(repo: Path, hp: str) -> list[str]:
    """The hooks this run may write into a hooks path: all but a TRACKED pre-push,
    which is the team's and is never written (it gets no gitignore entry either —
    an entry cannot help a tracked file, and would read as protection)."""
    pp = f'{hp.rstrip("/")}/pre-push'
    tracked_pp = sh("git", "ls-files", "--error-unmatch", pp, cwd=repo)[0] == 0
    return [h for h in ROBOREV_HOOKS if not (h == "pre-push" and tracked_pp)]


def backup_hook(f: Path) -> Path:
    """Beside itself, timestamped, never overwritten. The developer's copy to
    fold back in — this script never merges it for them, because a hook body is
    theirs to understand before it runs on every commit."""
    import time
    dst = f.with_name(f"{f.name}.pre-somnio.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(f, dst)
    return dst


def audit(repo: Path) -> dict:
    """Read-only. Every finding carries the trap it guards against."""
    out: dict = {"repo": str(repo), "findings": [], "ok": []}

    def bad(key, why, fix, scope="repo"):
        # scope="machine": a fact about this computer (~/.claude), not the
        # repository. Reported, but never part of the repository's verdict or
        # exit code — a complete registration on a machine without Layer 2
        # used to exit 2, which harness-init reads as "nothing written".
        out["findings"].append({"check": key, "problem": why, "fix": fix, "scope": scope})

    def good(key, detail):
        out["ok"].append({"check": key, "detail": detail})

    rc, top, _ = sh("git", "rev-parse", "--show-toplevel", cwd=repo)
    if rc != 0:
        out["error"] = "not a git repository"
        return out

    # The recorded decision is read FIRST, because it changes how every absence
    # below is graded. A repository whose person said "not this one" is not a
    # half-finished registration, and grading it as one would make a declined
    # repo report findings and exit 2 on every run for ever (ADR-0005 D-13).
    decision = read_decision(repo)
    out["decision"] = decision

    cfg = repo / ".roborev.toml"
    if not cfg.is_file():
        if decision == "declined":
            good("config", "no .roborev.toml — registration was declined and recorded")
        else:
            bad("config", ".roborev.toml is absent", "--apply writes one with no [[hooks]]")
    else:
        text = cfg.read_text(errors="replace")
        if HOOKS_RE.search(text):
            bad("hooks-block", ".roborev.toml declares a hooks block — ADR-0005 D-6 "
                "activation blocker; the guard will refuse every enqueue",
                "remove the block by PR; this script never edits it (D-6: detected, never removed)")
        else:
            good("hooks-block", "no [[hooks]] — the guard will pass through")
        if "hook_timeout_seconds" not in text:
            if decision == "declined":
                # A readiness nag about a config this run will never write. The
                # hazard on a declined repo is the LEFTOVER config itself, which
                # the decline-mismatch check below reports instead.
                good("worktree-timeout", "hook_timeout_seconds unset; registration was "
                     "declined, so nothing here queues a review")
            else:
                bad("worktree-timeout", "hook_timeout_seconds unset; upstream's 3s default "
                    "queues nothing from a linked worktree, silently",
                    "--apply sets 30")
        else:
            good("worktree-timeout", "hook_timeout_seconds present")

    # Trap 3: a tracked core.hooksPath
    rc, hp, _ = sh("git", "config", "core.hooksPath", cwd=repo)
    if rc == 0 and hp and decision == "declined":
        # Every tracked-hooks finding is about the hooks THIS RUN WOULD WRITE
        # into that directory. A declined repository gets none, so grading its
        # hooksPath makes the honest answer the one that exits 2 for ever --
        # measured on the `.githooks` shape this script's own trap 3 describes.
        hooks_dir = (repo / hp) if not os.path.isabs(hp) else Path(hp)
        out["hooks_path"] = str(hooks_dir)
        good("tracked-hooks", f"core.hooksPath={hp}; registration was declined, so this run "
             "writes no hook there and needs no gitignore entry")
    elif rc == 0 and hp:
        hooks_dir = (repo / hp) if not os.path.isabs(hp) else Path(hp)
        rc2, tracked, _ = sh("git", "ls-files", "--error-unmatch", str(hooks_dir.relative_to(repo))
                             if str(hooks_dir).startswith(str(repo)) else hp, cwd=repo)
        out["hooks_path"] = str(hooks_dir)
        if rc2 == 0:
            # Only hooks this run could WRITE. A tracked team pre-push is never
            # written, and counting it once let it suppress the gitignore entries
            # for the post-commit/post-rewrite files we then created untracked in
            # the same tracked directory — trap 3 verbatim, in the acme shape.
            ours = writable_hooks(repo, hp)
            gi = repo / ".gitignore"
            ig = gi.read_text(errors="replace") if gi.is_file() else ""
            missing = [h for h in ours
                       if not re.search(rf'^\s*{re.escape(hp.rstrip("/"))}/{h}\s*$', ig, re.M)]
            # A gitignore entry does NOTHING for a file git already tracks. Writing
            # one here would look like protection and be decoration, while the real
            # exposure goes unreported. So: entries are written only for hooks that
            # are NOT yet tracked (they stop a future `git add -A`), and a hook that
            # is ALREADY tracked is a refusal the person has to resolve.
            already = [h for h in ours
                       if sh("git", "ls-files", "--error-unmatch",
                             f'{hp.rstrip("/")}/{h}', cwd=repo)[0] == 0]
            if already:
                bad("tracked-hooks", f"core.hooksPath={hp} is TRACKED and {', '.join(already)} "
                    "are ALREADY COMMITTED. A .gitignore entry cannot help a tracked file — "
                    "this repository ships those hooks to every developer on clone",
                    "resolve by PR in that repository: either git rm --cached them, or adopt "
                    "the guard line deliberately. The harness will not write there.")
            elif missing:
                bad("tracked-hooks", f"core.hooksPath={hp} is TRACKED and {', '.join(missing)} "
                    "are not gitignored — a later `git add -A` would commit a per-machine "
                    "registration for every developer on clone",
                    "--apply adds the gitignore entries (they work: these files are untracked)")
            else:
                good("tracked-hooks", "roborev's hooks are gitignored in a tracked hooksPath")
        elif str(hooks_dir).startswith(str(repo)) and "/.git/" not in str(hooks_dir):
            # Untracked, but INSIDE the worktree: `git add -A` stages untracked
            # files whether or not the directory has tracked siblings. Only a
            # hooks dir under .git/ is genuinely outside the tree.
            bad("tracked-hooks", f"core.hooksPath={hp} is inside the worktree — the hooks and "
                "backups this run writes are stage-able by `git add -A` even though the "
                "directory is untracked today",
                "--apply adds the gitignore entries")
        else:
            good("tracked-hooks", f"core.hooksPath={hp} is outside the worktree; nothing travels")
    else:
        good("tracked-hooks", "no core.hooksPath; hooks live in .git/hooks, never tracked")

    # Trap 2: the post-commit hook must route through the guard
    guard = find_guard()
    out["guard"] = str(guard) if guard else None
    if decision == "declined":
        good("guard", "registration was declined and recorded; the guard is not needed here")
    elif not guard:
        bad("guard", "the harness guard was not found",
            "install the plugin with /somnio-engineering-ai:install, or pass CLAUDE_PLUGIN_ROOT")
    hooks_dir = Path(out.get("hooks_path") or (repo / ".git" / "hooks"))
    out["hooks"] = [classify_hook(repo, hooks_dir, h) for h in ROBOREV_HOOKS]
    for h in out["hooks"]:
        n, st = h["hook"], h["state"]
        if n == "pre-push" and not h.get("ours") and st != "absent":
            # See the supersede rule: a pre-push that does real work is left alone.
            # Graded ok, not a finding: the repo works without our flush (ROBOREV
            # LAST flushes before the skills ship), so leaving a team's gate in
            # place must not read as "not set up" forever.
            add = f"\"$GUARD\" {HOOK_OPS[n]} 2>/dev/null || true"
            if decision == "declined":
                good("hook-pre-push", f"a pre-push ({h['size']}B) is present; registration was "
                     "declined, so it is left exactly as it is")
                continue
            if st == "tracked":
                good("hook-pre-push", f"a TRACKED pre-push ({h['size']}B) is the team's and is "
                     f"left alone. For flush-on-push, add by PR: {add}   (ROBOREV LAST flushes "
                     "before the execution skills ship either way)")
                continue
            if h.get("kind") == "substantive":
                good("hook-pre-push", f"an untracked pre-push ({h['size']}B) does real work and is "
                     f"left alone. For flush-on-push, add to it: {add}")
                continue
            if decision == "declined":
                good("hook-pre-push", "registration was declined; the pre-push is left alone")
            else:
                bad("hook-pre-push", f"pre-push is {h.get('kind')} — nothing a person would miss",
                    "--activate backs it up and replaces it with the guard-routed flush")
            continue
        if st == "absent":
            if decision == "declined":
                good(f"hook-{n}", f"no {n} hook — registration was declined and recorded")
            else:
                bad(f"hook-{n}", f"no {n} hook — this repository is not registered",
                    "--activate installs one, pointed at the guard")
        elif h.get("ours"):
            good(f"hook-{n}", f"{n} routes through the guard")
        elif decision == "declined":
            # Every remaining branch describes a hook this run WOULD supersede.
            # A declined repository gets none superseded, so an ordinary husky
            # or lefthook post-commit was making the honest answer exit 2 for
            # ever -- with `--activate` printed as the fix, which is the skill
            # advising registration of the repository its owner declined.
            good(f"hook-{n}", f"{n} is the repository's own; registration was declined, so it "
                 "is left exactly as it is")
        elif st == "tracked":
            bad(f"hook-{n}", f"{n} is TRACKED ({h['size']}B) and is not ours — replacing it is a "
                "pull request against this repository, not a setup step",
                f"the harness will NOT write it. Add the guard call to {h.get('rel', n)} by PR: "
                f"\"$GUARD\" {n}")
        else:
            bad(f"hook-{n}", f"{n} exists, is untracked, and does not route through the guard",
                "--activate backs it up beside itself and supersedes it; the backup is yours "
                "to fold back in")

    # A recorded decline is an answer, NOT ground truth about the world. Every
    # branch above suppresses a finding on the strength of that record, so the
    # one state it must never hide is the contradiction: the person said no and
    # the repository is enqueueing reviews anyway. Reachable without hand-editing
    # anything -- `--activate` installs hooks before it writes the record, so a
    # refused record leaves live hooks, and `--decline` then only inspects the
    # manifest. ADR-0005 says the consent record carries the whole weight and
    # there is no second control behind it; this is that record being checked
    # against the disk instead of believed.
    if decision == "declined":
        # Only a guard-routed HOOK enqueues. A leftover .roborev.toml (which
        # `--apply` writes, and which needs no consent) is a file to clean up,
        # not a contradiction -- grading it as one would put a declined
        # repository back to exiting 2 for ever, which is the nag this release
        # removed.
        live = [h["hook"] for h in out["hooks"] if h.get("ours")]
        if not live and cfg.is_file():
            good("decline-mismatch", "declined; a .roborev.toml is left over but nothing "
                 "enqueues without a hook — `--reversal` lists it")
        elif live:
            bad("decline-mismatch",
                "this repository's record says registration was DECLINED, but "
                f"{', '.join(live)} {'is' if len(live) == 1 else 'are'} still on disk — the "
                "answer on file and the state of the repository disagree, and a guard-routed "
                "hook enqueues a review on every commit no matter what the record says",
                "run --reversal for the exact undo, then re-run --check; or --activate if the "
                "answer has actually changed")
        else:
            good("decline-mismatch", "declined, and no roborev artifact is on disk")

    # Trap 1 + Layer 2
    settings = Path.home() / ".claude" / "settings.json"
    try:
        d = json.loads(settings.read_text())
        n = sum("roborev" in hk.get("command", "")
                for ev in d.get("hooks", {}).values() for m in ev for hk in m.get("hooks", []))
        if n:
            good("layer-2", f"{n} roborev agent hooks registered")
        elif decision == "declined":
            good("layer-2", "no roborev agent hooks; registration was declined here anyway")
        else:
            bad("layer-2", "no roborev agent hooks in ~/.claude/settings.json — findings "
                "land in the database and reach no session, which is ENG-REV-05's entire point",
                "roborev agent-hook install --agent claude   (NEVER bare: nine agent configs)",
                scope="machine")
    except Exception as e:
        bad("layer-2", f"could not read {settings}: {e}", "check the file by hand", scope="machine")

    # D-1's record
    man = repo / ".claude" / "harness.json"
    if man.is_file():
        try:
            m = json.loads(man.read_text())
            rv = m.get("review")
            state, problem = classify_review(rv)
            if problem is not None and state != "absent":
                # One classifier, shared with verify_harness's check-7. Three
                # hand-written copies had already disagreed about an empty
                # `pin` -- graded good here, failed there -- and an empty pin
                # is the exact defect this release shipped to fix.
                bad("d1-record", problem,
                    "re-run --activate (or --decline) so the record is complete")
            elif state == "declined":
                good("d1-record", f"registration DECLINED and recorded by "
                     f"{rv.get('who','?')} on {rv.get('date','?')} — "
                     f"reason: {rv.get('reason','(none given)')}")
            elif state == "registered":
                good("d1-record", f"registration recorded by {rv.get('who','?')}")
            else:
                # D-13: an unregistered repository and one nobody was ever asked
                # about used to be the same absence. The fix line names both
                # answers, because either one closes this finding.
                bad("d1-record", "harness.json has no `review` block — this repository has "
                    "no recorded ANSWER: it is not registered, and no one is on record as "
                    "having been asked (ADR-0005 D-13)",
                    "--activate writes a registration, --decline writes a decline; either "
                    "one is a decided repository")
        except Exception as e:
            bad("d1-record", f"harness.json unreadable: {e}", "fix or regenerate it")
    else:
        bad("d1-record", ".claude/harness.json is absent — ADR-0005 D-1's registration "
            "record has nowhere to live", "run harness-init's install step first")
    return out


def apply(repo: Path, rep: dict) -> list[str]:
    """Idempotent, non-egress changes only. Never registers."""
    did = []
    cfg = repo / ".roborev.toml"
    if not cfg.is_file():
        cfg.write_text(
            "# Written by harness-init (ENG-REV-05, ADR-0005). Reviewed as config, by PR.\n"
            "#\n"
            "# NO [[hooks]] BLOCK, EVER. ADR-0005 D-6: a tracked hooks entry is arbitrary\n"
            "# shell the daemon runs on review events, from a file nobody reviews as code.\n"
            "# The harness guard refuses to enqueue when one is present.\n"
            "\n"
            "# Upstream's default is 3 seconds, which is too short in a LINKED WORKTREE —\n"
            "# the hook queues nothing and says nothing. Both execution skills work in\n"
            "# worktrees, so this is load-bearing rather than tuning.\n"
            "hook_timeout_seconds = 30\n"
            "\n"
            "# One review per N commits. Raising it cuts spend; it also means the everyday\n"
            "# post-commit state is \"no job yet\", which is why the skills flush explicitly\n"
            "# before shipping rather than trusting the hook.\n"
            "post_commit_batch_size = 3\n"
            "\n"
            "# Report at medium and above. This is the vendor's threshold, and ADR-0007 D-1\n"
            "# says severity is the vendor's while disposition is ours — do not add a second\n"
            "# severity scale in a skill.\n"
            "review_min_severity = \"medium\"\n", encoding="utf-8")
        did.append(f"wrote {cfg.name} (no [[hooks]], 30s timeout, batch 3, min-severity medium)")
    else:
        text = cfg.read_text(errors="replace")
        if "hook_timeout_seconds" not in text and not HOOKS_RE.search(text):
            cfg.write_text(text.rstrip("\n") + "\n\n# Worktree-safe timeout (harness-init).\n"
                           "hook_timeout_seconds = 30\n", encoding="utf-8")
            did.append("added hook_timeout_seconds = 30")

    for f in rep["findings"]:
        # Only the case a gitignore can actually fix: files not yet tracked.
        # "ALREADY COMMITTED" is the refusal case and writing a line there would
        # be the decoration this script exists to stop producing.
        # Both the tracked-dir case and the untracked-dir-inside-the-worktree case:
        # the audit promises "--apply adds the gitignore entries" for each, and
        # until 2026-09-18 only the first was honoured.
        if (f["check"] == "tracked-hooks" and "ALREADY COMMITTED" not in f["problem"]):
            hp = os.path.relpath(rep["hooks_path"], repo)
            gi = repo / ".gitignore"
            cur = gi.read_text(errors="replace") if gi.is_file() else ""
            add = [f"{hp}/{h}" for h in writable_hooks(repo, hp)
                   if not re.search(rf'^\s*{re.escape(hp)}/{h}\s*$', cur, re.M)]
            # The superseded-hook backups too: they land in the tracked hooks
            # directory and are a developer's private copy, not the team's file.
            pat = f"{hp}/*.pre-somnio.*.bak"
            if pat not in cur:
                add.append(pat)
            if add:
                gi.write_text(cur.rstrip("\n") + "\n\n"
                              "# roborev's hooks live in a hooks path inside this worktree. Committing\n"
                              "# them would turn a per-machine registration into one every\n"
                              "# developer inherits on clone (ADR-0005 D-1).\n"
                              + "\n".join(add) + "\n", encoding="utf-8")
                did.append(f"gitignored {len(add)} roborev hook(s) in {hp}/")
    return did


def install_hooks(repo: Path, rep: dict) -> list[str]:
    """Supersede the hook path so the harness works from day one — without ever
    silently replacing something the team owns. See THE SUPERSEDE RULE above."""
    changes, guard = [], rep.get("guard")
    if not guard:
        return ["REFUSED: the harness guard was not found; no hook installed"]
    # We write hook files ourselves and NEVER shell out to `roborev install-hook`.
    # Measured: install-hook edits a pre-existing TRACKED pre-push in place,
    # wrapping its own flush around the original body — on a repo like
    # acme-frontend that is a 69KB team push-gate modified in a tracked file,
    # one `git add -A` from shipping one developer's registration to everyone.
    # `roborev uninstall-hook` does restore it byte-for-byte, so the reversal path
    # stays the vendor's; the point is not to take the outbound path at all.
    hooks_dir = Path(rep.get("hooks_path") or (repo / ".git" / "hooks"))
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for h in rep.get("hooks", []):
        n, st = h["hook"], h["state"]
        if h.get("ours"):
            changes.append(f"unchanged: {n} already routes through the guard")
            continue
        f = hooks_dir / n
        if n == "pre-push" and st == "tracked":
            changes.append(f"LEFT ALONE: pre-push is tracked and belongs to this repository")
            continue
        if n == "pre-push" and h.get("kind") == "substantive":
            changes.append(
                f"LEFT ALONE: pre-push does real work. To flush on push, add:  "
                f"\"{guard}\" {HOOK_OPS[n]} 2>/dev/null || true")
            continue
        if st == "tracked":
            changes.append(
                f"REFUSED: {n} is tracked and belongs to this repository. Not written. "
                f"To adopt it, add this line by PR:  \"{guard}\" {n}")
            continue
        if st == "untracked":
            b = backup_hook(f)
            changes.append(f"SUPERSEDED: {n} -> backup at {b.name}. Your hook still exists; "
                           f"fold any of it you still want into the new file by hand.")
        f.write_text(
            "#!/bin/sh\n"
            "# Somnio harness (ENG-REV-05). Routes through the harness guard, never the\n"
            "# roborev binary: ADR-0005 D-6 refuses to enqueue a review in a repository\n"
            "# whose .roborev.toml declares [[hooks]], and that block is what stops the\n"
            "# daemon running arbitrary shell on a review event.\n"
            "#\n"
            "# A hook that was here before this ran was copied to <name>.pre-somnio.<ts>.bak\n"
            "# beside this file. It was not merged for you: a hook body runs on every\n"
            "# commit, so it is yours to read before it does.\n"
            "#\n"
            "# Never fails a commit or a push (ADR-0005 D-11).\n"
            f'GUARD={shlex.quote(str(guard))}\n'
            '[ -x "$GUARD" ] || exit 0\n'
            # The OP is not the hook's name: there is no `roborev post-rewrite`
            # subcommand (verified — "unknown command"). Upstream's own
            # post-rewrite calls `remap`, which re-attributes findings across a
            # rewrite (ADR-0005 Q-6). Writing the hook name here produced a hook
            # that could never work.
            f'"$GUARD" {HOOK_OPS[n]} 2>&1 | sed "s/^/roborev: /" || true\n'
            "exit 0\n", encoding="utf-8")
        f.chmod(0o755)
        if st == "absent":
            changes.append(f"installed: {n} -> the guard")
    return changes


def read_decision(repo: Path) -> str | None:
    """The recorded answer, or None when nobody has been asked.

    Three states, not two (ADR-0005 D-13): "registered", "declined", and the
    absence that means the question was never put. A `review` block with no
    `decision` key predates D-13 and is a registration — that is what every one
    of them was."""
    try:
        m = json.loads((repo / ".claude" / "harness.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rv = m.get("review")
    if not isinstance(rv, dict) or not rv:
        return None
    return "declined" if rv.get("decision") == "declined" else "registered"


def write_decline_record(repo: Path, who: str, reason: str) -> str:
    """D-13's half of D-1's record. A decline is an ordinary answer, and writing
    it is what stops "unregistered" and "never asked" being the same state on
    disk. Nothing else is written: no config, no hooks, no gitignore entry."""
    import datetime
    man = repo / ".claude" / "harness.json"
    if not man.is_file():
        return ("RECORD NOT WRITTEN: .claude/harness.json is absent, so the decline has "
                "nowhere to live. Run harness-init's install step first.")
    try:
        m = json.loads(man.read_text())
    except Exception as e:
        return f"RECORD NOT WRITTEN: harness.json is unreadable ({e})"
    existing = m.get("review")
    if isinstance(existing, dict) and existing and existing.get("decision") != "declined":
        # Declining a repository that is already registered is not a decline, it
        # is an unregistration, and that has a reversal path of its own (--reversal).
        return ("RECORD NOT WRITTEN: this repository is already REGISTERED "
                f"(who={existing.get('who','?')}, date={existing.get('date','?')}). A decline "
                "does not undo a registration — run the reversal (--reversal prints it) and "
                "then decline.")
    m["review"] = {
        "tool": "roborev",
        "decision": "declined",
        "date": datetime.date.today().isoformat(),
        "who": who,
        "reason": reason,
    }
    man.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    return (f"recorded the DECLINE in .claude/harness.json (who={who}, reason={reason}). "
            "Nothing else was written; re-run with --activate to change the answer.")


def reversal(repo: Path, rep: dict) -> dict:
    """What a registration left behind here, and the exact commands that undo it.

    Read-only. This exists because `.git/hooks` is untracked and trivially
    orphaned: harness-init's --uninstall strips what IT wrote and a roborev
    registration is not in that manifest's installed[], so without this listing
    a "clean" uninstall leaves a repository still enqueueing reviews."""
    artifacts: list[dict] = []
    cfg = repo / ".roborev.toml"
    if cfg.is_file():
        artifacts.append({"path": str(cfg), "what": "per-repo roborev config (written by --apply)",
                          "removal": "delete the file"})
    hooks_dir = Path(rep.get("hooks_path") or (repo / ".git" / "hooks"))
    for h in rep.get("hooks") or []:
        if h.get("ours"):
            artifacts.append({"path": h["path"], "what": f"{h['hook']} routed through the guard",
                              "removal": "roborev uninstall-hook restores it"})
    backups = sorted(hooks_dir.glob("*.pre-somnio.*.bak")) if hooks_dir.is_dir() else []
    for b in backups:
        artifacts.append({"path": str(b), "what": "byte-identical backup of the hook we superseded",
                          "removal": "restore it over the hook, then delete it"})
    decision = read_decision(repo)
    if decision:
        artifacts.append({"path": str(repo / ".claude" / "harness.json"),
                          "what": f"the D-1 record (decision: {decision})",
                          "removal": "remove the `review` block"})
    # The commands are scoped to what is actually here. Printing the full undo
    # against a repository that was never registered teaches a person to run
    # `repo delete --cascade` as a reflex, on a machine-wide store shared with
    # every other registered repository.
    if decision == "registered" or any(h.get("ours") for h in (rep.get("hooks") or [])):
        commands = [
            f"roborev uninstall-hook            # run inside {repo}",
            f"roborev repo delete --cascade --yes {repo}",
            "then remove the `review` block from .claude/harness.json",
        ]
        note = ("The five locations under ~/.roborev that hold review-derived content "
                "(reviews.db, logs/jobs/, logs/daemon.*, activity.log, post-commit.log) are "
                "machine-wide and shared with every other registered repository: "
                "`repo delete --cascade` is what scopes the deletion to this one "
                "(ADR-0005 Q-5).")
    elif decision == "declined":
        commands = ["remove the `review` block from .claude/harness.json"]
        note = ("This repository was never registered, so there is nothing under ~/.roborev "
                "to delete and no hook to uninstall. Removing the block does not re-decline "
                "it \u2014 it returns the repository to \"never asked\", which check-7 fails.")
    elif artifacts:
        # Reachable: `--apply` writes .roborev.toml and needs no consent, so a
        # repository can hold artifacts with no decision on file. The note used
        # to be derived from the decision alone and contradicted the list two
        # lines above it.
        commands = [f"delete {a['path']}" for a in artifacts]
        note = ("This repository has roborev artifacts but NO recorded decision, so it was "
                "never registered with the daemon: there is nothing under ~/.roborev to "
                "cascade-delete. Removing the files above returns it to untouched.")
    else:
        commands = []
        note = "This repository has no roborev artifacts and no recorded decision."
    return {"artifacts": artifacts, "commands": commands, "note": note,
            "registered": decision == "registered"}


def review_record_blocker(repo: Path) -> str | None:
    """Why D-1's record could NOT be written, decided BEFORE anything is written.

    The module docstring has always claimed "a refusal that has already written
    is not a refusal", and for --who/--payload-ack that was true. It was not
    true for the record itself: install_hooks() ran first, so a repository with
    no .claude/harness.json came out of --activate with three guard-routed
    hooks live on disk and NO consent record at all -- unattended per-commit
    egress with nothing on file, which is the one outcome ADR-0005 D-1 exists to
    prevent. This release made that path strictly more reachable by turning an
    unreadable pin into a refusal where it used to write pin: "".
    """
    man = repo / ".claude" / "harness.json"
    if not man.is_file():
        return (".claude/harness.json is absent, so D-1's record has nowhere to live. Run "
                "harness-init's install step first — this script will not invent a manifest, "
                "because the schema requires rules_source and installed[] that only that step "
                "can answer.")
    try:
        json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f".claude/harness.json is unreadable ({e})"
    if not pinned_roborev_version():
        return ("the pinned roborev version could not be read, so the record cannot name the "
                "version the approval was given against. The plugin root did not resolve, or "
                "scripts/install.sh has no ROBOREV_VERSION=\"...\" line. Fix: install the "
                "plugin with /somnio-engineering-ai:install, or pass CLAUDE_PLUGIN_ROOT.")
    return None


def write_review_record(repo: Path, who: str) -> str:
    """D-1's record, in the file D-1 names. Four independent reviews caught this
    being claimed in the docstring, in the audit's own fix text, in SKILL.md and
    in the reference file — while nothing wrote it. A registration that leaves no
    record is what D-1 exists to prevent, and a doc claim the code does not
    support is the class this repository grades hardest."""
    import datetime
    man = repo / ".claude" / "harness.json"
    if not man.is_file():
        return ("RECORD NOT WRITTEN: .claude/harness.json is absent, so D-1's record has "
                "nowhere to live. Run harness-init's install step first — this script will "
                "not invent a manifest, because the schema requires rules_source and "
                "installed[] that only that step can answer.")
    try:
        m = json.loads(man.read_text())
    except Exception as e:
        return f"RECORD NOT WRITTEN: harness.json is unreadable ({e})"
    pin = pinned_roborev_version()
    if not pin:
        # A record that cannot name the version it was given against does not
        # satisfy D-1's "a file, not a memory" — it satisfies the shape of it.
        # Refuse rather than write "pin": "" and call it recorded.
        return ("RECORD NOT WRITTEN: the pinned roborev version could not be read. The "
                "plugin root did not resolve, so scripts/install.sh was never found. "
                "Fix: install the plugin, or pass CLAUDE_PLUGIN_ROOT.")
    m["review"] = {
        "tool": "roborev", "decision": "registered", "pin": pin, "agent": "claude-code",
        "date": datetime.date.today().isoformat(), "who": who,
        # Q-2's words, not "the diff": an approval obtained against the narrower
        # description does not carry, so the record must not restate it narrowly.
        "payload_shape": "agent CLI with read access to the working tree",
    }
    man.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    return f"recorded D-1 registration in .claude/harness.json (who={who}, pin={pin})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--activate", action="store_true",
                    help="register: unattended per-commit egress. Needs --who and --payload-ack.")
    ap.add_argument("--who", default="")
    ap.add_argument("--payload-ack", action="store_true",
                    help="acknowledge that registration grants an agent CLI READ ACCESS to the "
                         "working tree, not a bounded diff (ADR-0005 Q-2)")
    ap.add_argument("--decline", action="store_true",
                    help="record that a person was asked and said no (ADR-0005 D-13). Needs "
                         "--who and --reason. Writes the record and nothing else; exits 0.")
    ap.add_argument("--reason", default="",
                    help="why registration was declined, in the person's own terms")
    ap.add_argument("--reversal", action="store_true",
                    help="read-only: list the roborev artifacts in this repository and print "
                         "the exact commands that undo a registration")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr); return 1

    rep = audit(repo)
    if "error" in rep:
        print(json.dumps(rep, indent=2)); print(f"error: {rep['error']}", file=sys.stderr); return 1

    if a.reversal:
        rep["reversal"] = reversal(repo, rep)
        print(json.dumps(rep["reversal"], indent=2))
        for art in rep["reversal"]["artifacts"]:
            print(f"  artifact: {art['path']}  ({art['what']})", file=sys.stderr)
        if not rep["reversal"]["artifacts"]:
            print("  no roborev artifacts in this repository", file=sys.stderr)
        for c in rep["reversal"]["commands"]:
            print(f"  undo: {c}", file=sys.stderr)
        print("  " + rep["reversal"]["note"], file=sys.stderr)
        return 0

    if a.decline and (a.activate or a.apply):
        print("error: --decline records an answer and writes nothing else; it cannot be "
              "combined with --apply or --activate", file=sys.stderr)
        return 1

    if a.decline:
        # A decline needs a person too. The same reason --activate needs --who:
        # the record has to name who answered, or it is a memory again.
        if not a.who.strip() or not a.reason.strip():
            print("error: --decline needs --who and --reason. A decline with no reason is "
                  "indistinguishable from a mis-click, and the next run has nothing to "
                  "re-open the question against (ADR-0005 D-13).", file=sys.stderr)
            return 2
        rep["record"] = write_decline_record(repo, a.who.strip(), a.reason.strip())
        print(f"  {rep['record']}", file=sys.stderr)
        if str(rep["record"]).startswith("RECORD NOT WRITTEN"):
            print(json.dumps(rep, indent=2))
            return 2
        after = audit(repo)
        rep["findings_before"], rep["findings"], rep["ok"] = (
            rep["findings"], after["findings"], after["ok"])
        rep["decision"] = "declined"
        repo_findings = [f for f in rep["findings"] if f.get("scope") != "machine"]
        rep["verdict"] = "clean" if not repo_findings else "work to do"
        print(json.dumps(rep, indent=2))
        for f in rep["findings"]:
            print(f"  [{f['check']}] {f['problem']}\n      fix: {f['fix']}", file=sys.stderr)
        # A decline is an ordinary outcome, never a failure: it must not degrade
        # the harness-init run that asked the question (ADR-0005 D-13).
        return 2 if repo_findings else 0

    # The refusal is decided BEFORE any write. A refusal that has already written
    # is not a refusal, and --activate without consent previously wrote
    # .roborev.toml and .gitignore and only then returned.
    if a.activate and (not a.who.strip() or not a.payload_ack):
        rep["refused"] = ("registration is unattended per-commit egress of this repository's "
                          "source (ADR-0005 D-1). It needs --who and --payload-ack, and a "
                          "provider already approved for this repository's INTERACTIVE work. "
                          "For a client repository that approval is the client's to give, "
                          "against Q-2's description: an agent CLI with read access to the "
                          "working tree, not a bounded diff.")
        rep["wrote"] = []
        print(json.dumps(rep, indent=2))
        print("REFUSED: " + rep["refused"], file=sys.stderr)
        return 2

    # The SECOND pre-write refusal, and the reason it is here rather than at the
    # point of writing: install_hooks() runs before write_review_record(), so a
    # record that cannot be written used to leave live hooks behind it.
    if a.activate:
        blocker = review_record_blocker(repo)
        if blocker:
            rep["refused"] = ("registration would install guard-routed hooks and then fail to "
                              "record the consent that justifies them: " + blocker)
            rep["wrote"] = []
            print(json.dumps(rep, indent=2))
            print("REFUSED: " + rep["refused"], file=sys.stderr)
            return 2

    if a.apply or a.activate:
        rep["applied"] = apply(repo, rep)
        for line in rep["applied"]:
            print(f"  applied: {line}", file=sys.stderr)

    if a.activate and a.who and a.payload_ack:
        rep["hook_changes"] = install_hooks(repo, rep)
        for c in rep["hook_changes"]:
            print(f"  {c}", file=sys.stderr)

    if a.activate:
        rep["record"] = write_review_record(repo, a.who.strip())
        print(f"  {rep['record']}", file=sys.stderr)
        rep["registered"] = {"who": a.who, "payload_shape":
                             "agent CLI with read access to the working tree (ADR-0005 Q-2)"}

    # Grade what is on disk NOW, not what was there before this run wrote. The
    # verdict used to come from the pre-write audit, so a complete registration
    # reported ".roborev.toml is absent" and exited 2 — which harness-init's
    # pipeline reads as "nothing written" (acme-frontend, 2026-09-18). The
    # pre-write findings stay in the report as the record of what was fixed.
    if a.apply or a.activate:
        after = audit(repo)
        rep["findings_before"] = rep["findings"]
        rep["findings"], rep["ok"] = after["findings"], after["ok"]
        rep["hooks"] = after.get("hooks", rep.get("hooks"))

    repo_findings = [f for f in rep["findings"] if f.get("scope") != "machine"]
    rep["verdict"] = "clean" if not repo_findings else "work to do"
    print(json.dumps(rep, indent=2))
    for f in rep["findings"]:
        print(f"  [{f['check']}] {f['problem']}\n      fix: {f['fix']}", file=sys.stderr)
    print(f"setup_roborev: {len(rep['ok'])} ok, {len(rep['findings'])} finding(s)", file=sys.stderr)
    # 2 whenever work remains OR anything was refused, regardless of mode. The old
    # form returned 2 only under --check, so a pipeline calling --apply/--activate
    # read success on a repo with a refused hook or an unwritten record.
    refused_any = any(str(c).startswith("REFUSED") or "NOT WRITTEN" in str(c)
                      for c in list(rep.get("hook_changes") or []) + [str(rep.get("record", ""))])
    return 2 if (repo_findings or refused_any) else 0


if __name__ == "__main__":
    sys.exit(main())
