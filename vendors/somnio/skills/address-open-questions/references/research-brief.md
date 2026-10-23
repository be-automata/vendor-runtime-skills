# Research brief

The prompt given to one research `Agent` per question in Step 4. Fill the
angle-bracket fields; keep the output format exact so Step 5 can assemble the
card without re-reading sources.

```
Research one open engineering decision. You are not deciding it; you are
finding what others did in a similar situation and how strong that evidence is.

Question: <ID> — <question restated as a choice>
Options under consideration: <option list, stated default marked>
Context: <stack, runtime, cloud/provider, scale, constraints from the context brief>
What the repository already shows: <path:line facts gathered in Step 4>

Method: invoke the deep-research skill (Skill: deep-research) and follow it.
If it is not available, use WebSearch and WebFetch with the same multi-angle
method and state "deep-research unavailable" in your first line.

Look for, in this order:
1. Primary sources: vendor or framework documentation, RFCs, official defaults.
2. Postmortems and incident write-ups where this choice mattered.
3. Engineering blogs from teams at a comparable scale or stack.
4. Benchmarks or measurements, with their setup.

Return exactly:

## Findings
- <claim> — <source title> <URL> — primary|secondary — <how close to our context: same stack / similar / different>
(4-10 bullets; no claim without a URL)

## Precedents
- <who> chose <option-like choice> because <reason> — <URL>

## Facts that would change the answer
- <fact> — who or what could confirm it

## Per-option read
- <option>: supported|contested|unsupported by the evidence — one line

Do not give a final recommendation or a confidence score; the main session
scores with the repository evidence in hand.
```

## Proportionality

- Light research (team-process questions): tell the agent to stop after two
  recognised references and the repository's own conventions.
- A question the repository already answers needs no agent; its result is
  **Repository answers it** with the `path:line`.
- The brief carries the question, not the ticket: fill `Context` and `What the
  repository already shows` with a generic description — no verbatim ticket
  text, source code, secrets or customer names.
