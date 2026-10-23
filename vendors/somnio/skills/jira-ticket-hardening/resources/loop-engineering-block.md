# Loop-engineering strategy block

Every story gets one. It defines how an autonomous multi-agent loop would build *that* ticket. When the
**target repo** runs the loop-engineering experiment (it has a `docs/loop-engineering/` directory), use its
trace schema and metric IDs; otherwise emit the generic topology + derivation blocks below and omit the
scorecard. Tailor it per ticket via the derivation rule — do not paste boilerplate.

## Topology (roles from the target repo's `docs/loop-engineering/trace-schema.json`, when present)
- **Orchestrator (1)** — sequences the waves, dispatches workers, validates intermediate artifacts. Produces no
  final output.
- **Spec Writer (1, frontier=Opus)** — authors the **design/spec only** (SPEC delta, API/UI contract, validation
  matrix). **Writes no code.**
- **Workers (N, tiered)** — one per concrete artifact in the ticket's file list; plus `verify`/`check` workers.

## Model-assignment rule (the #1 cost finding — honor it)
> **Only the Spec Writer is frontier. Everything else is as cheap as the task allows.**
- **Haiku** — mechanical writes: DTO/enum/barrel/migration skeleton/design-token/route-shell/test-scaffold.
- **Sonnet** — reasoning: service logic, validation matrices, query/WHERE builders, hooks, renderer edge cases,
  and the `verify`/`check` pass.
- **Opus** — Spec Writer + any final synthesis only.
(Running everything on one tier is the calibration miss the experiment flags — model-mix efficiency ≈ 40% 🔴.)

## Derivation rule (forces tailoring, not boilerplate)
Worker roster == the ticket's concrete artifact list, one worker per artifact-cluster, batched into **waves**
(parallel `waveId`): W0 foundation (enum/migration/DTO/shell) → W1 domain/logic → W2 tests → verify.

## Skills mapping (example bindings — substitute the executing dev's SDD framework)
These are illustrative bindings from one dev setup (GSD/SPARC); per ENG-EXEC-01 the framework is
the dev's choice, so map roles to whatever the executing dev runs:
- Spec Writer → e.g. `gsd-spec-phase` / `sparc:spec-pseudocode` (FE: `gsd-ui-phase`).
- Workers → e.g. `gsd-execute-phase` / `sparc:coder`.
- verify/check → e.g. `gsd-code-review` / `gsd-verify-work` (FE: `gsd-ui-review`).
- Thin brief → e.g. `prompt-enhancer`.

## Scorecard stub (cite the metric IDs; name the baseline)
`A3 cost/artifact · A7 model-mix · A8/B6/E3 vs a NAMED CC baseline (single-agent CC building the ticket
sequentially) · F3 parity = build+lint+test green AND the ticket's AC-specific invariant holds · trace →
docs/loop-engineering/runs/<TICKET>-trace.json`. F3 quality-parity is the non-negotiable gate; the loop must then
win on ≥1 of A8/B6/E3.

## Worked example — a BE endpoint ticket
```
Spec Writer (Opus): contract + filter-builder/resolver interface + DTO shapes. Design only.
W0 (Haiku, parallel): controller/route shell, request/response DTOs + @ApiProperty, error-code addition, migration skeleton, barrels.
W1 (Sonnet): param parser + query builder, auth resolver edge cases, cursor codec, assembler.
W2 (Sonnet): unit + integration tests (gsd-add-tests).
verify (Sonnet): gsd-code-review + pnpm build/lint/test + openapi:check.
Scorecard: A7 = 1 Opus / 3 Sonnet / 4 Haiku · F3 = green AND (out-of-scope row absent for the filtered role) · trace …<TICKET>-trace.json
```
