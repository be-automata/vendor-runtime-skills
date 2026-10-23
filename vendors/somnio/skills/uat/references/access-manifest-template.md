# Access & Credentials Manifest

The single consolidated request `/uat` makes before any test executes. Fill every row; mark each
`auto-resolved` (found in `docs/uat/`, the repo, or verified live), `verified` (checked this
run), or `needs person`. Present it once. A credential requested after this manifest is a
defect in the manifest, recorded under `## Manifest gaps` in the run record.

## Contents

- Rules
- The template
- Where to mine each row
- Verification commands

## Rules

- **Values never enter the transcript.** The manifest names the variable, the store, or the
  person who holds it (`ZOHO_SANDBOX_TOKEN` in the shell env; `staff-test` password in 1Password
  "Marketplace QA"). The run reads secrets from the environment at call time.
- **Test users are named by role and tenant**, and the record says which prior UAT used them so
  the next run can reuse them.
- **Every row has a purpose** (which criterion or step needs it). A row with no purpose is
  removed — over-asking is the opposite failure mode.
- **Reuse beats asking.** Mine `docs/uat/` first (below); ask only for what mining did not
  settle.

## The template

```markdown
## Access & Credentials Manifest — <scope> (<date>)

### Environments
| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| Environment | develop (`mh-develop-497000`, us-east5) | all lanes | verified | .loop/config.yaml, gcloud |
| Web URL | https://<web-host> | web lane | verified (200) | docs/uat/UAT-…-2026-08-26 |
| API URL | https://<api-host>/api/v1 | api lane | verified (`/health` 200) | same |
| Deployed build | revision `<rev>` image `<sha>` contains PR merge `<sha>` | BLOCKED gate | verified | gcloud |

### Identities
| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| Staff admin user | `staff-test@…` (roles staff+admin), password in <store> | web C1–C4 | needs person: password | docs/uat/UAT-…-2026-08-26 |
| Resident user | `<email>` tenant `<property>` | mobile M1–M3 | auto-resolved | docs/uat/UAT-MH-2185… |
| API token | env `UAT_API_TOKEN` (obtained via `/auth/login` as staff admin) | api A1–A5 | needs person: login | — |

### Infrastructure provider and CLI access
Detected by `scripts/check_infra_cli.py` from the repo (Terraform providers, deploy
workflows, platform config); one row per provider, each probed for installation and
authentication with a read-only command. The provider decides the CLI:

| Provider | CLI | Auth probe (read-only) | Log-in command when missing |
|:---------|:----|:-----------------------|:----------------------------|
| GCP | `gcloud` | `gcloud auth list --filter=status:ACTIVE` | `gcloud auth login` (+ `gcloud config set project`) |
| AWS | `aws` | `aws sts get-caller-identity` | `aws sso login --profile <p>` or `aws configure` |
| Azure | `az` | `az account show` | `az login` |
| Fly.io | `flyctl` | `flyctl auth whoami` | `flyctl auth login` |
| Vercel | `vercel` | `vercel whoami` | `vercel login` |
| Cloudflare | `wrangler` | `wrangler whoami` | `wrangler login` |
| Kubernetes | `kubectl` | `kubectl config current-context` | obtain the cluster kubeconfig |
| Terraform | `terraform` | presence only | install (pinned version) |
| GitHub | `gh` | `gh auth status` | `gh auth login` |
| Neon (optional) | `neonctl` | `neonctl me` | `neonctl auth` — `psql` with a read-only URL is acceptable instead |

| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| Provider | GCP (from `infra/**/*.tf` provider "google", `deploy-develop.yml`) | — | verified | check_infra_cli.py |
| gcloud | installed <version>, active account `<user>@<domain>`, project `<project>` selectable | build check, drift, jobs | verified | `gcloud auth list`, `gcloud projects describe` |
| terraform | installed <version> | drift | verified | `terraform version` |
| Database read | Neon develop, read-only role, `DATABASE_URL_RO` in env | DB assertions | needs person | — |
| GitHub | `gh auth status` OK for `<owner>/<repo>` | PR read, comment draft | verified | gh |

### Feature flags / config
| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| `<FLAG>` | expected `true` on the service | criterion C2 | verified via gcloud describe | spec §4 |

### Seed data
| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| Lease ending in ≤90 days | `<lease id>` under `<property>` | C1 | auto-resolved (prior UAT) | docs/uat/… |

### Third-party sandboxes
| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| Zoho sandbox org | `<org id>`, token via worker SA (read-only probe) | R2 cross-check | verified | docs/uat/… |

### Devices / simulators
| Row | Value / locator | Purpose | Status | Source |
|:----|:----------------|:--------|:-------|:-------|
| Android emulator `uat` | API 34, geo fix required after launch | M1–M3 | verified booted | agent-device devices |
| App build | `<flavor>` pointed at develop, version `<v+build>` | M1–M3 | needs person: build path | — |

### Asked of the person (consolidated)
1. <row> — <what exactly to provide and where to put it>
2. …
```

## Where to mine each row

| Row family | Mine first | Then |
|:-----------|:-----------|:-----|
| Environments | `docs/uat/` records' "Environment" sections; `infra/environments/<env>/`; `.loop/config.yaml` | `gcloud run services list` |
| Identities | prior UAT records (they name the QA identities and roles) | `docs/specs/` §14 Security & permissions for the role the criterion needs |
| Cloud/CLI | `gcloud config get-value account`, `gh auth status` | the repo's `CLAUDE.md` / runbooks for project ids |
| Flags/config | spec §4 Prerequisites, §18 Restrictions | `gcloud run services describe … env` |
| Seed data | prior UAT records' identifiers (lease ids, property ids) | a read-only DB query planned in the manifest |
| Sandboxes | prior UAT records; spec §3 | — |
| Devices | `agent-device devices`; the plugin agent's known-device notes | — |

## Verification commands

Each verification is read-only and its output is kept as `evidence/00-manifest-*.txt`:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "$WEB_URL"
curl -sS -o /dev/null -w '%{http_code}\n' "$API_URL/health"
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(status.latestReadyRevisionName,spec.template.spec.containers[0].image)'
gh auth status
agent-device devices
```
