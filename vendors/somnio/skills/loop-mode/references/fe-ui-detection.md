# FE/UI ticket detection

## Detecting a flagged ticket

A ticket flags as FE/UI if its scope text contains any of, or a close variant of:
`[FE]`, "screen", "view", "component", "page", "design", "Figma", "mockup", "UI", "layout",
"pantalla", "vista", "componente", "diseño" — or its scope names a frontend app path directly
(e.g. `apps/web`, `domains/*/components` in the target repo). When in doubt, err toward asking rather than skipping —
the cost of one extra question is far lower than shipping a UI ticket with the wrong visual
reference or the wrong model.

## Question 1 — design reference

For each flagged ticket (or grouped, if several visibly share one design source), ask:

> "{ticket-key} appears to touch UI/design. Do you want to attach a visual reference for this
> task?" (Rendered at run time in the Step-0 orchestrator language.)

Options:
- **Figma link** — ask for the URL; stored as `designRef: { type: 'figma', value: <url> }`.
- **Claude/Figma Design reference** — if the Figma MCP is connected, offer to extract design
  context (`get_design_context`/`get_screenshot`) instead of asking for a link by hand;
  `designRef: { type: 'claude-design', value: <what you extracted or the file/node reference> }`.
- **Image** — ask for the path/file; `designRef: { type: 'image', value: <path> }`.
- **No visual reference** — valid, especially when the ticket already cites a design handoff in
  its own text; `designRef: { type: 'none' }`.

## Question 2 — model recommendation for this ticket's implementation

Ask, per flagged ticket (can be a single question covering all FE tickets at once if they'll all
use the same choice):

> "For FE/UI tasks I recommend the latest available Opus model (or Fable 5 if available) for the
> implementation stage — it improves visual fidelity against the design. Use that recommendation,
> or keep the general model you already chose?" (Rendered in the Step-0 orchestrator language.)

Options: "Use latest Opus / Fable 5 for this task (recommended)", "Keep the general
implementation model (Sonnet)", "Specify another model".

Whatever they choose becomes `feModel` on that ticket's entry in `args.tickets[]` — the workflow
script uses it to override `implementModel` (and `specModel`, so the spec itself already accounts
for the design reference) for that specific ticket only, leaving every other ticket on the
Step 3 default.

## Hard requirement baked into FE ticket prompts

Every FE-flagged ticket's spec and implementation prompts (see `loop-mode-workflow.js`) carry this
instruction regardless of what the user answered above:

> Before writing anything, read this project's `CLAUDE.md` (and any nested one for the frontend
> app, e.g. `apps/web/CLAUDE.md`) and inspect `DESIGN.md` or whatever the project's actual
> design-system source is (tokens file, style guide, component library docs). Match its existing
> conventions exactly — colors, spacing, component patterns, naming. Do not invent a visual
> convention the project already defines elsewhere, and do not silently deviate from `DESIGN.md`
> even if the ticket's own description suggests something different — flag the disagreement
> instead.

If a `designRef` was supplied, it's passed into the same prompt so the implementing agent has the
concrete visual target, not just the written ticket description.
