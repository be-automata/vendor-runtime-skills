# roborev per-repository setup (ENG-REV-05)

What `scripts/setup_roborev.py` checks, why each check exists, and what a person must be
told before a repository is registered. Step 6 of `SKILL.md` is the short form; this is the
reasoning behind it.

## The traps, and where each was learned

Every item below cost a day somewhere. The script exists so the next person pays none of it.

| # | Trap | Where it was measured |
|:--|:--|:--|
| 1 | `roborev agent-hook install` with **no `--agent`** writes **nine** agent configs — Codex, Copilot, Cursor, Gemini, Qwen, Grok, Droid, Hermes and Claude Code. Always `--agent claude` | 2026-09-18, this machine |
| 2 | The post-commit hook must point at the **harness guard**, never the binary, or ADR-0005 D-6's `[[hooks]]` activation blocker is bypassed on the one path that matters | ADR-0005 D-6 amendment |
| 3 | `core.hooksPath` may be a **tracked** directory. roborev honours the setting, so its hooks land as untracked files one `git add -A` from being committed — turning a per-machine registration into one every developer inherits on clone | pilot day zero, #45 |
| 4 | A linked worktree does not inherit the main checkout's `.roborev.toml` | pilot, #45 |
| 5 | `[[hooks]]` is never written by us and is an activation blocker if present — **reported, never removed** | ADR-0005 D-6 |
| 6 | `agent-hook dump` prints the **entire** settings file, secrets included. Never pipe it anywhere a log will see | 2026-09-18 |
| 7 | `roborev install-hook` edits a pre-existing **tracked** `pre-push` in place, wrapping its own flush around the original body. On acme-frontend that file is a 69 KB team push gate. `uninstall-hook` restores it byte-for-byte, so the reversal path is the vendor's — the point is not to take the outbound path at all | 2026-09-18, fixture |

## The supersede rule (area decision, 2026-09-18)

The harness takes over the hook path so it works from day one, **and the developer keeps
what was there.**

| Hook state | Action |
|:--|:--|
| **absent** | install ours — purely additive |
| **untracked** | back it up beside itself (`<name>.pre-somnio.<ts>.bak`, byte-identical), supersede, and print how to fold the old body back in. Machine-local and reversible |
| **tracked** | **refuse.** A tracked hook is the team's; replacing it is a pull request against their repository, not a setup step. Print the line to adopt instead |
| **pre-push** | stricter, because it is the hook a team most likely already owns. Ours is a flush of the pending post-commit batch, through the guard, that never fails a push — it covers a plain `git push` made outside the execution skills (ROBOREV LAST still does their synchronous flush-and-wait). Installed when the hook is **absent**; replaces an **untracked** one only when it is *replaceable* — empty or only `exit 0`/`true`/`:`, or exactly roborev 0.67.0's own generated template (unguarded, so it bypasses D-6), backed up first. A **tracked** pre-push, or an untracked one that does any other work, is **left alone** and the report prints the one line to add |

**The old body is never merged for the developer.** A hook runs on every commit, so it is
theirs to read before it does.

**A `.gitignore` entry does nothing for a file git already tracks.** Entries are written
only for hooks that are *not yet* tracked, where they genuinely stop a future `git add -A`.
A hook that is already committed is a refusal routed to a PR — writing a line there would
look like protection and be decoration.

**"Better than ours" is not graded.** Whether a team's pre-push beats a flush is a judgement
a script cannot make, so the rule uses a measurable proxy: a hook that does *any* work is
assumed to be there on purpose. The only bodies replaced are ones nobody would miss, or
roborev's own template, which ours is a strict improvement on (same flush, through the guard).

## The question is always put (ADR-0005 D-13, 2026-09-21)

Registration used to be outside `harness-init`'s default pipeline: the audit ran, the findings
printed, and the decision appeared in hand-over prose that only an operator reading to the end
would see. On `acme-core` (2026-09-20, plugin 0.9.27) that produced a run at **verdict: PASS**
on a completely unregistered repository, and the registration happened only because the operator
thought to ask why it had not.

**Step 6 now asks, every run, with the five disclosures below read out first.** "Default-on"
means *always asked, pre-selected yes* — never auto-registered. `--activate` still refuses
without `--who` and `--payload-ack`, before any write; per-repository consent is still required
and still recorded. What was removed is the possibility of the step being skipped unmentioned.

**A decline is a recorded answer, not a failure.** `--decline --who <name> --reason <why>` writes
`review` with `decision: "declined"` and **nothing else** — no `.roborev.toml`, no hook, no
`.gitignore` entry — and exits 0. It buys the distinction D-1 never had: an unregistered
repository and one nobody was ever asked about used to be the same absence on disk. After a
decline, `--check` stops the readiness nags rather than repeating them for ever, and
`verify_harness.py`'s `check-7` passes it exactly as it passes a registration. The only thing
`check-7` fails is silence — and `--rules-only`, which runs no registration step at all, passes
its own flag to the verifier so the absence that mode guarantees is a `skip` rather than a
failure.

"Clean" is precise: what stops being reported are the findings that describe a hook or config
*this run would have written* — the repository's own `post-commit`, a tracked `core.hooksPath`,
an unset `hook_timeout_seconds`. Ungated, a repository with an ordinary husky hook exited 2 for
ever and printed `--activate` as the fix, which is the harness advising registration of the
repository its owner declined.

**One finding is added, not removed: `decline-mismatch`.** A record saying declined while a
guard-routed hook sits on disk is a contradiction, and it is reachable without anyone editing a
file — so it is checked against the disk rather than believed. The record is an answer, not
ground truth about the world, and *The sandbox is not a control here* below says there is no
second control behind it. A leftover `.roborev.toml` alone is a leftover, not a contradiction:
nothing enqueues without a hook.

A decline needs a reason for the same reason a registration needs a `who`: without one it is
indistinguishable from a mis-click, and the next run has nothing to re-open the question
against. Declining a repository that is already **registered** is refused — that is an
unregistration, and it has its own path (`--reversal` prints it).

## What a person must be told before `--activate`

Registration is **unattended per-commit egress of this repository's source**. Read this out,
and record the answer:

1. **Every commit in this repository will be sent for review automatically.** No person
   chooses the moment.
2. **What is granted is an agent CLI with read access to this working tree** — not a
   bounded diff. Oversized diffs go to repo-local snapshot files the prompt points the
   agent at, and panel synthesis runs with read tools enabled, so files never opened in a
   session can be read. (ADR-0005 Q-2.)
3. **The provider must already be approved for this repository's interactive work.** For a
   client repository that approval is the client's to give, and it must have been given
   against the description in (2). **An approval obtained against "we send the diff" does
   not carry.**
4. **The data does not expire.** `roborev config` has no retention knob. Review-derived
   content lands in five locations under `~/.roborev` — `reviews.db`, `logs/jobs/`,
   `logs/daemon.*`, `activity.log`, `post-commit.log` — of which the database is the
   *smallest* (256 K of a 4.9 M store; the per-job agent logs hold 1.2 M). Forgetting it is
   a five-location deletion plus `roborev repo delete --cascade`. (ADR-0005 Q-5.)
5. **How to undo it:** `roborev uninstall-hook` in the repository, then
   `roborev repo delete --cascade --yes <path>`, then remove the `review` block from
   `.claude/harness.json`.

The answer is recorded in `.claude/harness.json` → `review`, committed, with `who` and
`payload_shape` — written by `--activate` itself, and **refused with a named reason if the
manifest is absent, unreadable, or the pin cannot be read**. That refusal happens **before any
write**: the hooks used to be installed first and the impossibility of the record discovered
afterwards, which left unattended per-commit egress running in a repository with no consent
record at all. Four roborev reviews caught this claim
being made while nothing wrote it; the record is real now. ADR-0005 D-1 calls that "a file, not a memory", and it is greppable across
every repository the plugin has touched.

## The sandbox is not a control here

Worth stating plainly, because it is easy to assume otherwise: the review is performed by
the roborev **daemon**, a supervised process outside any session's sandbox profile (D-11).
`allowedDomains` and `excludedCommands` do not constrain it. D-7's socket-only posture is a
filesystem permission, which is exactly why ADR-0005 requests no `excludedCommands` entry
and why business rule 18 is not strained. **The consent record carries the whole weight —
there is no second control behind it.**

## The reversal, listed rather than remembered

`--reversal` is read-only and removes nothing. It names what a registration left in this
repository — `.roborev.toml`, each hook that routes through the guard, every
`<name>.pre-somnio.<ts>.bak` backup, the `review` record — and prints the commands that undo it.
It exists because none of those paths are in the manifest's `installed[]`, so `harness-init
--uninstall` does not touch them, and `.git/hooks` is outside the work tree and therefore
trivially orphaned: an "uninstalled" repository that still enqueues a review on every commit is
the exact failure this listing prevents. The commands are scoped to what is actually present — a
repository that only ever declined is told to remove one JSON block, not to run
`repo delete --cascade` against a store shared with every other registered repository on the
machine.
