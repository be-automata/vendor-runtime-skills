#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Detect the infrastructure providers a repo deploys to and verify their CLIs are installed and authenticated.

Standard library only. Part of /uat Step 2 (Access & Credentials Manifest):
the manifest must name every cloud/CLI access the run needs *before* any test
runs, and "gcloud is not logged in" discovered twenty minutes into a run is
exactly the interruption Step 2 exists to prevent.

Detection reads the repo (never the network): Terraform `provider`/`required_providers`
blocks, GitHub deploy workflows (setup-gcloud, configure-aws-credentials, azure/login,
flyctl, vercel), platform config files (app.yaml, serverless.yml, fly.toml, vercel.json,
netlify.toml, wrangler.toml, kubeconfig/k8s manifests, Neon/Prisma/DB URLs in env
examples). Each provider maps to the CLI that operates it and a read-only auth probe:

    gcp     gcloud     gcloud auth list --filter=status:ACTIVE --format=value(account)
    aws     aws        aws sts get-caller-identity
    azure   az         az account show
    fly     flyctl     flyctl auth whoami
    vercel  vercel     vercel whoami
    cloudflare wrangler wrangler whoami
    kubernetes kubectl kubectl config current-context
    terraform terraform terraform version         (presence only)
    github  gh         gh auth status
    neon    neonctl    neonctl me                  (optional: psql may be enough)

Usage:
    uv run check_infra_cli.py [--repo .] [--require gh,terraform] [--optional neon] [--no-auth-probe]

stdout: JSON {verdict, scan_truncated, providers: [{provider, cli, detected_by[], installed, version, authenticated, identity, required}]}
stderr: a row per provider
exit:   0 READY (every required CLI installed and its auth probe passed) ·
        1 MISSING / INSTALLED_ONLY (--no-auth-probe never grades READY) / INCOMPLETE_SCAN ·
        2 usage

Probe semantics, stated plainly: `gcloud auth list` proves an active account,
not authorization on the target project — the manifest names the project and
`gcloud projects describe <project>` is the per-run check; `aws sts
get-caller-identity` and `az account show` prove an identity; `kubectl
cluster-info` round-trips to the API server; terraform has no auth concept
(presence only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Auth probes are read-only and cheap; 20 s covers a slow token refresh without
# letting a hung CLI stall the manifest step.
PROBE_TIMEOUT_S = 20
# 20k tracked-ish files covers a large monorepo once node_modules/build dirs are skipped.
# Hitting the cap is reported as INCOMPLETE_SCAN (exit 1): a provider hiding past the
# cutoff must never turn into a silently absent requirement.
SCAN_LIMIT_FILES = 20000

PROVIDERS: dict[str, dict] = {
    "gcp": {"cli": "gcloud", "version": ["gcloud", "--version"],
            "auth": ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]},
    "aws": {"cli": "aws", "version": ["aws", "--version"],
            "auth": ["aws", "sts", "get-caller-identity", "--output", "json"]},
    "azure": {"cli": "az", "version": ["az", "version", "--output", "json"],
              "auth": ["az", "account", "show", "--output", "json"]},
    "fly": {"cli": "flyctl", "version": ["flyctl", "version"], "auth": ["flyctl", "auth", "whoami"]},
    "vercel": {"cli": "vercel", "version": ["vercel", "--version"], "auth": ["vercel", "whoami"]},
    "cloudflare": {"cli": "wrangler", "version": ["wrangler", "--version"], "auth": ["wrangler", "whoami"]},
    # cluster-info actually round-trips to the API server; current-context only reads a file.
    "kubernetes": {"cli": "kubectl", "version": ["kubectl", "version", "--client", "--output=json"],
                   "auth": ["kubectl", "cluster-info", "--request-timeout=10s"]},
    "terraform": {"cli": "terraform", "version": ["terraform", "version"], "auth": None},
    "github": {"cli": "gh", "version": ["gh", "--version"], "auth": ["gh", "auth", "status"]},
    "neon": {"cli": "neonctl", "version": ["neonctl", "--version"], "auth": ["neonctl", "me"]},
}

# (provider, regex, file glob it applies to). Detection is by content, never by directory name alone.
SIGNALS: list[tuple[str, re.Pattern, tuple[str, ...]]] = [
    ("gcp", re.compile(r'provider\s+"google(-beta)?"|hashicorp/google|google-github-actions/(auth|setup-gcloud|deploy-cloudrun)|gcloud\s+(run|app|builds)|runtime:\s*(nodejs|python|go)\d*\s*$', re.M), ("*.tf", "*.yml", "*.yaml", "*.sh", "Makefile")),
    ("aws", re.compile(r'provider\s+"aws"|hashicorp/aws|aws-actions/configure-aws-credentials|aws\s+(ecs|lambda|s3|cloudformation|eks)\s|serverless\.yml|provider:\s*\n\s*name:\s*aws', re.M), ("*.tf", "*.yml", "*.yaml", "*.sh", "Makefile")),
    ("azure", re.compile(r'provider\s+"azurerm"|hashicorp/azurerm|azure/login|az\s+(webapp|aks|containerapp)\s', re.M), ("*.tf", "*.yml", "*.yaml", "*.sh", "Makefile")),
    ("fly", re.compile(r'^app\s*=|superfly/flyctl-actions|flyctl\s+deploy', re.M), ("fly.toml", "*.yml", "*.yaml", "*.sh")),
    ("vercel", re.compile(r'vercel\s+(deploy|--prod)|amondnet/vercel-action|"vercel"\s*:', re.M), ("vercel.json", "*.yml", "*.yaml", "*.sh", "package.json")),
    ("cloudflare", re.compile(r'cloudflare/wrangler-action|wrangler\s+(deploy|publish)|^name\s*=.*\n(?:.*\n)*?compatibility_date', re.M), ("wrangler.toml", "wrangler.jsonc", "*.yml", "*.yaml", "*.sh")),
    ("kubernetes", re.compile(r'^apiVersion:\s*(apps/v1|v1)\s*\n\s*kind:\s*(Deployment|Service|Ingress|StatefulSet)|kubectl\s+(apply|rollout)', re.M), ("*.yml", "*.yaml", "*.sh", "Makefile")),
    ("terraform", re.compile(r'^\s*(resource|module|provider)\s+"', re.M), ("*.tf",)),
    ("neon", re.compile(r'neon\.tech|neonctl|neondatabase/', re.M), ("*.yml", "*.yaml", "*.env.example", "*.md", "*.tf", "*.json")),
]
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", ".terraform", "vendor", "__pycache__", ".venv"}


def scan(repo: Path) -> tuple[dict[str, set[str]], bool]:
    """Return (hits per provider, truncated). Truncation is a verdict, not a warning.

    os.walk with in-place pruning, deliberately, not Path.rglob: rglob descends into
    every directory and leaves filtering to the caller, so a repo with node_modules
    (100k+ entries) was fully walked before SKIP_DIRS discarded it — and because
    skipped entries never reached the counter, SCAN_LIMIT_FILES bounded nothing.
    Pruning dirnames stops the descent at the directory itself.
    """
    hits: dict[str, set[str]] = {}
    count = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]   # prune, do not descend
        for name in filenames:
            count += 1
            if count > SCAN_LIMIT_FILES:
                print(f"warning: stopped scanning after {SCAN_LIMIT_FILES} files — verdict is INCOMPLETE_SCAN",
                      file=sys.stderr)
                return hits, True
            path = Path(dirpath) / name
            # Read at most once per file, not once per matching signal: a .yml matches
            # 7 of the 9 signal globs and was being re-read and re-decoded each time.
            text: str | None = None
            for provider, pattern, globs in SIGNALS:
                if not any(path.match(g) for g in globs):
                    continue
                if text is None:
                    try:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        break
                if pattern.search(text):
                    hits.setdefault(provider, set()).add(str(path.relative_to(repo)))
    return hits, truncated


def probe(cmd: list[str] | None) -> tuple[bool, str]:
    if cmd is None:
        return True, ""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe failed: {exc}"
    out = (res.stdout or res.stderr).strip()
    return res.returncode == 0 and bool(out), out.splitlines()[0][:120] if out else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--require", default="github", help="comma-separated providers always required (default: github)")
    parser.add_argument("--optional", default="neon", help="comma-separated providers reported but never blocking")
    parser.add_argument("--no-auth-probe", action="store_true", help="check installation only")
    args = parser.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2
    required = {p.strip() for p in args.require.split(",") if p.strip()}
    optional = {p.strip() for p in args.optional.split(",") if p.strip()}
    unknown = (required | optional) - PROVIDERS.keys()
    if unknown:
        print(f"error: unknown provider(s) {sorted(unknown)}; known: {sorted(PROVIDERS)}", file=sys.stderr)
        return 2

    hits, truncated = scan(repo)
    detected = set(hits) | required
    rows = []
    for provider in sorted(detected):
        spec = PROVIDERS[provider]
        installed = shutil.which(spec["cli"]) is not None
        version = probe(spec["version"])[1] if installed else ""
        # authenticated: True/False when probed; None when not probed (--no-auth-probe) or
        # when the provider has no auth probe (terraform: presence only). None is never READY.
        authed: bool | None = None
        identity = ""
        if installed and spec["auth"] is None:
            authed, identity = None, "(presence only — no auth concept)"
        elif installed and not args.no_auth_probe:
            authed, identity = probe(spec["auth"])
            identity = re.sub(r"[A-Za-z0-9._%+-]+@", "<user>@", identity)  # keep the domain, drop the user
        elif installed:
            authed, identity = None, "(auth NOT probed — --no-auth-probe)"
        rows.append({
            "provider": provider, "cli": spec["cli"], "detected_by": sorted(hits.get(provider, set()))[:5],
            "installed": installed, "version": version, "authenticated": authed, "identity": identity,
            "required": provider not in optional,
        })

    def satisfied(r: dict) -> bool:
        if not r["installed"]:
            return False
        return r["authenticated"] is True or (r["authenticated"] is None and PROVIDERS[r["provider"]]["auth"] is None)

    blocking = [r for r in rows if r["required"] and not satisfied(r)]
    if truncated:
        verdict = "INCOMPLETE_SCAN"
    elif args.no_auth_probe:
        verdict = "INSTALLED_ONLY"  # never READY without an auth probe
    else:
        verdict = "READY" if not blocking else "MISSING"
    print(json.dumps({"verdict": verdict, "scan_truncated": truncated, "providers": rows}, indent=2))
    for r in rows:
        state = ("ok" if satisfied(r) else "not probed" if r["installed"] and r["authenticated"] is None
                 else "not authenticated" if r["installed"] else "NOT INSTALLED")
        flag = "" if r["required"] else " (optional)"
        print(f"{r['provider']:<11} {r['cli']:<10} {state:<18} {r['identity']}{flag}", file=sys.stderr)
    if blocking:
        print("manifest gap: " + ", ".join(f"{r['cli']} ({'install' if not r['installed'] else 'log in'})" for r in blocking),
              file=sys.stderr)
    if not hits:
        print("no infrastructure signals found in the repo — confirm the deploy target by hand", file=sys.stderr)
    return 0 if verdict == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
