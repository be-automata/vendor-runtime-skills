export const meta = {
  name: 'loop-mode-ticket-pipeline',
  description: 'Per-ticket worktree+branch, spec-gen, spec-validation gate, phased implementation with real verification',
  phases: [
    { title: 'Setup', detail: 'create worktree + branch, rebase onto parent branch if dependent' },
    { title: 'Spec', detail: 'generate a spec verified against real code, not ticket prose', model: 'opus' },
    { title: 'Validate', detail: 'independent higher-capability audit of the spec before any code is touched', model: 'opus' },
    { title: 'Implement', detail: 'phased implementation, real build/lint/test verification + commit per phase', model: 'sonnet' },
  ],
}

// args shape (see SKILL.md Step 2/3/5 for the full example):
// {
//   tickets: [{
//     key, slug, scope, baseBranch?, parentBranch?,
//     isFE?, feModel?,                 // Step 3 — per-ticket model override for FE/UI tickets
//     designRef?: { type: 'figma'|'claude-design'|'image'|'none', value },
//   }],
//   fileOwnership: [{ file, owner }],
//   pushPolicy: "paused",
//   baseBranch: "develop",
//   verifyCommand: "<GATE_BUILD> && <GATE_TYPECHECK> && <GATE_LINT> && <GATE_COVERAGE>", // REQUIRED
//   installCommand: "<GATE_INSTALL>", buildCommand: "<GATE_BUILD>",                       // REQUIRED
//   (all three from dev-execution's execution_config.py — never a literal from another repo)
//   appPath: "",
//   worktreeRoot: ".claude/worktrees",
//   branchPrefix: "feat/",
//   maxFilesPerPhase: 5,
//   models: { spec: "opus", validate: "opus", implement: "sonnet" }, // Step 2 default, per-run
// }

// Some hosts deliver `args` as a raw JSON string rather than the parsed object the Workflow docs
// promise — parse defensively so this script works either way.
const ARGS = typeof args === 'string' ? JSON.parse(args) : args || {}

// Workflow subagents do NOT reliably inherit the orchestrator's working directory — a dry run of
// this exact script once created real worktrees inside the wrong repo because nothing pinned the
// target repo explicitly. repoPath is required and every git command below is anchored to it with
// `-C`, and worktreePath is always resolved to an absolute path — never rely on subagent cwd.
if (!ARGS.repoPath) {
  throw new Error('loop-mode-workflow.js requires args.repoPath (absolute path to the target git repo) — refusing to guess a working directory.')
}
const repoPath = ARGS.repoPath

function resolveWorktreeRootAbs() {
  const root = ARGS.worktreeRoot || '.claude/worktrees'
  return root.startsWith('/') ? root : `${repoPath}/${root}`
}

const worktreeRoot = resolveWorktreeRootAbs()
const branchPrefix = ARGS.branchPrefix || 'feat/'
const maxFilesPerPhase = ARGS.maxFilesPerPhase || 5
// The gate commands come from the target repo (execution_config.py's GATE_<ROLE> lines), never
// from a default: a pnpm literal here once ran `pnpm run test` in repos whose pre-push gate is a
// coverage target, and skipped the typecheck. And the worktree is bootstrapped before any agent
// runs (ENG-EXEC-06): a worktree without dependencies makes every gate exit 1, and agents read
// that tooling failure as a verification result.
for (const k of ['verifyCommand', 'installCommand', 'buildCommand']) {
  // A GAP anywhere in a composed command, not only as the whole value: `build && GAP && lint`
  // would run a command named GAP in every verify stage and exit 127.
  if (!ARGS[k] || /(^|&&|\|\||;)\s*GAP\s*($|&&|\|\||;)/.test(String(ARGS[k]).trim())) {
    throw new Error(`loop-mode-workflow.js requires args.${k} (from execution_config.py) — refusing to guess the target repo's commands.`)
  }
}
const verifyCommand = ARGS.verifyCommand
const installCommand = ARGS.installCommand
const buildCommand = ARGS.buildCommand
const models = ARGS.models || {}
const defaultSpecModel = models.spec || 'opus'
const defaultValidateModel = models.validate || 'opus'
const defaultImplementModel = models.implement || 'sonnet'

// FE/UI tickets (Step 3) can override spec+implement model with feModel (e.g. latest Opus / Fable 5)
// — validation stays on the general research/verification model, visual fidelity isn't its job.
function specModelFor(ticket) {
  return (ticket.isFE && ticket.feModel) || defaultSpecModel
}
function implementModelFor(ticket) {
  return (ticket.isFE && ticket.feModel) || defaultImplementModel
}

const KARPATHY_NOTE = `Apply these four rules to everything you do here:
1. Define verifiable success before coding — state the exact command/test that proves this step works, not "should work".
2. Surface assumptions before writing code — if a reasonable person would be surprised by a decision you made silently (an invented shared type, a guessed design detail, an ignored dependency), stop and report it as a blocker instead of guessing past it.
3. Simplicity over speculation — the minimum code that satisfies the stated scope; no speculative abstractions, no unrequested configurability, no defensive handling for states that can't occur here.
4. Surgical changes only — touch only the files named for this step. Don't "improve" adjacent code, comments, or formatting nobody asked about; if you notice something else worth fixing, report it, don't fix it inline.`

const SPEC_SECTIONS = `Write the spec with these 20 sections, in this exact order (this project's canonical spec template — keep every heading even when a section doesn't apply, writing "Not applicable — <reason>" instead of omitting it; never leave a raw [...] placeholder, use "TBD — see Open questions" instead):
1. Objetivo/Goal — what's being built, what outcome it enables.
2. Alcance/Scope — "Incluido en esta fase" / "Fuera de scope" (be exhaustive on the out-of-scope side).
3. Tecnologías y convenciones/Technologies & conventions — stack, versions (cite package.json/pubspec.yaml/etc), existing patterns to respect.
4. Dependencias previas/Prerequisites — concrete checkboxes for everything that must already exist.
5. Arquitectura/Architecture — pattern, affected layers (yes/no + description), numbered flow, file layout.
6. Archivos a crear o modificar/Files to create or modify — table (Ruta | Acción NUEVO/MODIFICAR | Propósito | Ejemplo del proyecto a seguir), then per-file detail.
7. API Contract — one-paragraph summary + endpoint list; if there's real API surface, also write a sibling api-contract.md (method, URL, auth, request/response schemas + status codes per endpoint); "Sin API surface — no aplica" if none.
8. Criterios de éxito/Success criteria — verifiable checkboxes, required tests (specific files+scenarios), exact verification commands for this project.
9. Criterios de UX/UX criteria — Loading, Formularios, Passwords (if applicable), Errores, Navegación, Accesibilidad.
10. Decisiones tomadas/Decisions made (locked) — with the why; the implementer must not change these.
11. Edge cases — Datos inválidos, API errors (one bullet per relevant status code), Sin conexión, Timeout, Respuesta vacía/inesperada, Doble submit.
12. Estados de UI requeridos/Required UI states — table: idle/loading/success/error/empty/disabled/offline.
13. Validaciones/Validations — Validaciones de cliente (table Campo|Regla|Mensaje), Validaciones de servidor (defer to api-contract.md).
14. Seguridad y permisos/Security & permissions — secret handling, sensitive payloads, permission checks, 401/403 flow.
15. Observabilidad y logging — what to log/never log, citing the project's real logging mechanism.
16. i18n/textos visibles — table of required translation keys; no hardcoded strings.
17. Performance — renders, repeated API calls, debouncing/cancellation, caching.
18. Restricciones/Restrictions — hard "do not" rules for the implementer.
19. Entregables/Deliverables — final checkboxes.
20. Checklist final para el agente/Final agent checklist — pre-delivery verification checkboxes.
Use the language this project's own spec corpus uses (sample a couple of existing specs if any exist; default English). Slugs/filenames are always English kebab-case regardless of spec language.`

function feRequirementNote(ticket) {
  if (!ticket.isFE) return ''
  const ref = ticket.designRef && ticket.designRef.type !== 'none'
    ? `Design reference for this ticket (${ticket.designRef.type}): ${ticket.designRef.value}`
    : 'No visual design reference was supplied for this ticket — if the ticket text alone leaves visual details ambiguous, flag it as blocked rather than inventing a look.'
  return `\n\nThis is a FE/UI ticket. Before writing anything: read this project's CLAUDE.md (and any nested one for the frontend app, e.g. apps/web/CLAUDE.md) and inspect DESIGN.md or whatever the project's actual design-system source is (tokens file, style guide, component library docs). Match its existing conventions exactly — do not invent a visual convention the project already defines elsewhere, and flag any disagreement between the ticket's own description and DESIGN.md instead of silently picking one. ${ref}`
}

const SETUP_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    worktreePath: { type: 'string' },
    branch: { type: 'string' },
    notes: { type: 'string' },
  },
  required: ['ok'],
}

const SPEC_SCHEMA = {
  type: 'object',
  properties: {
    written: { type: 'boolean' },
    path: { type: 'string' },
    notes: { type: 'string' },
  },
  required: ['written'],
}

const IMPLEMENT_SCHEMA = {
  type: 'object',
  properties: {
    blocked: { type: 'boolean' },
    blockReason: { type: 'string' },
    filesChanged: { type: 'array', items: { type: 'string' } },
  },
  required: ['blocked', 'filesChanged'],
}

const VALIDATE_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'PASS_WITH_WARNINGS', 'BLOCK'] },
    sectionScores: {
      type: 'array',
      description: 'one entry per of the 20 spec sections',
      items: {
        type: 'object',
        properties: {
          section: { type: 'string' },
          score: { type: 'string', enum: ['PASS', 'WEAK', 'MISSING'] },
        },
        required: ['section', 'score'],
      },
    },
    requiredFixes: {
      type: 'array',
      description: 'must be non-empty when verdict is BLOCK',
      items: {
        type: 'object',
        properties: {
          issue: { type: 'string' },
          confidence: { type: 'number', description: '1-10: 9-10 verified directly in code, 7-8 strong pattern match, 5-6 needs a human to verify, <=4 speculative' },
        },
        required: ['issue', 'confidence'],
      },
    },
    warnings: {
      type: 'array',
      description: 'non-blocking notes, populated when verdict is PASS_WITH_WARNINGS',
      items: { type: 'string' },
    },
    phases: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
        },
        required: ['title', 'files'],
      },
    },
  },
  required: ['verdict', 'requiredFixes', 'warnings', 'phases'],
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    written: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['written'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    passed: { type: 'boolean' },
    exitCode: { type: 'number' },
    output: { type: 'string' },
    commitHash: { type: 'string' },
  },
  required: ['passed'],
}

const blocked = []

function ownershipNote(ticketKey) {
  if (!ARGS.fileOwnership || !ARGS.fileOwnership.length) {
    return 'No shared-file ownership conflicts were declared for this ticket set.'
  }
  return (
    'Shared-file ownership map — do not modify a file owned by another ticket unless this ticket owns it:\n' +
    ARGS.fileOwnership.map((o) => `- ${o.file} -> owned by ${o.owner}`).join('\n')
  )
}

// Every path a subagent types must be absolute. This is the same rule the git
// commands above already follow with `-C`, extended to the exploration commands
// agents write themselves. Beyond the cwd-inheritance hazard, `cd <abs> && <relative
// read>` is a shape Claude Code's permission checker cannot resolve statically: in a
// repo carrying any Read() deny rule it cannot prove the command misses a denied
// path, so it fails closed and prompts — which stalls an unattended run even with
// bypass permissions on (deny rules are never bypassed; docs: permission-modes).
function pathAddressingNote(worktreePath) {
  return (
    'Path addressing — every path you type must be absolute, rooted at ' +
    `${worktreePath}. Never \`cd\` and then read a relative path ` +
    `(write \`ls ${worktreePath}/test/...\`, not \`cd ${worktreePath} && ls test/...\`; ` +
    `\`grep -rn X ${worktreePath}/src\`, not \`cd ... && grep -rn X src\`; ` +
    '`git -C` for git). Two reasons: workflow subagents do not reliably inherit a ' +
    'working directory, and a compound `cd`-then-relative-read cannot be checked ' +
    'against the repo permission rules, so it stops an unattended run with a prompt.'
  )
}

const SAFE_ID = /^[A-Za-z0-9._-]+$/
function shellSafeTitle(s) {
  // phase titles come from agent/tracker-derived text and are interpolated into
  // command lines below — strip shell metacharacters rather than trust them.
  return String(s).replace(/[`$\\"';|&<>(){}!\n]/g, '').slice(0, 120)
}

async function setupStage(ticket) {
  if (!SAFE_ID.test(ticket.key) || (ticket.slug && !SAFE_ID.test(ticket.slug))) {
    throw new Error(`unsafe ticket identifier for shell interpolation: ${JSON.stringify({ key: ticket.key, slug: ticket.slug })}`)
  }
  const branch = `${branchPrefix}${ticket.key}-${ticket.slug}`
  const worktreePath = `${worktreeRoot}/${ticket.key}`
  const baseRef = ticket.parentBranch || ticket.baseBranch || ARGS.baseBranch || 'develop'

  const rebaseInstructions = ticket.parentBranch
    ? `\n3. This ticket depends on ${ticket.parentBranch}. That branch may have advanced since ${ticket.key} was scoped, so rebase onto its current tip: ` +
      `\`git -C ${worktreePath} fetch origin ${ticket.parentBranch} 2>/dev/null; git -C ${worktreePath} rebase ${ticket.parentBranch}\`. ` +
      `If the rebase produces conflicts, STOP — do not resolve them yourself. Report the conflicting files and set ok=false so this can be escalated to the user.`
    : ''

  const prompt = `Set up an isolated git worktree for ticket ${ticket.key}. The target repo is ${repoPath} — every git command below is anchored to it with -C; do not assume your own shell's current directory is this repo.

Steps:
1. Run: git -C ${repoPath} worktree add ${worktreePath} -b ${branch} ${baseRef}
   - If the branch already exists, use instead: git -C ${repoPath} worktree add ${worktreePath} ${branch}
   - If a worktree already exists at ${worktreePath}, do NOT recreate it — run git -C ${worktreePath} status to verify it's on ${branch} and reuse it as-is.
   - If ${worktreePath} exists but is NOT a clean registered worktree (a leftover from a killed run), do NOT tear it down and do NOT use --force: report ok=false with the output of git -C ${worktreePath} status --short in notes, so the person decides. It may hold work nobody pushed. NEVER run rm -rf on it — that trips the repo's rm -rf safety gate and a bad path expansion could delete the wrong tree.
2. Run git -C ${worktreePath} status and git -C ${worktreePath} log -1 --oneline to confirm the worktree is clean and on the right branch.${rebaseInstructions}
${ticket.parentBranch ? '4' : '3'}. Bootstrap the toolchain from the worktree ROOT, in this order, each as its own Bash call with ${worktreePath} as the working directory: \`${installCommand}\`, then \`${buildCommand}\`. Both MUST exit 0. If either exits non-zero, do NOT try to fix the code or the lockfile: report ok=false with the command, its exit code and the last 30 lines of its output in notes. A tooling failure here is a bootstrap bug, never a verification result.

Report structured output: ok (true ONLY if the worktree exists, is clean, is on the correct branch/rebase state, AND both bootstrap commands exited 0 — false on any conflict, unexpected git error or non-zero bootstrap), worktreePath, branch, and notes describing anything unexpected (conflicts, dirty state, bootstrap output, etc).`

  const result = await agent(prompt, { label: `setup:${ticket.key}`, phase: 'Setup', schema: SETUP_SCHEMA })
  if (!result || !result.ok) {
    log(`${ticket.key}: blocked at setup — ${result ? result.notes : 'setup agent produced no result'}`)
    blocked.push({ ticket: ticket.key, stage: 'setup', detail: result })
    throw new Error(`setup blocked for ${ticket.key}`)
  }
  return { worktreePath: result.worktreePath || worktreePath, branch: result.branch || branch }
}

function specPromptBase(ticket, worktreePath) {
  const acText = ticket.acceptanceCriteria && ticket.acceptanceCriteria.length
    ? `Acceptance criteria (from the tracker, ground truth — treat these as more authoritative than the one-line scope below):\n${ticket.acceptanceCriteria.map((a) => `- ${a}`).join('\n')}`
    : 'No structured acceptance criteria were pulled from the tracker for this ticket — rely on the scope text and flag ambiguity rather than inventing criteria.'
  const dodText = ticket.definitionOfDone && ticket.definitionOfDone.length
    ? `Definition of Done (from the tracker):\n${ticket.definitionOfDone.map((d) => `- ${d}`).join('\n')}`
    : ''

  return `You are writing an implementation spec for ticket ${ticket.key}, inside the git worktree at ${worktreePath}.

Ticket scope: ${ticket.scope}

${acText}
${dodText}

${SPEC_SECTIONS}

Critical rule: verify every backend contract this ticket touches (endpoint paths, request/response envelope shape, nullability, status codes) against the ACTUAL code in this worktree — controllers, DTOs, e2e tests. Never trust the ticket description's prose about the API as ground truth. Explicitly call out every place the ticket text and the real code disagree, and every place the ticket assumes a dependency "already exists" that you haven't personally confirmed is true in THIS worktree right now.

${ownershipNote(ticket.key)}

${pathAddressingNote(worktreePath)}

Write the spec to ${worktreePath}/docs/specs/${ticket.key}/spec.md (and a sibling api-contract.md if section 7 has real API surface). Within section 6, break the implementation into phases of at most ${maxFilesPerPhase} touched files each; for every phase, name the exact files it touches. Verification for every phase always runs the project's own canonical command (\`${verifyCommand}\`) from the worktree ROOT (${worktreePath}) — this is a turbo pipeline that resolves workspace-package build order (e.g. rebuilding ${'`'}packages/*${'`'} dist output) automatically; do NOT propose scoping verification into a subpackage directory (e.g. running it from inside apps/web directly invokes that package's own script and skips turbo's cross-package dependency build, which has previously caused false "module not found" failures for otherwise-correct code). Every phase MUST list at least one real file it modifies or creates — a phase with zero files is rejected downstream as a stalled/misunderstood phase, so do NOT emit a "bootstrap"/"pnpm install"/"clean tree check" phase with an empty file list; if setup steps are needed, fold them as a preamble note inside phase 1's instructions (run before touching phase 1's files), not as their own phase.${feRequirementNote(ticket)}

${KARPATHY_NOTE}`
}

async function specStage(setup, ticket) {
  const prompt = `${specPromptBase(ticket, setup.worktreePath)}

After writing the file(s), run \`ls -la ${setup.worktreePath}/docs/specs/${ticket.key}/spec.md\` yourself and confirm it actually exists on disk with real content before reporting written=true. Do not report written=true from memory of having written it — a write that silently failed is worse than reporting the failure honestly.`

  const specResult = await agent(prompt, { label: `spec:${ticket.key}`, phase: 'Spec', model: specModelFor(ticket), schema: SPEC_SCHEMA })
  if (!specResult || !specResult.written) {
    log(`${ticket.key}: blocked at spec generation — ${specResult ? specResult.notes : 'spec agent produced no result'}`)
    blocked.push({ ticket: ticket.key, stage: 'spec', detail: specResult })
    throw new Error(`spec generation failed for ${ticket.key}`)
  }
  return { worktreePath: setup.worktreePath }
}

const MAX_REVALIDATE_CYCLES = 3

function validatePrompt(specPath, worktreePath, ticketKey) {
  return `Audit the spec at ${specPath} before any implementation agent is allowed to touch code for ${ticketKey}, against this project's canonical 20-section template (${SPEC_SECTIONS}).

${pathAddressingNote(worktreePath)}

Score each of the 20 sections PASS / WEAK / MISSING. Challenge vague content specifically: generic patterns without names, error handling without listing cases, missing version pins, non-existent file paths, unreplaced [...] placeholders, an API Contract section with no sibling api-contract.md despite real API surface.

Also check, independent of section scoring:
- dead or unreferenced code the spec would introduce
- registries, barrels, or contracts that would not actually compile as specified
- any phase that proposes scoping verification into a subpackage directory instead of running the project's canonical command from ${worktreePath} root (subpackage-scoped runs skip turbo's cross-package dependency build order)
- any phase touching more than ${maxFilesPerPhase} files
- any phase with an empty or missing files list (a "bootstrap"/"pnpm install"/"clean tree check" phase with zero files is not a valid phase — it must be folded into phase 1's instructions as a preamble step, or the spec must be revised to remove it) — this is a BLOCK-worthy requiredFix, not a warning
- any claim that a shared dependency "already exists from ticket X" that the spec asserts but doesn't demonstrate — re-check this yourself against the real code in ${worktreePath}, don't trust the spec's word for it

Return a verdict:
- BLOCK if any section scores MISSING, or any of the independent checks above finds a real problem — requiredFixes must list every one of them, each with a confidence score (9-10 verified directly in code, 7-8 strong pattern match, 5-6 a human should double-check, ≤4 speculative).
- PASS_WITH_WARNINGS if everything is at least WEAK-or-better and no independent check failed, but something's worth a human glancing at — put those in warnings (plain notes, no confidence needed, non-blocking).
- PASS if everything is solid.

Alongside the verdict, always emit phases[]: the ordered implementation phases the implement stage will execute, each with a title and a non-empty files list of at most ${maxFilesPerPhase} files. A PASS without usable phases is not a PASS.`
}

async function validateStage(specResult, ticket) {
  const specPath = `${specResult.worktreePath}/docs/specs/${ticket.key}/spec.md`
  let validation = null
  const history = []

  for (let cycle = 1; cycle <= MAX_REVALIDATE_CYCLES; cycle++) {
    validation = await agent(validatePrompt(specPath, specResult.worktreePath, ticket.key), {
      label: `validate:${ticket.key}:cycle${cycle}`,
      phase: 'Validate',
      model: defaultValidateModel,
      effort: 'xhigh',
      schema: VALIDATE_SCHEMA,
    })
    history.push({ cycle, verdict: validation ? validation.verdict : 'NO_RESULT' })

    if (!validation || validation.verdict === 'BLOCK') {
      const fixes = validation ? validation.requiredFixes : []
      log(`${ticket.key}: spec validation cycle ${cycle}/${MAX_REVALIDATE_CYCLES} — BLOCK — ${fixes.map((f) => `${f.issue} (confidence ${f.confidence}/10)`).join('; ') || 'validation agent produced no result'}`)
      if (cycle === MAX_REVALIDATE_CYCLES) break
      const fixPrompt = `Revise the spec at ${specPath} to address these required fixes from the validation gate — do not touch anything else in the spec:\n${fixes.map((f) => `- ${f.issue}`).join('\n')}\n\nAfter revising, run \`ls -la ${specPath}\` yourself and confirm the file was actually updated before reporting written=true.\n\n${KARPATHY_NOTE}`
      await agent(fixPrompt, { label: `spec-fix:${ticket.key}:cycle${cycle}`, phase: 'Spec', model: specModelFor(ticket), schema: FIX_SCHEMA })
      continue
    }
    break
  }

  if (!validation || validation.verdict === 'BLOCK') {
    log(`${ticket.key}: blocked at spec validation after ${MAX_REVALIDATE_CYCLES} cycles`)
    blocked.push({ ticket: ticket.key, stage: 'validate', detail: { history, lastResult: validation } })
    throw new Error(`spec validation failed for ${ticket.key} after ${MAX_REVALIDATE_CYCLES} cycles`)
  }
  if (validation.verdict === 'PASS_WITH_WARNINGS' && validation.warnings && validation.warnings.length) {
    log(`${ticket.key}: spec validation PASS_WITH_WARNINGS — ${validation.warnings.join('; ')}`)
  }
  return { ...validation, worktreePath: specResult.worktreePath }
}

async function implementStage(validation, ticket) {
  const worktreePath = validation.worktreePath
  // The validator's phases[] IS the implement contract — a PASS without usable
  // phases is a validator failure, not a license for one unbounded mega-phase.
  const phases = validation.phases && validation.phases.length ? validation.phases : null
  if (!phases) {
    log(`${ticket.key}: validator returned no phases — blocking instead of improvising a single unbounded phase`)
    blocked.push({ ticket: ticket.key, stage: 'implement', detail: 'validator emitted no phases[]' })
    return { ticket: ticket.key, status: 'blocked-in-phase', phase: 'phase-planning', phaseResults: [] }
  }

  const phaseResults = []
  for (const phase of phases) {
    // Verification deliberately always runs from the worktree root — turbo
    // cross-package ordering makes subpackage-scoped runs produce false failures.
    const verifyCwd = worktreePath

    const implPrompt = `Implement phase "${phase.title}" of the spec at ${worktreePath}/docs/specs/${ticket.key}/spec.md, inside the worktree at ${worktreePath}.

Touch only the files this phase specifies (at most ${maxFilesPerPhase}): ${phase.files && phase.files.length ? phase.files.join(', ') : 'per the phase breakdown in the spec'}.

${pathAddressingNote(worktreePath)}

If you hit a genuine ambiguity the spec doesn't resolve, or discover the backend contract differs from what the spec assumed, STOP and report it as blocked — do not guess or improvise past it.

If this phase is gated on missing backend work, build a placeholder that is structurally incapable of rendering fake data — no prop reads of the missing field, no network call referencing it, no dynamic expression that could resolve to fabricated content — not a soft "TODO" comment that could silently render something fake.${feRequirementNote(ticket)}

${KARPATHY_NOTE}

After you're done (or if you stopped on a blocker), run \`git -C ${worktreePath} status --short\` yourself and report the real list of changed files in filesChanged — don't reconstruct it from memory of what you intended to touch. Set blocked=true and blockReason if you stopped early.`

    const impl = await agent(implPrompt, { label: `implement:${ticket.key}:${phase.title}`, phase: 'Implement', model: implementModelFor(ticket), schema: IMPLEMENT_SCHEMA })

    if (!impl || impl.blocked) {
      log(`${ticket.key}: blocked in phase "${phase.title}" during implementation — ${impl ? impl.blockReason : 'implementation agent produced no result'}`)
      blocked.push({ ticket: ticket.key, stage: `implement:${phase.title}`, detail: impl })
      return { ticket: ticket.key, status: 'blocked-in-phase', phase: phase.title, phaseResults }
    }
    if (!impl.filesChanged || impl.filesChanged.length === 0) {
      log(`${ticket.key}: phase "${phase.title}" reported no file changes — treating as a no-op, not committing`)
      blocked.push({ ticket: ticket.key, stage: `implement:${phase.title}`, detail: 'no files changed — likely a stalled or misunderstood phase' })
      return { ticket: ticket.key, status: 'blocked-in-phase', phase: phase.title, phaseResults }
    }
    if (impl.filesChanged.length > maxFilesPerPhase) {
      log(`${ticket.key}: phase "${phase.title}" touched ${impl.filesChanged.length} files, over the ${maxFilesPerPhase}-file cap — escalating instead of committing an oversized phase`)
      blocked.push({ ticket: ticket.key, stage: `implement:${phase.title}`, detail: { reason: 'phase-cap exceeded', filesChanged: impl.filesChanged } })
      return { ticket: ticket.key, status: 'blocked-in-phase', phase: phase.title, phaseResults }
    }

    const verifyPrompt = `Run the project's real verification command from inside ${verifyCwd}:
\`${verifyCommand}\`

Report the ACTUAL exit code you observed and a short excerpt of any failure output. Do not report passed=true unless you literally ran the command and saw a zero exit code — "should pass" or "looks correct" is not acceptable evidence.

If and only if it passes, then run:
\`git -C ${worktreePath} add -A && git -C ${worktreePath} commit -m "${shellSafeTitle(phase.title)} (${ticket.key})"\`
and report the resulting commit hash.`

    const verify = await agent(verifyPrompt, { label: `verify:${ticket.key}:${phase.title}`, phase: 'Implement', schema: VERIFY_SCHEMA })
    phaseResults.push({ phase: phase.title, verify })

    if (!verify || !verify.passed) {
      log(`${ticket.key}: blocked in phase "${phase.title}" — verification did not pass`)
      blocked.push({ ticket: ticket.key, stage: `implement:${phase.title}`, detail: verify })
      return { ticket: ticket.key, status: 'blocked-in-phase', phase: phase.title, phaseResults }
    }
  }

  return { ticket: ticket.key, status: 'done', phaseResults }
}

const outcomes = await pipeline(ARGS.tickets, setupStage, specStage, validateStage, implementStage)

return {
  completed: outcomes.filter(Boolean),
  blocked,
  ticketCount: ARGS.tickets.length,
}
