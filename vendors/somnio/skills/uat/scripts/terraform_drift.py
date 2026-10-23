#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Terraform / environment drift check for /uat Step 6.

Standard library only. Runs `terraform plan -detailed-exitcode` in the
environment's root module, parses the plan through `terraform show -json`,
and optionally diffs a live service inventory (one name per line, e.g. from
`gcloud run services list --format='value(metadata.name)'`) against the
service names Terraform actually manages. The live-vs-state comparison uses
the resource's `name` attribute, never the state address — module addresses
like `module.cloud_run_web.google_cloud_run_v2_service.api` never contain the
deployed service name (Marketplace validator finding, 2026-08-07).

Plan exit code 1 is reported as UNKNOWN, never as "no drift": a plan that
errors (stale init, missing tfvars, bad credentials) has said nothing about
drift.

Usage:
    uv run terraform_drift.py --dir infra/environments/develop \
        [--live-services live.txt] [--service-types google_cloud_run_v2_service,google_cloud_run_v2_job] \
        [--var-file develop.tfvars]

What the verdict means: `terraform plan` reports the difference between the
configuration and the refreshed remote state, so DRIFT covers both undeclared
live changes and un-applied code; the report lists each planned action so the
reader can tell them apart. State locking is left on.

stdout: JSON {verdict, plan_exit, changes: [{address, actions, type, name}], unmanaged_live: [], missing_live: []}
stderr: progress + the human summary
exit:   0 NO_DRIFT · 1 DRIFT · 3 UNKNOWN (plan errored / terraform missing) · 2 usage
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# terraform plan -detailed-exitcode contract: 0 = no changes, 1 = error, 2 = changes.
PLAN_NO_CHANGES, PLAN_ERROR, PLAN_CHANGES = 0, 1, 2
# A plan against a remote backend can take minutes on large states; ten minutes
# is the ceiling before a hung provider is reported instead of waited on.
PLAN_TIMEOUT_S = 600
DEFAULT_SERVICE_TYPES = "google_cloud_run_v2_service,google_cloud_run_v2_job,google_cloud_run_service"


def run(cmd: list[str], cwd: Path, timeout: int = PLAN_TIMEOUT_S) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True, help="Terraform root module for the environment")
    parser.add_argument("--live-services", help="file with one live service/job name per line")
    parser.add_argument("--service-types", default=DEFAULT_SERVICE_TYPES,
                        help="comma-separated resource types whose `name` is compared to the live list")
    parser.add_argument("--var-file", action="append", default=[], help="passed through as -var-file")
    args = parser.parse_args()

    tf_dir = Path(args.dir)
    if not tf_dir.is_dir() or not list(tf_dir.glob("*.tf")):
        print(f"error: {tf_dir} has no *.tf files — Step 6 is 'not applicable' when the repo has no Terraform",
              file=sys.stderr)
        return 2
    if shutil.which("terraform") is None:
        print(json.dumps({"verdict": "UNKNOWN", "reason": "terraform binary not on PATH"}))
        print("UNKNOWN: terraform not installed — install it or record the step as UNKNOWN", file=sys.stderr)
        return 3

    var_args = [a for vf in args.var_file for a in ("-var-file", vf)]
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / "uat.tfplan"
        try:
            # State locking stays ON: a concurrent apply must make this plan wait or fail
            # (→ UNKNOWN), never produce a snapshot of a state that never existed coherently.
            plan = run(["terraform", "plan", "-detailed-exitcode", "-input=false", "-no-color",
                        f"-out={plan_file}", *var_args], tf_dir)
        except subprocess.TimeoutExpired:
            print(json.dumps({"verdict": "UNKNOWN", "reason": f"terraform plan exceeded {PLAN_TIMEOUT_S}s"}))
            print("UNKNOWN: plan timed out", file=sys.stderr)
            return 3
        if plan.returncode == PLAN_ERROR:
            # Provider diagnostics can echo variable values and backend URLs: keep the tail
            # short and scrub the obvious credential shapes before it reaches the transcript.
            tail = "\n".join(plan.stderr.strip().splitlines()[-15:])
            tail = re.sub(r"(?i)(token|secret|password|key|credential)s?\s*[:=]\s*\S+", r"\1=<redacted>", tail)
            tail = re.sub(r"[a-z]+://[^/\s]*@", "<redacted>@", tail)
            print(json.dumps({"verdict": "UNKNOWN", "plan_exit": PLAN_ERROR, "reason": tail}))
            print(f"UNKNOWN: terraform plan errored — this is not a pass:\n{tail}", file=sys.stderr)
            return 3

        changes: list[dict] = []
        managed_names: dict[str, list[str]] = {}
        show = run(["terraform", "show", "-json", str(plan_file)], tf_dir)
        if show.returncode != 0:
            print(json.dumps({"verdict": "UNKNOWN", "plan_exit": plan.returncode, "reason": show.stderr.strip()[-500:]}))
            print("UNKNOWN: terraform show -json failed", file=sys.stderr)
            return 3
        try:
            doc = json.loads(show.stdout)
        except json.JSONDecodeError as exc:
            print(json.dumps({"verdict": "UNKNOWN", "reason": f"unparseable plan JSON: {exc}"}))
            return 3

        for rc in doc.get("resource_changes", []) or []:
            actions = rc.get("change", {}).get("actions", [])
            if actions and actions != ["no-op"]:
                after = rc.get("change", {}).get("after") or {}
                changes.append({"address": rc.get("address"), "type": rc.get("type"), "actions": actions,
                                "name": after.get("name") if isinstance(after, dict) else None})

        service_types = {t.strip() for t in args.service_types.split(",") if t.strip()}

        def walk(module: dict) -> None:
            for res in module.get("resources", []) or []:
                if res.get("type") in service_types:
                    name = (res.get("values") or {}).get("name")
                    if name:
                        managed_names.setdefault(name, []).append(res.get("address", "?"))
            for child in module.get("child_modules", []) or []:
                walk(child)

        # prior_state carries what Terraform currently manages; planned_values what it will.
        for root_key in ("prior_state", "planned_values"):
            root = doc.get(root_key) or {}
            values = root.get("values", root)
            walk(values.get("root_module", {}) if isinstance(values, dict) else {})

    unmanaged_live: list[str] = []
    missing_live: list[str] = []
    if args.live_services:
        try:
            live = {ln.strip() for ln in Path(args.live_services).read_text(encoding="utf-8").splitlines() if ln.strip()}
        except OSError as exc:
            print(f"error: cannot read {args.live_services}: {exc}", file=sys.stderr)
            return 2
        if not live:
            print("warning: live-services file is EMPTY — an empty inventory makes every check vacuous; "
                  "verify the gcloud command before trusting this result", file=sys.stderr)
        unmanaged_live = sorted(live - managed_names.keys())
        missing_live = sorted(managed_names.keys() - live)

    # Read through the named contract rather than a bare integer, so the constants at the
    # top document something the code actually uses. `changes` is normally non-empty
    # whenever the plan reports 2, but a plan that reports changes while the parsed
    # resource_changes come back empty is drift we must not round down to NO_DRIFT.
    drift = bool(changes or unmanaged_live or missing_live or plan.returncode == PLAN_CHANGES)
    verdict = "DRIFT" if drift else "NO_DRIFT"
    out = {"verdict": verdict, "plan_exit": plan.returncode, "changes": changes,
           "managed_services": {k: v for k, v in sorted(managed_names.items())},
           "unmanaged_live": unmanaged_live, "missing_live": missing_live}
    print(json.dumps(out, indent=2))

    print(f"{verdict}: {len(changes)} planned change(s), {len(unmanaged_live)} live service(s) not in Terraform, "
          f"{len(missing_live)} managed service(s) not live", file=sys.stderr)
    for c in changes[:20]:
        print(f"  {'/'.join(c['actions']):<14} {c['address']}", file=sys.stderr)
    for s in unmanaged_live:
        print(f"  UNMANAGED live: {s}", file=sys.stderr)
    for s in missing_live:
        print(f"  MISSING live:   {s} (managed at {', '.join(managed_names[s])})", file=sys.stderr)
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
