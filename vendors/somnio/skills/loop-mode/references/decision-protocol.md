# Decision protocol

Every point where this skill hands a decision to the user — a ticket-intelligence mismatch, a
spec-validation blocker, an implementation blocker, a rebase conflict, a PR-comment finding — follows
the same shape. Consistency here matters more than cleverness: the user should be able to tell,
from the shape of the question alone, that they're in a loop-mode decision point.

## The four guardrails behind every decision (Karpathy guidelines)

These govern not just what you ask the user, but what you and every subagent in this pipeline do
before a question is even needed. Most gray areas this skill exists to eliminate get closed by
guardrail 1 and 2 alone — a decision point only needs to reach the user when these two didn't
already resolve it.

1. **Define verifiable success before coding.** Every phase, every spec, every fix has a concrete
   check attached — a command, a test, a diff — not "should work". This is why `loop-mode-workflow.js`
   never accepts "passed=true" without the agent having actually run the verification command.
2. **Surface assumptions before writing code.** If a reasonable person would be surprised by a
   decision made silently — a shared type invented instead of confirmed, a design detail guessed
   instead of asked, a tracker blocker ignored instead of flagged — that decision gets surfaced as a
   question or an explicit stated assumption, not shipped quietly. This is the guardrail that turns
   "gray area" into "the user made an informed call."
3. **Simplicity over speculation.** The minimum code that satisfies the ticket's actual scope — no
   speculative abstractions, no unrequested configurability, no defensive handling for states that
   can't occur in this phase. Applies to specs too: a spec section that pads itself with generic
   best-practice filler instead of naming this project's real patterns fails the workflow's
   spec-validation gate (Step 6).
4. **Surgical changes only.** Touch only the files a phase's spec section names. Don't "improve"
   adjacent code, comments, or formatting nobody asked about. If something else in the file looks
   wrong, report it — in the run summary's Notes section, or as a decision point if it's actually
   blocking — never fix it inline as a drive-by.

These four are injected directly into the spec-generation and implementation prompts inside
`loop-mode-workflow.js` (see `KARPATHY_NOTE`), not just documented here — the point is that the
agents doing the work apply them, not that a human reads about them after the fact.

## The rule

**Research before you ask, always.** Before presenting options, you (the orchestrator) must have
already done enough investigation — grep the actual code, check relevant ADRs, check `CLAUDE.md`
and any nested rules files, check what the blocked subagent already reported — to have a genuine
opinion about which option is best for this project. A decision prompt with no recommendation, or
a recommendation you haven't actually checked against the code, is not acceptable — it just moves
the investigation burden onto the user, which defeats the point of having an orchestrator.

## The shape

Use AskUserQuestion, multiple choice, structured as:

- **question** — state the actual decision, not a vague "what should I do?" Name the ticket, the
  stage, and the concrete fork in the road.
- **options** — 2-4 concrete choices. One of them is your researched recommendation, and its label
  ends with "(Recomendado)" / "(Recommended)" per the language chosen in Step 0, with a
  description that states *why*, citing what you actually found (a file, a pattern, an ADR) — not
  a generic justification.
- Never present a false binary ("do it" vs. "don't") when a third option (defer, partial, ask for
  more info) is genuinely on the table.

## Example — implementation blocker

A phase's implementing agent reports it's blocked because the ticket's spec assumed a shared
`Suggestion` type already existed in `packages/shared-types`, but grep shows it doesn't.

(The example below is rendered in English; at run time the orchestrator uses the language chosen
in Step 0.)

Bad: "The agent is blocked on MH-1915, phase 2. What do I do?"

Good:
> "MH-1915 [Implement] phase 2 blocked: the spec assumes `Suggestion` already exists in
> `packages/shared-types`, but I grepped the package and it doesn't — only a similar, unvalidated
> type exists in `apps/web/domains/dashboard/schemas/portfolio.schema.ts` (which the ticket itself
> marks 'reference shape only, do not reuse'). Options:"
> 1. **Define `Suggestion` in this ticket, under `domains/copilot/model/`, and continue
>    (Recommended)** — it's what MH-1904 already specifies as the real shape; no other ticket in
>    this set owns it, so no ownership collision.
> 2. Pause MH-1915 until MH-1904 is implemented at some later point.
> 3. Define it as a local, non-shared type without touching `shared-types` (redo if MH-1904 lands).

## Propagating the answer

Once the user answers, propagate it back into the actual run — re-invoke `Workflow` with
`resumeFromRunId` once the blocking condition is genuinely fixed (e.g. the type now exists), so
only the previously-blocked ticket and its downstream stages re-run. Log the decision (who chose
what, and your original recommendation) into that ticket's run summary (Step 8) under "Decisions"
— this is what lets a later run-summary reader tell the difference between the user's actual words
and your own delegated judgment calls.
