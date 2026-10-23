#!/bin/sh
# Ship gate (ENG-HARNESS-04). Two modes:
#
#   Hook mode (default): PreToolUse hook on Bash. Reads the hook event JSON on
#   stdin; when the command is `gh pr create` / `git push`, blocks (exit 2)
#   unless every requirement in the repo's .claude/ship-gate.json has FRESH
#   run evidence in $GIT_DIR/somnio-ship-markers/ (written by ship-marker.sh).
#
#   ship-gate.sh --status: read-only report. Prints a per-marker verdict table
#   (FRESH / STALE / MISSING) and a final "SHIP: ALLOWED|BLOCKED" line.
#   Always exits 0 — it is a report, not a gate.
#
# Freshness is content-based: a marker is FRESH when its recorded wtree
# fingerprint (see ship-wtree.sh) equals the current working tree's, it is
# younger than ttl_minutes (default 480, a backstop that never EXTENDS
# validity), its recorded run exited 0, and it satisfies the requirement's
# "receipt" flag. Branch is recorded for orientation but never blocks —
# content equality subsumes it.
#
# Content identity deliberately ignores history: a rebase, amend or squash that
# preserves content keeps a marker FRESH. That is the right default (evidence
# recorded on a dirty tree survives the commit of that same content), but it
# means `git commit --amend` to reword a message keeps the receipt while
# changing the commit it certifies. A requirement that must certify a specific
# COMMIT — not merely specific content — opts in with "head_sensitive": true
# and additionally requires the marker's recorded HEAD to equal the current
# one. Default false: no requirement gains this without a PR that says so.
# This is NOT the branch check v0.3 removed — branch is still never consulted.
#
# Fail-open by design when: the repo has no ship-gate.json (gate not adopted),
# the command is not a ship command, we are not in a git repo, python3 is
# unavailable, or the wtree fingerprint cannot be computed (TTL-only fallback).
# Fail-closed when the config exists but cannot be parsed — a reviewed file
# that breaks should break loudly, not silently disable a gate.
#
# This is the ONE plugin hook allowed to block. The advisory learning hooks
# (ruflo-hook.sh) must never gain this power.
#
# Override: SOMNIO_SHIP_GATE=off skips the gate for that invocation. That is
# a decision that belongs to the user, not the agent: ask first.
#
# WHAT THIS GATE IS, AND IS NOT. It matches command TEXT before the shell runs
# it, so it recognises spelling it can see: path-qualified binaries, escapes,
# quotes inside a word, parentheses, and a substituted command word. It cannot
# resolve what only the shell knows -- an alias or function (g() { git "$@"; }),
# a symlink or a renamed copy of git. Those still reach git and are NOT gated.
# That is a stated limit, not an oversight: the gate exists to stop an agent, or
# an inattentive person, shipping without evidence, and a person who wants past
# it already has a documented one-line override above. Do not read the parser as
# a security boundary against its own operator, and do not add ever-cleverer
# text tricks in the belief that it could become one.
command -v python3 >/dev/null 2>&1 || exit 0
SCRIPTS_DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 -c '
import json, math, os, re, subprocess, sys, time

scripts_dir = sys.argv[1]
status_mode = len(sys.argv) > 2 and sys.argv[2] == "--status"

MARKER_SH = os.path.join(scripts_dir, "ship-marker.sh")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args],
                          capture_output=True, text=True)


def current_wtree(cwd):
    r = subprocess.run(["sh", os.path.join(scripts_dir, "ship-wtree.sh")],
                       cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def load_config(root):
    # Returns (requirements, ttl_seconds) or raises.
    with open(os.path.join(root, ".claude", "ship-gate.json")) as f:
        conf = json.load(f)
    raw = conf.get("required") or []
    if not isinstance(raw, list):
        raise ValueError("required must be a list")
    reqs = []
    for entry in raw:
        if isinstance(entry, str):
            reqs.append({"name": entry, "receipt": False})
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            reqs.append({"name": entry["name"],
                         "receipt": bool(entry.get("receipt", False)),
                         "expect_cmd": entry.get("expect_cmd"),
                         "head_sensitive": bool(
                             entry.get("head_sensitive", False))})
        else:
            reqs.append({"name": str(entry), "receipt": False,
                         "invalid": True})
    ttl = int(conf.get("ttl_minutes", 480)) * 60
    return reqs, ttl


def grade(req, marker_dir, wtree, head, ttl, now):
    # -> (verdict, detail): verdict in {"FRESH", "STALE", "MISSING"}.
    # A corrupt or malformed MARKER always degrades to STALE — only the
    # reviewed config file may fail the gate closed.
    name = req["name"]
    if req.get("invalid") or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return "STALE", "invalid requirement in ship-gate.json"
    path = os.path.join(marker_dir, name + ".json")
    if not os.path.isfile(path):
        return "MISSING", "no run evidence"
    try:
        with open(path) as f:
            marker = json.load(f)
        run_exit = int(marker.get("exit", 0))
        ts = float(marker.get("ts", 0))
        # float() accepts the JSON strings "nan"/"inf": every comparison against
        # NaN is False, so both expiry checks below would pass and the marker
        # would be FRESH forever. Non-finite is corrupt, and corrupt degrades.
        if not math.isfinite(ts):
            return "STALE", "marker timestamp is not a finite number"
        age = now - ts
    except Exception:
        return "STALE", "unreadable marker"
    if not marker.get("wtree"):
        return "STALE", ("marker predates content fingerprinting (v0.2) — "
                         "re-record with the current ship-marker.sh")
    if marker.get("mode") == "run" and run_exit != 0:
        return "STALE", (f"recorded run failed (exit {run_exit}) "
                         "— a failing run is not evidence")
    if req["receipt"] and marker.get("mode") != "run":
        return "STALE", ("claim marker; this requirement needs run evidence: "
                         f"ship-marker.sh run --label {name} -- <command>")
    expect_cmd = req.get("expect_cmd")
    if expect_cmd and marker.get("command") != expect_cmd:
        recorded = str(marker.get("command"))[:60]
        return "STALE", ("receipt command mismatch — expected "
                         f"{expect_cmd!r}, recorded {recorded!r}")
    marker_branch = marker.get("branch") or "unknown"
    if wtree is not None and marker["wtree"] != wtree:
        return "STALE", ("working tree content changed since the run "
                         f"(recorded on branch {marker_branch})")
    # Opt-in only. Checked after the content rule so the more actionable
    # message wins when both changed, and fail-closed for THIS requirement
    # when the commit cannot be established — an unverifiable history is not
    # a verified one.
    if req.get("head_sensitive"):
        recorded_head = marker.get("head")
        # isinstance, not truthiness: a marker is attacker-influenced data in
        # $GIT_DIR, and a non-string here (e.g. 12345) would raise TypeError on
        # the slice below, escape grade(), and be caught by the CONFIG handler —
        # which blames .claude/ship-gate.json and tells the operator to remove
        # it, i.e. routes a person to the documented fail-OPEN path. A corrupt
        # marker degrades to STALE. The literal `none` is the no-commit
        # sentinel written by ship-marker.sh; the two are coupled on purpose.
        if (not isinstance(recorded_head, str) or not recorded_head
                or recorded_head == "none"):
            return "STALE", ("marker records no usable commit and this "
                             "requirement is head_sensitive — re-record on a "
                             "committed tree")
        if head is None:
            return "STALE", ("cannot resolve HEAD, and this requirement is "
                             "head_sensitive — re-record")
        if recorded_head != head:
            return "STALE", ("history rewritten since the run: content is "
                             f"unchanged but HEAD is {head[:12]}, not the "
                             f"{recorded_head[:12]} that was verified "
                             "(amend/rebase/squash) — re-record")
    if age < 0:
        return "STALE", ("marker timestamp is in the future — check the "
                         "clock, then re-record")
    if age > ttl:
        return "STALE", (f"evidence expired ({int(age // 60)} min old, "
                         f"ttl {ttl // 60} min)")
    if marker.get("mode") == "run":
        return "FRESH", "receipt: " + str(marker.get("command"))[:60]
    return "FRESH", "claim marker matches the current tree"


def gather(cwd):
    # -> (reqs, verdicts, ttl) for a configured repo, or None.
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    root = top.stdout.strip()
    if not os.path.isfile(os.path.join(root, ".claude", "ship-gate.json")):
        return None
    reqs, ttl = load_config(root)
    # COMMON git dir, not the per-worktree one: markers are shared across
    # linked worktrees (the flow dev-execution/loop-mode use). Safe because
    # freshness is content-based — a marker only grades FRESH where the
    # working tree content matches its recorded wtree fingerprint.
    git_dir = git(cwd, "rev-parse", "--git-common-dir").stdout.strip()
    if git_dir and not os.path.isabs(git_dir):
        git_dir = os.path.normpath(os.path.join(cwd, git_dir))
    marker_dir = os.path.join(git_dir, "somnio-ship-markers")
    wtree = current_wtree(cwd) if reqs else None
    # Only when some requirement opts in — this runs on the hot path of every
    # gated push, and the default config never reads it. None when there is no
    # commit to name.
    head = None
    if any(r.get("head_sensitive") for r in reqs):
        h = git(cwd, "rev-parse", "HEAD")
        head = h.stdout.strip() if h.returncode == 0 and h.stdout.strip() \
            else None
    now = time.time()
    verdicts = [(r["name"],) + grade(r, marker_dir, wtree, head, ttl, now)
                for r in reqs]
    return reqs, verdicts, ttl


if status_mode:
    cwd = os.getcwd()
    try:
        result = gather(cwd)
    except Exception as exc:
        print(f"ship gate: .claude/ship-gate.json is unreadable ({exc}). "
              "Fix or remove it by PR before shipping.")
        sys.exit(0)
    if result is None:
        print("ship gate: not configured in this repo (no .claude/ship-gate.json)")
        sys.exit(0)
    reqs, verdicts, ttl = result
    if not reqs:
        print("ship gate: configured with no requirements — every ship passes")
        print("SHIP: ALLOWED")
        sys.exit(0)
    width = max(len(n) for n, _, _ in verdicts)
    print(f"ship gate (ttl {ttl // 60} min):")
    flags_by_name = {r["name"]: [f for f in ("receipt", "head_sensitive")
                                 if r.get(f)] for r in reqs}
    for name, verdict, detail in verdicts:
        # Detail always shown — a FRESH receipt names its recorded command
        # so a person can spot a no-op (e.g. true) posing as verification.
        # Effective flags shown too: a misspelled key in the reviewed config
        # parses cleanly and silently yields the WEAKER rule, and the verdict
        # alone cannot show that. An empty list is printed as "none".
        eff = ", ".join(flags_by_name.get(name) or ["none"])
        print(f"  {name.ljust(width)}  {verdict} ({detail})")
        print(f"  {chr(32) * width}  flags: {eff}")
    fresh = sum(1 for _, v, _ in verdicts if v == "FRESH")
    if fresh == len(verdicts):
        print("SHIP: ALLOWED")
    else:
        print(f"SHIP: BLOCKED ({fresh} of {len(verdicts)} fresh)")
    sys.exit(0)

# ---- hook mode ----------------------------------------------------------------
try:
    event = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if os.environ.get("SOMNIO_SHIP_GATE") == "off":
    sys.exit(0)

cmd = (event.get("tool_input") or {}).get("command") or ""

QUOTES = chr(34) + chr(39)
GIT_OPTS_WITH_ARG = {"-c", "--git-dir", "--work-tree", "--exec-path",
                     "--namespace"}


def resolve_dir(arg, base):
    # Resolve a cd/-C argument literally where that is safe. ~ expands
    # deterministically; anything the shell would still rewrite ($VAR,
    # $(...), backticks) is UNRESOLVABLE -> None, and the caller falls back
    # to the session cwd rather than grading a nonexistent literal path
    # open (P1 fix: unexpanded paths must not disarm the gate).
    if re.search(r"[$`(]", arg):
        return None
    arg = os.path.expanduser(arg)
    if base is None and not os.path.isabs(arg):
        return None
    return arg if os.path.isabs(arg) else os.path.join(base, arg)


def ship_target(command, session_cwd):
    # -> (is_ship, target_dir). Subcommand-position parsing so option-
    # bearing forms (git -C <dir> push, git -c k=v push, gh --repo x pr
    # create) cannot slip past, quote characters are stripped so subshell
    # wrappers (bash -c "git push") still gate, and a `cd <dir>` prefix or
    # `git -C <dir>` resolves the gate against the repo actually pushed to.
    # Matching the SUBCOMMAND (not any token) keeps git commit -m "push
    # fix" and git stash push out of the gate. Deeper evasions covered:
    # gh api on a /pulls endpoint, and -c alias.<x>=push definitions.
    cur = session_cwd
    for segment in re.split(r"[;&|]+", command):
        toks = [t.strip(QUOTES) for t in segment.split()]
        if not toks:
            continue
        # Match on the command WORD as the shell will resolve it, not on raw
        # text. `/usr/bin/git push` is ordinary usage and `g\it push` is a
        # deliberate evasion; both reach git, and matching raw tokens saw
        # neither. Arguments keep their ORIGINAL spelling (ntoks is only for
        # recognising command words and subcommands) so paths stay intact.
        def _cmdword(t):
            t = re.sub(r"[\\\"()]|[\x27]", "", t)
            # NEVER basename an option: basename("--git-dir=/r/.git") is
            # ".git", which no longer starts with "-", so the option walk below
            # reads it as the SUBCOMMAND and stops before it ever sees `push`.
            # That regressed --git-dir/--work-tree/--namespace/--exec-path into
            # ungated pushes. Options keep their spelling; only command words
            # and bare subcommands are normalised.
            return t if t.startswith("-") else os.path.basename(t)
        ntoks = [_cmdword(t) for t in toks]
        if ntoks[0] == "cd" and len(toks) > 1:
            cur = resolve_dir(toks[1], cur)
        # A segment whose command word is hidden behind a substitution
        # ($(which git) push) cannot be resolved statically. Treat a bare
        # `push` there as a ship rather than grading it open — but ONLY when no
        # command word was recognised, so `git commit -m "$(echo push)"` still
        # routes through the subcommand logic below and stays ungated.
        # Only when the COMMAND WORD ITSELF is substituted ($(which git) push).
        # Matching a substitution anywhere in the segment caught every
        # `docker push $IMG` and `npm run push -- $ARG` in a gated repo, whose
        # only advertised escape is SOMNIO_SHIP_GATE=off — a false positive
        # that teaches the operator to disable the one blocking control.
        if ("git" not in ntoks and "gh" not in ntoks
                and "push" in ntoks and re.search(r"[$`]", toks[0])):
            return True, cur
        if "git" in ntoks:
            rest = toks[ntoks.index("git") + 1:]
            nrest = ntoks[ntoks.index("git") + 1:]
            git_dir_target = cur
            i = 0
            sub = None
            while i < len(rest):
                t = nrest[i]
                if t == "-C" and i + 1 < len(rest):
                    git_dir_target = resolve_dir(rest[i + 1], cur)
                    i += 2
                    continue
                if t in GIT_OPTS_WITH_ARG and i + 1 < len(rest):
                    if re.fullmatch(r"alias\.[^=]+=push", rest[i + 1]):
                        return True, git_dir_target
                    i += 2
                    continue
                if t.startswith("-"):
                    if re.fullmatch(r"-c(alias\.[^=]+=push)", t):
                        return True, git_dir_target
                    i += 1
                    continue
                sub = t
                break
            if sub == "push":
                return True, git_dir_target
        if "gh" in ntoks:
            i = ntoks.index("gh") + 1
            # Subcommand WORDS come from the normalised list; the endpoint path
            # must come from the ORIGINAL tokens — basename("repos/o/r/pulls")
            # is "pulls", which would lose the /pulls match entirely.
            nrest_gh, rest_gh = ntoks[i:], toks[i:]
            if "pr" in nrest_gh and "create" in nrest_gh[nrest_gh.index("pr") + 1:]:
                return True, cur
            if "api" in nrest_gh and any("/pulls" in t for t in rest_gh):
                return True, cur
    return False, None


session_cwd = event.get("cwd") or os.getcwd()
is_ship, target_dir = ship_target(cmd, session_cwd)
if not is_ship:
    sys.exit(0)

# Fall back to the session cwd when the target is unresolvable ($VAR,
# $(...), backticks) or does not exist — never grade an imaginary path
# open when the session itself may sit in a gated repo.
if target_dir and os.path.isdir(target_dir):
    cwd = target_dir
else:
    cwd = session_cwd
try:
    result = gather(cwd)
except Exception as exc:
    print(f"SHIP GATE: .claude/ship-gate.json is unreadable ({exc}). "
          "Fix or remove it by PR before shipping.", file=sys.stderr)
    sys.exit(2)
if result is None:
    sys.exit(0)
reqs, verdicts, ttl = result
failures = [(n, d) for n, v, d in verdicts if v != "FRESH"]
if not failures:
    sys.exit(0)

print("SHIP GATE: blocked — required run evidence is missing or stale:",
      file=sys.stderr)
for name, reason in failures:
    print(f"  - {name}: {reason}", file=sys.stderr)
print("Run each required verification and record it as a receipt: "
      f"{MARKER_SH} run --label <name> -- <command> "
      "(bare ship-marker.sh <name> still works for markers not flagged "
      "receipt). Requirements live in .claude/ship-gate.json and change by "
      "PR. A person can skip the gate once with SOMNIO_SHIP_GATE=off — that "
      "decision belongs to the user, not the agent: ask first.",
      file=sys.stderr)
sys.exit(2)
' "$SCRIPTS_DIR" "${1:-}"
