# Spec template — 20 mandatory sections

This is the structural source of truth for every spec `loop-mode` writes (Step 6's `specStage`,
inside `loop-mode-workflow.js` — the compressed version embedded there as `SPEC_SECTIONS` must stay
in sync with this file if either is edited). Adapted from this project's own `generate-spec` skill
so a loop-mode spec and a manually-run `/generate-spec` spec are indistinguishable in structure.

**Hard rules, same as `/generate-spec`:**
- Never leave a raw `[...]` placeholder — replace with verified content or `TBD — see Open
  questions` (`TBD — ver Open questions` in Spanish specs).
- If a section genuinely doesn't apply, keep the heading and write `Not applicable — <reason>`.
- Spec language follows the project's own spec corpus (sample existing specs; default English).
  Slugs and filenames are always English kebab-case, independent of spec language.
- Section 7 (API Contract) gets its own `api-contract.md` file when there's real API surface — one
  section per endpoint: method, URL, auth, request schema, success response + status code, error
  responses + status codes/codes. `Sin API surface — no aplica.` when there's none (no separate
  file in that case).

## The 20 sections, in order

1. **Objetivo / Goal** — what's being built, what user outcome it enables, no ambiguity.
2. **Alcance / Scope** — `Incluido en esta fase` / `Fuera de scope` (out-of-scope is load-bearing —
   be exhaustive, not a token gesture).
3. **Tecnologías y convenciones / Technologies & conventions** — stack, language, package manager,
   UI library, state mgmt, API client, forms, validation, testing; pin real versions (cite the
   file: `package.json`, `pubspec.yaml`, etc); existing patterns to respect.
4. **Dependencias previas / Prerequisites** — concrete checkboxes: modules, endpoints, screens, API
   contract files, env vars, designs, types, base services, feature flags that must already exist.
5. **Arquitectura / Architecture** — pattern name, affected layers (yes/no + description each),
   numbered flow from user action to UI result, file layout for new files.
6. **Archivos a crear o modificar / Files to create or modify** — table (`Ruta | Acción | Propósito
   | Ejemplo del proyecto a seguir`), each `NUEVO`/`MODIFICAR`, then a `Detalle por archivo`
   subsection per file: responsibilities, the real existing file to follow, what must NOT mix in.
7. **API Contract** — see hard rules above.
8. **Criterios de éxito / Success criteria** — verifiable checkboxes; `Tests requeridos` (specific
   files + scenarios) and `Comandos de verificación` (the real lint/typecheck/test/build commands
   for this project — not generic ones).
9. **Criterios de UX / UX criteria** — non-obvious interface behavior: `Loading`, `Formularios`,
   `Passwords` (if applicable), `Errores`, `Navegación`, `Accesibilidad`.
10. **Decisiones tomadas / Decisions made (locked)** — design choices locked for this spec, with
    the *why* — the implementer must not silently change these.
11. **Edge cases** — `Datos inválidos`, `API errors` (one bullet per relevant status code — 400 401
    403 404 409 422 429 500), `Sin conexión`, `Timeout`, `Respuesta vacía o inesperada`, `Doble
    submit`.
12. **Estados de UI requeridos / Required UI states** — table: `idle / loading / success / error /
    empty / disabled / offline` — what shows, what the user can do, in each.
13. **Validaciones / Validations** — `Validaciones de cliente` (table `Campo | Regla | Mensaje`),
    `Validaciones de servidor` (defer to `api-contract.md`; define server-error-to-field mapping).
14. **Seguridad y permisos / Security & permissions** — secret handling, sensitive payloads,
    permission checks, 401/403 flow.
15. **Observabilidad y logging** — what to log, what to never log; cite the project's existing
    logging mechanism.
16. **i18n / textos visibles** — all user-facing strings from the project's translation system;
    table (`Key | Texto`) — no hardcoded strings.
17. **Performance** — renders, repeated API calls, debouncing/cancellation, main-thread work,
    caching patterns.
18. **Restricciones / Restrictions** — what the implementer must not do: API contract changes, new
    global abstractions, new dependencies, unrelated refactors, global style/nav changes,
    undocumented APIs.
19. **Entregables / Deliverables** — final checkboxes: code, tests, translations, types/interfaces,
    API integration, UX states, edge cases, docs.
20. **Checklist final para el agente / Final agent checklist** — pre-delivery verification: read
    spec end-to-end, reviewed `api-contract.md`, confirmed prerequisites exist, modified only
    listed files, followed real project examples, all UI states/edge cases implemented, no
    unauthorized deps, no changed decisions, ran lint/typecheck/tests/build, no temp logs, no
    unjustified TODOs.

## How `loop-mode` differs from a standalone `/generate-spec` run

- The tracker-sourced acceptance criteria and DoD from the ticket-intelligence pass
  (`references/ticket-intelligence.md`) feed sections 1, 2, and 8 directly — the spec agent should
  treat them as ground truth over the one-line scope summary, not re-derive them from scratch.
- FE/UI tickets (Step 4) get the extra CLAUDE.md/DESIGN.md requirement folded into sections 3 and
  9 — the "technologies & conventions" and "UX criteria" sections must cite the actual design
  system, not restate generic UI best practice.
- Validation (Step 6) uses the same PASS / PASS_WITH_WARNINGS / BLOCK verdict shape as
  `/generate-spec`'s validator, capped at 3 fix-and-revalidate cycles before escalating to the
  user — see `loop-mode-workflow.js`'s `validateStage`.
