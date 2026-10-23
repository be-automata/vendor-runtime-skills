# Driver selection matrix

How `/uat` detects the stack under test and picks the automation driver per lane. Every path
is enumerated; there is no else-branch. When detection lands on "undetectable", the skill stops
and asks.

## Detection signals

Read, in this order, and stop at the first lane set that is non-empty:

1. **PR changed paths** (`gh pr diff --name-only`) — the strongest signal, because it names what
   actually changed.
2. **Repo layout** at the root of the target checkout.
3. **Spec §3 Technologies & conventions** of the ticket's spec, when the paths are ambiguous
   (for example a shared package).

| Signal | Lane |
|:-------|:-----|
| `apps/web/**`, `src/app/**` (Angular), `pages/**` / `app/**` (Next), `*.tsx` outside `apps/api` | **web** |
| `lib/**/*.dart`, `pubspec.yaml`, `android/**`, `ios/**`, `*.swift`, `*.kt` | **mobile** |
| `apps/api/**`, `src/**/*.controller.*`, `src/**/*.service.*`, `openapi.*`, `prisma/**`, `migrations/**` | **api** |
| `infra/**`, `*.tf`, `.github/workflows/**` only | **api** (health + drift) — plus Step 6 is mandatory |
| Any two of the above | **mixed** — one lane each |
| Only docs, tests, or config with no runtime surface | **undetectable** — ask whether a UAT is even due |
| Nothing recognizable | **undetectable** — ask |

## Lane → driver

| Lane | Driver | Invocation | Evidence it must produce |
|:-----|:-------|:-----------|:-------------------------|
| web | gstack `browse` | `Skill: browse`. Aside `READY` → one `aside repl` script per flow from the `/browse` cookbook; otherwise `$B goto <ENV.web>` … `$B screenshot`, `$B console`, `$B network` (ADR-0009) | screenshots (entry, each state change, end state, every failure) + console/network captures |
| mobile | `agent-device` through the plugin agent | `Agent(subagent_type: "agent-device")` with platform, app build, scenario | device screenshot per step, full-flow recording, device/OS/build identifiers, `device-evidence.json` |
| api | direct authenticated HTTP against `ENV.api` | recorded `curl`-style calls (never a literal bearer token in a ticket body — see the WAF note in jira-ticket-hardening) | full request/response pair + `check_api_contract.py` result |
| mixed | each of the above | per lane | union; the report shows per-lane results |

## Web lane specifics

- `export GSTACK_SKIP_ASIDE=1` until ADR-0009 is accepted (its D-4). Then run the browse skill's
  BROWSER SETUP probe; it decides the driver once per run, never per step.

### Under Aside (`READY: aside`)

- The browser is the person's real one. The person signs in to `ENV.web` **as the manifest's test
  user** inside Aside — never the agent, never with the person's own account (ADR-0009 D-2). The
  first evidence line of every script prints the signed-in identity; any other identity is
  BLOCKED.
- One flow per `aside repl` script, built from the `/browse` cookbook's flow script: console hook,
  open, act, evidence lines, screenshot copied out of `ASIDE_DIR`, close the tab. Exit code is
  always 0 — a missing `GSTACK_STEP_OK` or a line starting `[error` is a failure.
- Never `aside exec` (ADR-0009 D-3). Never read or print cookies, tokens or localStorage.
- Read each copied PNG with the Read tool, then hand it to `collect_evidence.py`.

### Under `$B` (`NEEDS_ASIDE`, `ASIDE_NOT_RUNNING`, or Aside opted out)

- Run the browse SETUP check from the browse skill first. `NEEDS_SETUP` → ask before building.
- Log in as the manifest's test user. For MFA/CAPTCHA use `$B handoff` and ask the person to
  complete it; never store the secret in the transcript.
- Before every screenshot that asserts a resting state, move the cursor off interactive elements
  (hover states silently invalidate screenshots).
- `$B goto` before every `screenshot` — the daemon's tab persists across sessions.
- Read the PNG with the Read tool after capture so the evidence is actually inspected, then hand
  it to `collect_evidence.py`.

## Mobile lane specifics

- The app build must point at `ENV` (`--dart-define-from-file=env.<env>.json` or the platform
  equivalent); a debug build pointed at localhost violates the remote-only rule.
- Record: `agent-device record start <path>` before the flow, `agent-device record stop` after (it takes no path) — the full
  flow recording is mandatory evidence.
- Marketplace resident app: navigation is FCM push, not URL deep links; re-send the geo fix after
  every launch on the `uat` emulator; DNS flags on boot. These live in the agent's own reference;
  do not restate them per run, cite them.
- The agent may open, drive, capture and close. It may not post, merge, comment or transition.

## API lane specifics

- Auth: the manifest names the identity and where its token comes from (env var name). Redact
  `Authorization`, `Cookie`, `X-Api-Key`, and any header the collector's redaction list matches.
- Capture with timings: `curl -sS -w '\n%{http_code} %{time_total}' …` or the platform
  equivalent, one file per call, then the collector.
- Contract source, in order: the spec's §7 API Contract block (when it is a JSON/YAML OpenAPI
  fragment), the repo's generated OpenAPI (`openapi.json` / `apps/api/openapi/*`), the live
  `GET /api-json` of `ENV` (the deployed contract — a mismatch between the live contract and the
  repo's is itself a finding).

## When the driver is unavailable

| Missing | Action |
|:--------|:-------|
| gstack `browse` binary, and Aside not `READY` | ask to run the one-time setup; if declined, the web lane is BLOCKED (never fall back to "manual by the person" silently) |
| `agent-device` CLI | point to `/install --profile mobile`; mobile lane BLOCKED until present |
| `gcloud` / DB access | manifest gap (Step 2); the affected cases are BLOCKED |
