# `docs/uat/` run-record format

What `/uat` writes into the target repository so the next run can mine it. The shape extends
the Marketplace records (`docs/uat/UAT-<scope>-<date>.md` + `evidence-<date>-<slug>/`) into one
folder per run with a machine-readable twin.

## Contents

- Folder layout
- `README.md` (human record)
- `run.json` (machine record)
- Re-run behavior
- Compatibility with existing flat records

## Folder layout

```
docs/uat/UAT-<scope>-<YYYY-MM-DD>/
  README.md
  run.json
  report.html
  drafts/
    ticket-comment.md
    pr-comment.md
  evidence/
    manifest.json
    NN-<lane>-<case>-<slug>.<ext>
```

`<scope>`: ticket key, `PR-<n>`, epic key, or `unscoped-<slug>`. One folder per scope and date;
a same-day re-run reuses the folder.

## `README.md` (human record)

Mirrors the Marketplace UAT shape so existing readers and `match_prior_uat.py` parse it the
same way:

```markdown
# UAT — <KEY>: <title> (<date>)

> **Execution status:** 🟢 EXECUTED <date> (<hh:mm–hh:mm> UTC) against the deployed **<env>**
> environment (<project>, <region>), revision `<rev>` on image `<sha>` (PR #<n>, merge `<sha>`).
> Report: <ARTIFACT_URL>. Evidence in `evidence/`.

**Scope:** <one paragraph — the use case, from the Context Frame.>

## Context Frame
<the frame, verbatim>

## Environment
- API: … · Web: … · Jobs: … · Identity: <role> (QA identity)

## Access manifest (redacted)
| Row | Locator | Status |
|---|---|---|

## Manifest gaps
- <anything requested after Step 2, with the criterion that needed it> (or "none")

## Criteria and verdicts
| id | Criterion | Source | Kind | Lane | Verdict | Evidence |
|---|---|---|---|---|---|---|
| C1 | … | spec §8.2 | NEW | web | 🟢 PASS | `07-web-C1-….png` |
| R1 | … | UAT-develop-…-2026-08-18 B-4 | RERUN | web | 🔴 FAIL | `08-…` |

## Deviations (spec vs PR)
| Spec said | PR delivered | Consequence | Status |
|---|---|---|---|

## Findings
- **F-1 [HIGH] (confidence 9/10)** … (finding entries per the findings template)

## Production-validator
Verdict: <PASS|PASS_WITH_WARNINGS|BLOCK> — <summary>; commands and outputs in `evidence/`.

## Terraform drift
<table | "Not applicable: the repository contains no Terraform.">

## Run history
| Run | Date | Build | Verdict | Report |
|---|---|---|---|---|
| 1 | … | … | … | <ARTIFACT_URL> |
```

Verdict glyphs match the existing records: 🟢 PASS · 🔴 FAIL · 🟡 BLOCKED.

## `run.json` (machine record)

```json
{
  "schema": "somnio-uat-run/1",
  "scope": "MH-2185",
  "input_kind": "ticket",
  "unscoped": false,
  "tickets": ["MH-2185"],
  "epic": "MH-2135",
  "pr": {"number": 912, "url": "…", "merge_sha": "…", "base": "develop"},
  "env": {"name": "develop", "project": "mh-develop-497000", "region": "us-east5",
          "web": "https://…", "api": "https://…"},
  "build": {"revision": "marketplace-api-00394-gxh", "image": "…:b6fb841"},
  "runs": [{"n": 1, "started_at": "…", "finished_at": "…", "verdict": "FAIL"}],
  "artifact_url": "https://claude.ai/…",
  "context_frame": {"use_case": "…", "predecessors": ["MH-2068"], "reproduction": ["…"],
                    "prior_uat_rerun": ["docs/uat/UAT-develop-e2e-…-2026-08-18.md"],
                    "prior_uat_considered": [{"path": "…", "score": 3}]},
  "manifest": [{"row": "Staff admin user", "locator": "…", "status": "verified", "purpose": "C1-C4"}],
  "manifest_gaps": [],
  "criteria": [{"id": "C1", "text": "…", "source": "spec §8.2", "kind": "NEW", "lane": "web",
                "verdict": "PASS", "evidence": ["07-web-C1-bell-panel-open.png"]}],
  "deviations": [{"spec": "…", "delivered": "…", "consequence": "…", "status": "F-1"}],
  "findings": [{"id": "F-1", "severity": "HIGH", "confidence": 9, "title": "…",
                "criteria": ["C1"], "origin": "web", "evidence": ["…"]}],
  "validator": {"verdict": "PASS_WITH_WARNINGS", "findings": []},
  "terraform": {"applicable": true, "verdict": "DRIFT", "items": []},
  "posted": {"ticket": null, "pr": null}
}
```

## Re-run behavior

- Same scope → same folder, `runs[]` gains an entry, `artifact_url` is reused, evidence `NN`
  continues from the manifest's last sequence.
- Criteria are reloaded from `run.json`; a spec change since run 1 is a new Deviations row, not
  a silent criteria edit.
- `README.md` `## Run history` gains a row; the criteria table shows the latest verdict with the
  previous one in parentheses when it changed (`🟢 PASS (was 🔴)`).

## Compatibility with existing flat records

`match_prior_uat.py` reads both shapes: flat `docs/uat/UAT-*.md` files (Marketplace) and
`docs/uat/UAT-*/README.md` folders (this skill). It extracts case rows from any markdown table
whose header contains a verdict-like column (`Result`, `Verdict`) and an evidence column, so
old records are re-runnable without conversion.
