# Evidence specification per lane

What every case verdict must carry, how artifacts are named and stored, and what is redacted
before anything is written under `docs/uat/`. A verdict without the lane's required evidence is
recorded as `BLOCKED (no evidence)`, never as `PASS`.

## Contents

- Required evidence per lane
- Naming
- Storage layout
- Redaction
- Manifest entry shape
- Contract check output

## Required evidence per lane

| Lane | Mandatory per case | Also when applicable |
|:-----|:-------------------|:---------------------|
| **api** | one `.http.json` per validated call: method, URL, request headers (redacted), request body, status, response headers (redacted), response body, `latency_ms`; plus the `check_api_contract.py` JSON result for that response | DB assertion output (`.sql.json`: query, row count, sample rows with PII stripped) |
| **web** | screenshots at: entry state, after each state-changing interaction, the asserted end state, **every failure at the moment it occurs**; `console.txt` and `network.txt` captured after the end state | responsive shots when the criterion is layout; the failing request's own entry saved as `.http.json` (a raw `.har` is refused by the collector — it carries cookies and auth headers it cannot redact) |
| **mobile** | one device screenshot per step, one full-flow recording (`.mp4`/`.gif`), `device-evidence.json` (device, OS, app build, sha, scenario, observed) | logs excerpt for a failure |
| **all** | `run.json` case entry with `env`, `build`, `started_at`, `duration_ms` | timing series when the criterion is performance (spec §17) |

Timings are reported, never asserted from a loaded machine: if other agents or builds were
running concurrently, say so next to the number.

## Naming

`NN-<lane>-<case>-<slug>.<ext>` — `NN` a two-digit sequence per run (continues across re-runs),
`lane` in `api|web|mobile|db|infra|manifest`, `case` the criterion id (`C3`, `R1` for re-runs,
`F2` for a finding's reproduction), `slug` kebab-case ≤ 40 chars. Examples:

```
00-manifest-run-health-check.txt
03-api-C3-pipeline-response.http.json
04-api-C3-pipeline-contract-check.json
07-web-C1-bell-panel-open.png
08-web-C1-console.txt
12-mobile-M2-renewal-screen.png
13-mobile-M2-full-flow.mp4
20-infra-C0-terraform-plan.json
```

`collect_evidence.py` assigns `NN` and refuses a name that already exists.

## Storage layout

```
docs/uat/UAT-<scope>-<YYYY-MM-DD>/
  README.md            human-readable record (Marketplace UAT shape)
  run.json             machine record: criteria, verdicts, evidence index, artifact_url
  report.html          the Artifact source (redeployed to the same URL on re-run)
  evidence/
    manifest.json      one entry per artifact (below)
    NN-…               the artifacts
```

`<scope>` is the ticket key (`MH-2185`), the PR (`PR-912`) when no ticket, the epic key, or
`unscoped-<slug>`. Binary artifacts over 5 MB are stored but the report embeds a downscaled
copy (max 1600 px wide) to stay under the Artifact size limit.

## Redaction

Applied by `collect_evidence.py` to every text/JSON artifact before writing (binary files are
copied as-is; screenshots are the person's responsibility — crop or excise PII before capture,
as the Marketplace records do with contact cards):

| Pattern | Replacement |
|:--------|:------------|
| `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `X-Auth-Token`, `Proxy-Authorization` header values | `<redacted>` |
| `Bearer <token>`, `Basic <b64>` anywhere | `Bearer <redacted>` / `Basic <redacted>` |
| JSON keys matching `/(password|passwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token|session)/i` | value → `<redacted>` |
| JWT-shaped strings (`xxx.yyy.zzz` base64url ≥ 20 chars per segment) | `<redacted-jwt>` |
| Email addresses (except allow-listed QA identities, `--keep-email`) | `<email-redacted>` |
| Phone numbers (E.164 or `(xxx) xxx-xxxx` shapes) | `<phone-redacted>` |
| Connection strings (`postgres://…`, `mysql://…`, `mongodb://…`) | scheme kept, credentials and host → `<redacted>` |
| GCP/AWS key shapes (`AIza…`, `AKIA…`, `-----BEGIN … PRIVATE KEY-----` blocks) | `<redacted-key>` |

The collector prints the count of redactions per pattern to stderr and records it in the
manifest entry, so a zero-redaction API capture that should have carried an auth header is
visibly suspicious.

## Manifest entry shape

```json
{
  "seq": 3,
  "file": "03-api-C3-pipeline-response.http.json",
  "lane": "api",
  "case": "C3",
  "slug": "pipeline-response",
  "sha256": "…",
  "bytes": 2481,
  "redactions": {"authorization_header": 1, "email": 0},
  "source": "/tmp/resp.json",
  "captured_at": "2026-09-03T14:02:11Z",
  "env": "develop",
  "build": "b6fb841"
}
```

## Contract check output

`check_api_contract.py` emits one JSON object per checked response:

```json
{
  "path": "/api/v1/staff/leasing/newlease/pipeline",
  "method": "GET",
  "status": 200,
  "schema": "#/components/schemas/PipelineResponse",
  "verdict": "MISMATCH",
  "missing": ["asOf"],
  "extra": ["debugTiming"],
  "type_mismatch": [{"field": "total", "expected": "integer", "actual": "string"}],
  "checked_fields": 14
}
```

Each `missing`, `extra`, or `type_mismatch` entry is a finding in the report with severity
per the findings template.
