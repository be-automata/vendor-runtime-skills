#!/bin/sh
# PR-opened trigger (ENG-REV-01, session side). PostToolUse hook on Bash.
#
# Every time a Bash call runs `gh pr create` and GitHub answers with the new pull
# request's URL, this hook hands the session one instruction: attach the harness PR
# watch to that PR now (`/somnio-engineering-ai:pr-sync <url>`). gstack `/ship`,
# dev-execution Step 5, loop-mode Step 10 and a person typing the command all open PRs
# through `gh pr create`, so this is the one place the "watch every PR we open" rule
# lives — the agent no longer has to remember it.
#
# Advisory by construction: it reads the hook event on stdin, prints a
# `hookSpecificOutput.additionalContext` JSON document on stdout, and always exits 0.
# It never blocks, never calls the network, never touches the repository (one local
# `git remote get-url origin` read, to learn which repository the create targeted). The
# only plugin hook allowed to block is ship-gate.sh, and that stays so.
#
# Silent (no output) when: the command is not a `gh pr create` (`create` must be the
# subcommand right after `pr` — `gh pr comment --body "create"` is not one; multi-line
# commands, `URL=$(gh pr create …)` captures and `bash -c "…"` wrappers are), the response
# carries no github.com/<owner>/<repo>/pull/<n> URL for the repository the create targeted
# (the create failed, or ran with --web), the call was interrupted, the event is unreadable or
# oddly shaped, python3 is missing, or SOMNIO_PR_SYNC_HOOK=off is set — a person's override,
# never the agent's. A create that failed because the branch already has a PR echoes that
# PR's URL; watching it is right, and the line says the URL was returned, not that a PR was
# opened.
#
# Only the created PR is named: URLs are kept when their owner/repo is the create's
# `--repo`/`-R` target, else the cwd's `origin` remote; when neither is known, only the first
# URL in the response is kept. A chained command echoing some other repository's PR URL
# must not turn into an instruction to watch it.
#
# The /pr-sync run this triggers arms auto-push by default (ADR-0008 D-11): review-fix pushes
# pass autopush-guard.sh; replies and merges still wait for the person.
#
# A repository whose `.claude/harness.json` records `pr_review.reviewer = "none"` (nothing
# waits for a review; PRs merge on green CI) gets the opposite instruction: arm no watch,
# report CI once. Only the cwd's own record counts, and only for a create against that
# repository; a create with --repo elsewhere keeps the watch instruction.
command -v python3 >/dev/null 2>&1 || exit 0
[ "${SOMNIO_PR_SYNC_HOOK:-}" = "off" ] && exit 0
python3 -c '
import json, os, re, subprocess, sys

try:
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        sys.exit(0)
    tool_input = event.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd:
        sys.exit(0)
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()

    QUOTES = chr(34) + chr(39)
    WRAPPERS = {"env", "sudo", "time", "nohup", "command", "exec", "bash", "sh", "zsh",
                "-c", "-lc", "-e", "-x"}
    REPO_FLAGS = {"-R", "--repo"}
    PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")

    def create_target(command):
        # -> (creates, repo-or-None). `gh … pr create …` with gh in COMMAND position of a
        # segment (after VAR=x prefixes and shell wrappers such as bash -c), `pr` then `create`
        # as consecutive subcommands (only option tokens may sit between them) — so
        # `git commit -m "gh pr create"`, `gh pr comment --body "create"` and
        # `gh pr list --search create` are not creates. Quotes are stripped so that
        # bash -c "gh pr create" still is. The repo comes from -R/--repo when given.
        for segment in re.split(r"[;&|\r\n]+", command):
            toks = [t.strip(QUOTES) for t in segment.split()]
            # `PR_URL=$(gh pr create …)`, `URL=`gh …``, `(gh pr create …)`: peel the capture so
            # the command inside it is seen in command position.
            while toks:
                m = re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*=)?(?:\$\(|\(|`)(.*)", toks[0])
                if m:
                    toks[0] = m.group(1)
                    if not toks[0]:
                        toks = toks[1:]
                    continue
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]) or toks[0] in WRAPPERS:
                    toks = toks[1:]
                    continue
                break
            toks = [t.rstrip(")`") for t in toks]
            if not toks or os.path.basename(toks[0]) != "gh":
                continue
            rest = toks[1:]
            repo = None
            i = 0
            while i < len(rest):
                t = rest[i]
                if t in REPO_FLAGS and i + 1 < len(rest):
                    repo = rest[i + 1]
                elif t.startswith("--repo="):
                    repo = t[len("--repo="):]
                elif t.startswith("-R") and len(t) > 2:
                    repo = t[2:]
                i += 1
            if "pr" not in rest:
                continue
            after = rest[rest.index("pr") + 1:]
            j = 0
            while j < len(after) and after[j].startswith("-"):
                j += 2 if after[j] in REPO_FLAGS else 1
            if j < len(after) and after[j] == "create":
                return True, repo
        return False, None

    def recorded_reviewer(directory):
        try:
            r = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                return None
            with open(os.path.join(r.stdout.strip(), ".claude", "harness.json")) as f:
                block = json.load(f).get("pr_review")
            return block.get("reviewer") if isinstance(block, dict) else None
        except Exception:
            return None

    def origin_repo(directory):
        try:
            r = subprocess.run(["git", "-C", directory, "remote", "get-url", "origin"],
                               capture_output=True, text=True, timeout=3)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", r.stdout.strip())
        return f"{m.group(1)}/{m.group(2)}" if m else None

    creates, target = create_target(cmd)
    if not creates:
        sys.exit(0)
    if target and "/" in target:
        target = target.split("github.com/")[-1].strip("/")
        if target.endswith(".git"):
            target = target[:-4]
    else:
        target = origin_repo(cwd)

    # The response shape varies by version (an object with stdout/stderr, or a string):
    # search its JSON rendering, which covers both.
    resp = event.get("tool_response")
    if isinstance(resp, dict) and resp.get("interrupted") is True:
        sys.exit(0)
    text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
    urls = []
    for m in PR_URL.finditer(text):
        url = m.group(0)
        if target and f"{m.group(1)}/{m.group(2)}".lower() != target.lower():
            continue
        if url not in urls:
            urls.append(url)
    if not target:
        urls = urls[:1]
    target_desc = target if target else "the repository of the create"
    if not urls:
        sys.exit(0)

    lines = ["This `gh pr create` returned a pull request URL for " + target_desc + ": " + ", ".join(urls) + "."]
    own = origin_repo(cwd)
    if own and target and own.lower() == target.lower() and recorded_reviewer(cwd) == "none":
        for url in urls:
            lines.append("This repository records no review watch (`pr_review.reviewer` is `none` in "
                         ".claude/harness.json): do not run /pr-sync. Check CI once for " + url +
                         " (`gh pr checks " + url.rsplit("/", 1)[-1] + "`), report it, and leave the "
                         "merge to the person.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": "\n".join(lines)}}))
        sys.exit(0)
    for url in urls:
        lines.append("Run `/somnio-engineering-ai:pr-sync " + url + "` now — before any other step — "
                     "so the harness PR watch is attached to it (ENG-REV-01: every PR this session "
                     "opens is watched; the watch collects every collaborator'"'"'s review, and Automata'"'"'s "
                     "where the repository records it as a reviewer).")
    lines.append("One watch per PR: if the skill you are running arms the same watch on this PR itself "
                 "(dev-execution Step 5, loop-mode Step 10), that arming satisfies this and you do not "
                 "run /pr-sync as well; if a watch on it is already running in this session, TaskStop it "
                 "before re-arming. Arming the watch reads GitHub only; pushes, replies and merges still "
                 "wait for the person, except review-fix pushes: /pr-sync arms auto-push by default "
                 "(ADR-0008 D-11), and each one still passes autopush-guard.sh. Pass --no-auto-push "
                 "only when the person asked for it. In a loop-mode run, Step 10 owns the watch: do not run "
                 "/pr-sync, and if its auto-push answer was No, run autopush-guard.sh decline on this PR.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "additionalContext": "\n".join(lines)}}))
except SystemExit:
    raise
except Exception:
    pass
' 2>/dev/null
exit 0
