#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Network-vendor detection for /harness-init: which hosts this repository needs.

Read-only: this script writes nothing to <root> or anywhere else, makes no
network call of its own, and never reads a real `.env`. It answers one question
deterministically — which third-party services do this repository's own
dependencies, providers, workflows and env-var names name? — and turns the
answer into a `.claude/settings.json` fragment the person reviews and a list of
gaps the person accepts.

Why it exists: the shipped `templates/target-settings.json` carries three
allowed domains (registry.npmjs.org, github.com, api.github.com). Every repo
that talks to anything else — Neon, Cloudflare, Sentry, Stripe — would need a
hand-edited sandbox block per repository. This script makes the repository's
own dependencies decide its network policy, so the hand-edit becomes a review.

Measured inside a real sandboxed target (marketplace-monorepo, plugin 0.9.6):
DNS is blocked at the kernel, all egress goes through an HTTP CONNECT proxy
injected as HTTPS_PROXY/https_proxy, curl and python urllib honour it, Node's
global fetch does not, and a raw TCP socket (a `pg` client to :5432) cannot be
brokered at all. `references/network-vendors.md` records those facts and the
gap catalogue.

Usage:
    uv run detect_network.py --root <path>
    uv run detect_network.py --list-vendors

`--list-vendors` prints the vendor table (id, name, domains, CLIs) and exits
without scanning; no root is needed.

stdout: on success, a single JSON object with exactly the keys `root`,
        `runtime`, `vendors`, `settings_fragment`, `gaps` and `sources`;
        on error, nothing.
stderr: on success, a one-line summary; on error, one line beginning "error:".
exit:   0 JSON emitted (detection is advisory and never fails a run)
        2 bad arguments or an unreadable root
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# _common.py ships beside this script and owns the constants more than one of
# these scripts needs. Imported, never copied. Byte-compilation is refused for
# the same reason detect_stack.py refuses it: a plugin cache is read-only by
# contract and a __pycache__/ beside these scripts is a write nobody asked for.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import EXCLUDED_DIRS, _resolve_asset  # noqa: E402

# The three domains templates/target-settings.json already ships. The fragment
# is always the union of these and whatever the scan found, so a repo that
# needs nothing extra still gets a valid, complete block.
BASELINE_DOMAINS = ("api.github.com", "github.com", "registry.npmjs.org")

# ---------------------------------------------------------------------------
# The vendor table.
#
# One row per third-party service a Somnio repository reaches. Alphabetical by
# id, and meant to be extended by PR — a new row is data, never code. Fields:
#
#   id          stable machine id; appears in evidence, gaps and the summary
#   name        display name
#   deps        package names in package.json / pubspec.yaml / requirements /
#               pyproject. A signal ending in "/" or "-" is a prefix match
#               (`@aws-sdk/` matches `@aws-sdk/client-s3`); anything else is
#               matched exactly. HIGH confidence.
#   files       paths, relative to the root, whose existence names the vendor
#               (`fly.toml`, `.github`). HIGH confidence.
#   providers   terraform provider names read from *.tf / *.tf.json. HIGH.
#   actions     `uses:` prefixes in .github/workflows/*.yml. HIGH.
#   env         env-var NAMES read from .env.example/.sample/.template (never
#               values, never a real .env). MEDIUM confidence.
#   hosts       hostname fragments matched in tracked source files. MEDIUM.
#   domains     what the vendor needs in sandbox.network.allowedDomains. Every
#               row must carry at least one real domain — a row without one
#               cannot change a repository's policy and is a documentation
#               entry, not a vendor.
#   clis        the vendor's CLI binaries. A CLI reaches the fragment's
#               excludedCommands ONLY when it is already in the shipped
#               template's list; this table never invents a new escape.
#   transports  "https" for anything a proxy can broker; a "*-tcp" transport
#               says a raw socket is involved and cannot be.
#
# Some rows carry a trailing `# network-cli: prose` marker. That is the
# documented opt-out of scripts/check_network_cli.py: a value such as
# `"clis": ["aws"]` is a list literal whose first element is a tool name, which
# is exactly the shape the validator reads as an invocation. Nothing here runs
# any CLI — this file is a table, and the marker says so.
# ---------------------------------------------------------------------------
VENDORS: list[dict] = [
    {
        "id": "anthropic", "name": "Anthropic",
        "deps": ["@anthropic-ai/", "anthropic", "anthropic-sdk"],
        "files": [], "providers": [], "actions": ["anthropics/"],
        "env": ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"],
        "hosts": ["api.anthropic.com"],
        "domains": ["api.anthropic.com"], "clis": [], "transports": ["https"],
    },
    {
        "id": "atlassian", "name": "Atlassian / Jira",
        "deps": ["jira-client", "jira.js", "atlassian-jwt", "atlassian-python-api"],
        "files": [], "providers": [], "actions": ["atlassian/"],
        "env": ["JIRA_API_TOKEN", "JIRA_BASE_URL", "ATLASSIAN_API_TOKEN", "JIRA_EMAIL"],
        "hosts": ["api.atlassian.com", "atlassian.net"],
        "domains": ["api.atlassian.com", "atlassian.net"], "clis": [],
        "transports": ["https"],
    },
    {
        "id": "aws", "name": "Amazon Web Services",
        "deps": ["@aws-sdk/", "aws-sdk", "aws-cdk-", "boto3", "botocore", "aws_client"],
        "files": [], "providers": ["aws"], "actions": ["aws-actions/"],  # network-cli: prose
        "env": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
                "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"],
        "hosts": ["amazonaws.com"],
        "domains": ["amazonaws.com", "sts.amazonaws.com"], "clis": ["aws"],  # network-cli: prose
        "transports": ["https"],
    },
    {
        "id": "azure", "name": "Microsoft Azure",
        "deps": ["@azure/", "azure-identity", "azure-storage-blob", "azure-mgmt-"],
        "files": [], "providers": ["azurerm", "azuread"], "actions": ["azure/"],
        "env": ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET",
                "AZURE_SUBSCRIPTION_ID"],
        "hosts": ["management.azure.com", "login.microsoftonline.com"],
        "domains": ["login.microsoftonline.com", "management.azure.com"],
        "clis": ["az"], "transports": ["https"],  # network-cli: prose
    },
    {
        "id": "cloudflare", "name": "Cloudflare",
        "deps": ["wrangler", "@cloudflare/", "cloudflare", "miniflare"],  # network-cli: prose
        "files": ["wrangler.toml", "wrangler.json", "wrangler.jsonc"],
        "providers": ["cloudflare"], "actions": ["cloudflare/"],
        "env": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CF_API_TOKEN",
                "CF_ACCOUNT_ID"],
        "hosts": ["api.cloudflare.com", "workers.dev"],
        "domains": ["api.cloudflare.com", "workers.dev"], "clis": ["wrangler"],  # network-cli: prose
        "transports": ["https"],
    },
    {
        "id": "datadog", "name": "Datadog",
        "deps": ["dd-trace", "@datadog/", "datadog-api-client", "ddtrace"],
        "files": [], "providers": ["datadog"], "actions": ["datadog/", "DataDog/"],
        "env": ["DD_API_KEY", "DD_APP_KEY", "DATADOG_API_KEY", "DD_SITE"],
        "hosts": ["datadoghq.com"],
        "domains": ["api.datadoghq.com", "datadoghq.com"], "clis": [],
        "transports": ["https"],
    },
    {
        "id": "dockerhub", "name": "Docker Hub",
        "deps": [],
        "files": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                  "compose.yaml"],
        "providers": ["docker"], "actions": ["docker/"],  # network-cli: prose
        "env": ["DOCKERHUB_TOKEN", "DOCKERHUB_USERNAME"],
        "hosts": ["docker.io"],
        "domains": ["auth.docker.io", "index.docker.io", "registry-1.docker.io"],
        "clis": ["docker"], "transports": ["https"],  # network-cli: prose
    },
    {
        "id": "flyio", "name": "Fly.io",
        "deps": [], "files": ["fly.toml"], "providers": ["fly"],
        "actions": ["superfly/"],
        "env": ["FLY_API_TOKEN", "FLY_APP_NAME"],
        "hosts": ["fly.dev", "api.fly.io"],
        "domains": ["api.fly.io", "registry.fly.io"], "clis": ["flyctl"],  # network-cli: prose
        "transports": ["https"],
    },
    {
        "id": "gcp", "name": "Google Cloud / Firebase",
        "deps": ["@google-cloud/", "google-cloud-", "firebase-admin", "firebase",
                 "firebase_core", "cloud_firestore", "firebase_auth",
                 "google-auth", "google-api-python-client"],
        "files": ["firebase.json", ".firebaserc"],
        "providers": ["google", "google-beta"],
        "actions": ["google-github-actions/", "firebase/", "FirebaseExtended/"],
        "env": ["GOOGLE_APPLICATION_CREDENTIALS", "GCP_PROJECT_ID", "GCLOUD_PROJECT",
                "FIREBASE_API_KEY", "FIREBASE_PROJECT_ID"],
        "hosts": ["googleapis.com", "firebaseio.com"],
        "domains": ["firebaseio.com", "googleapis.com", "oauth2.googleapis.com"],
        "clis": ["gcloud"], "transports": ["https"],  # network-cli: prose
    },
    {
        "id": "github", "name": "GitHub",
        "deps": ["@octokit/", "octokit", "PyGithub", "ghapi"],
        "files": [".github"], "providers": ["github"], "actions": ["actions/"],
        "env": ["GITHUB_TOKEN", "GH_TOKEN"],
        "hosts": ["api.github.com", "githubusercontent.com"],
        "domains": ["api.github.com", "github.com", "objects.githubusercontent.com",
                    "raw.githubusercontent.com"],
        "clis": ["gh"], "transports": ["https"],  # network-cli: prose
    },
    {
        "id": "mongodb-atlas", "name": "MongoDB Atlas",
        "deps": ["mongodb", "mongoose", "@nestjs/mongoose", "pymongo", "motor"],
        "files": [], "providers": ["mongodbatlas"], "actions": ["mongodb/"],
        "env": ["MONGODB_URI", "MONGO_URL", "MONGODB_ATLAS_PUBLIC_KEY"],
        "hosts": ["mongodb.net", "cloud.mongodb.com"],
        "domains": ["cloud.mongodb.com", "mongodb.net"], "clis": [],
        "transports": ["https", "mongodb-tcp"],
    },
    {
        "id": "neon", "name": "Neon",
        "deps": ["@neondatabase/serverless", "neonctl", "neon-serverless"],
        "files": [], "providers": ["neon"], "actions": ["neondatabase/"],
        "env": ["NEON_API_KEY", "NEON_DATABASE_URL", "NEON_PROJECT_ID"],
        "hosts": ["neon.tech"],
        "domains": ["api.neon.tech", "console.neon.tech"], "clis": ["neonctl"],  # network-cli: prose
        "transports": ["https", "postgres-tcp"],
    },
    {
        "id": "npm", "name": "npm registry",
        "deps": [], "files": ["package.json"], "providers": [], "actions": [],
        "env": ["NPM_TOKEN"],
        "hosts": ["registry.npmjs.org"],
        "domains": ["registry.npmjs.org"], "clis": [], "transports": ["https"],
    },
    {
        "id": "openai", "name": "OpenAI",
        "deps": ["openai", "@ai-sdk/openai", "langchain-openai"],
        "files": [], "providers": [], "actions": [],
        "env": ["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_BASE_URL"],
        "hosts": ["api.openai.com"],
        "domains": ["api.openai.com"], "clis": [], "transports": ["https"],
    },
    {
        "id": "planetscale", "name": "PlanetScale",
        "deps": ["@planetscale/database", "planetscale"],
        "files": [], "providers": ["planetscale"], "actions": ["planetscale/"],
        "env": ["PLANETSCALE_TOKEN", "PLANETSCALE_ORG", "PSCALE_TOKEN"],
        "hosts": ["psdb.cloud", "planetscale.com"],
        "domains": ["api.planetscale.com", "psdb.cloud"], "clis": ["pscale"],
        "transports": ["https", "mysql-tcp"],
    },
    {
        "id": "pub-dev", "name": "pub.dev",
        "deps": [], "files": ["pubspec.yaml"], "providers": [],
        "actions": ["dart-lang/", "subosito/flutter-action"],
        "env": ["PUB_HOSTED_URL"],
        "hosts": ["pub.dev"],
        "domains": ["pub.dev", "storage.googleapis.com"], "clis": [],
        "transports": ["https"],
    },
    {
        "id": "pypi", "name": "PyPI",
        "deps": [],
        "files": ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile"],
        "providers": [], "actions": ["pypa/"],
        "env": ["PYPI_TOKEN", "PIP_INDEX_URL"],
        "hosts": ["pypi.org", "pythonhosted.org"],
        "domains": ["files.pythonhosted.org", "pypi.org"], "clis": [],
        "transports": ["https"],
    },
    {
        "id": "sentry", "name": "Sentry",
        "deps": ["@sentry/", "sentry-sdk", "sentry_flutter", "sentry"],
        "files": [".sentryclirc"], "providers": ["sentry"],
        "actions": ["getsentry/"],
        "env": ["SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT"],
        "hosts": ["sentry.io"],
        "domains": ["ingest.sentry.io", "sentry.io"], "clis": [],
        "transports": ["https"],
    },
    {
        "id": "stripe", "name": "Stripe",
        "deps": ["stripe", "@stripe/", "flutter_stripe"],
        "files": [], "providers": ["stripe"], "actions": ["stripe/"],
        "env": ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET"],
        "hosts": ["api.stripe.com", "js.stripe.com"],
        "domains": ["api.stripe.com", "js.stripe.com"], "clis": [],
        "transports": ["https"],
    },
    {
        "id": "supabase", "name": "Supabase",
        "deps": ["@supabase/", "supabase", "supabase_flutter", "supabase-py"],
        "files": ["supabase/config.toml"], "providers": ["supabase"],  # network-cli: prose
        "actions": ["supabase/"],
        "env": ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
                "NEXT_PUBLIC_SUPABASE_URL"],
        "hosts": ["supabase.co", "supabase.com"],
        "domains": ["api.supabase.com", "supabase.co"], "clis": ["supabase"],  # network-cli: prose
        "transports": ["https", "postgres-tcp"],
    },
    {
        "id": "upstash", "name": "Upstash",
        "deps": ["@upstash/"],
        "files": [], "providers": ["upstash"], "actions": ["upstash/"],
        "env": ["UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
                "UPSTASH_KAFKA_REST_URL"],
        "hosts": ["upstash.io"],
        "domains": ["api.upstash.com", "upstash.io"], "clis": [],
        "transports": ["https", "redis-tcp"],
    },
    {
        "id": "vercel", "name": "Vercel",
        "deps": ["vercel", "@vercel/"],  # network-cli: prose
        "files": ["vercel.json"], "providers": ["vercel"],  # network-cli: prose
        "actions": ["vercel/", "amondnet/vercel-action"],
        "env": ["VERCEL_TOKEN", "VERCEL_ORG_ID", "VERCEL_PROJECT_ID", "VERCEL_URL"],
        "hosts": ["vercel.app", "api.vercel.com"],
        "domains": ["api.vercel.com", "vercel.app"], "clis": ["vercel"],  # network-cli: prose
        "transports": ["https"],
    },
]

# Raw-socket database clients. Paired with a hosted DB vendor they produce the
# `raw-tcp-db` gap: an HTTP CONNECT proxy brokers TLS-over-HTTP, never a bare
# PostgreSQL/MySQL/Redis/Mongo wire protocol on its own port.
DB_CLIENTS = ("pg", "postgres", "postgresql", "node-postgres", "mysql2", "mysql",
              "mongodb", "redis", "ioredis", "psycopg", "psycopg2", "psycopg2-binary",
              "asyncpg", "pymongo", "aioredis", "psql")

# Vendors that host a database behind a raw port. `raw-tcp-db` needs one of
# these AND a client from DB_CLIENTS.
HOSTED_DB_VENDORS = ("mongodb-atlas", "neon", "planetscale", "supabase", "upstash")

# The Node major from which `NODE_OPTIONS=--use-env-proxy` exists at all. Below
# it there is no way to make Node's global fetch read HTTPS_PROXY, so a harness
# script must use curl or python instead.
NODE_PROXY_MAJOR = 24

# Only these three env-file names are ever opened. A real `.env` is never read:
# it is not in this tuple, the deny rules in templates/target-settings.json
# forbid it, and the scan below skips every path whose basename starts ".env"
# unless it is one of these.
ENV_EXAMPLE_FILES = (".env.example", ".env.sample", ".env.template")

# Caps on the tracked-source hostname scan: this is advisory detection, not an
# audit, and it must stay fast on a monorepo.
MAX_SOURCE_FILES = 1500
MAX_SOURCE_BYTES = 262144
MAX_PACKAGE_JSON = 200

# Files whose contents are noise for hostname matching: lockfiles pin registry
# URLs for every transitive dependency and would name half the table.
SKIP_BASENAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "poetry.lock", "Pipfile.lock", "uv.lock", "pubspec.lock", "Cargo.lock",
    "go.sum", "composer.lock", "Gemfile.lock",
}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".tgz", ".bz2", ".xz", ".jar", ".war", ".class", ".so", ".dylib",
    ".dll", ".exe", ".bin", ".wasm", ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".psd", ".sketch", ".keystore", ".jks",
    ".p12", ".pfx", ".pem", ".key", ".db", ".sqlite", ".pyc",
}

# Paths the HOST scan never reads. Dependency, provider, env-key, workflow and
# file signals are unaffected — this is only about matching a hostname inside a
# file's text.
#
# Why: a hostname in the harness's own configuration, in an agent's instruction
# file, or in a spell-check dictionary is not evidence that the product talks to
# that vendor. Two failure modes were observed on real repositories:
#
#   * self-evidence — `.claude/settings.json` already carries the allowlist this
#     harness writes, so `api.github.com` and `registry.npmjs.org` inside it
#     "detected" GitHub and npm. On a re-run that is circular: the fragment
#     would keep re-justifying whatever a previous run put there.
#   * documentation — automata's `datadog` and `gcp` rows came only from
#     `.claude/skills/*/SKILL.md` prose, and acme's GitHub host came from
#     `.github/cspell.json`, a word list. Each seeded a domain into the
#     allowlist for a vendor the product never calls.
#
# Extend by PR, and only with paths that are documentation, agent
# configuration, or a word list — never a path where product code lives.
HOST_SCAN_EXCLUDED_PREFIXES = (
    ".claude/",
    ".codex/",
    ".cursor/",
    ".windsurf/",
)
HOST_SCAN_EXCLUDED_PATHS = (
    ".windsurfrules",
    ".github/copilot-instructions.md",
    ".github/dependabot.yaml",
    ".github/dependabot.yml",
)
HOST_SCAN_EXCLUDED_BASENAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".windsurfrules",
)
HOST_SCAN_EXCLUDED_GLOBS = (
    "cspell*.json",
    ".cspell*",
    "*dictionary*",
    "*wordlist*",
    "*.dic",
)


def _host_scan_excluded(rel: str) -> bool:
    """True when `rel` is agent configuration, documentation or a word list,
    and so must not contribute a hostname match. See HOST_SCAN_EXCLUDED_* above
    for the two failure modes this closes."""
    posix = Path(rel).as_posix()
    if posix in HOST_SCAN_EXCLUDED_PATHS:
        return True
    if posix.startswith(HOST_SCAN_EXCLUDED_PREFIXES):
        return True
    base = os.path.basename(posix)
    if base in HOST_SCAN_EXCLUDED_BASENAMES:
        return True
    lowered = base.lower()
    return any(fnmatch.fnmatch(lowered, pat.lower()) for pat in HOST_SCAN_EXCLUDED_GLOBS)


_PROVIDER_RE = re.compile(r'provider\s+"([A-Za-z0-9_-]+)"')
_PROVIDER_SOURCE_RE = re.compile(r'source\s*=\s*"[^"]*?/([A-Za-z0-9_-]+)"')
_USES_RE = re.compile(r'^\s*-?\s*uses\s*:\s*["\']?([^"\'\s@]+)', re.MULTILINE)
_NODE_MAJOR_RE = re.compile(r"(\d+)")


def _rel_posix(path: str, root: str) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _add(found: dict, vendor: dict, confidence: str, evidence: str) -> None:
    """Record one evidence string for `vendor`, keeping the strongest
    confidence seen. Mirrors detect_stack.py's `_add`: an entry is created on
    first sight, later signals only append evidence and may promote."""
    entry = found.get(vendor["id"])
    if entry is None:
        found[vendor["id"]] = {
            "id": vendor["id"],
            "name": vendor["name"],
            "confidence": confidence,
            "evidence": [evidence],
            "domains": sorted(vendor["domains"]),
            "clis": list(vendor["clis"]),
            "transports": list(vendor["transports"]),
        }
        return
    if evidence not in entry["evidence"]:
        entry["evidence"].append(evidence)
    if confidence == "high":
        entry["confidence"] = "high"


def _add_source(sources: set, rel: str) -> None:
    sources.add(rel)


def _read_text(path: str, limit: int = MAX_SOURCE_BYTES) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def _dep_matches(dep: str, signal: str) -> bool:
    """A signal ending in `/` or `-` is a namespace prefix; anything else is an
    exact name. Prefix matching is what lets one row cover `@aws-sdk/client-s3`
    and `@aws-sdk/client-sqs` without listing every package AWS ships."""
    dep_l = dep.lower()
    sig = signal.lower()
    if sig.endswith("/") or sig.endswith("-"):
        return dep_l.startswith(sig)
    return dep_l == sig


# --- runtime ---------------------------------------------------------------

def _detect_runtime(root: str, sources: set) -> dict:
    """The Node version this repository pins, and where it says so.

    Order is deliberate: `.nvmrc` is what a developer's shell obeys,
    `engines.node` is what the package declares, `volta.node` is what a Volta
    machine installs. The first that answers wins and is named in
    `node_source`, so a person can see which file to edit.
    """
    version = None
    source = None

    nvmrc = os.path.join(root, ".nvmrc")
    text = _read_text(nvmrc, 256) if os.path.isfile(nvmrc) else None
    if text:
        candidate = text.strip().lstrip("vV").strip()
        if candidate and _NODE_MAJOR_RE.search(candidate):
            version, source = candidate, ".nvmrc"
            _add_source(sources, ".nvmrc")

    pkg_path = os.path.join(root, "package.json")
    pkg = None
    if os.path.isfile(pkg_path):
        raw = _read_text(pkg_path)
        try:
            pkg = json.loads(raw) if raw else None
        except ValueError:
            pkg = None
    if isinstance(pkg, dict):
        if version is None:
            engines = pkg.get("engines")
            if isinstance(engines, dict) and isinstance(engines.get("node"), str):
                version = engines["node"].strip()
                source = "package.json:engines.node"
                _add_source(sources, "package.json")
        if version is None:
            volta = pkg.get("volta")
            if isinstance(volta, dict) and isinstance(volta.get("node"), str):
                version = volta["node"].strip()
                source = "volta"
                _add_source(sources, "package.json")

    return {
        "node_version": version,
        "node_source": source,
        # Measured, not inferred: Node's global fetch (undici) reads no proxy
        # environment variable on its own at any version. From Node 24 a person
        # can opt in with NODE_OPTIONS=--use-env-proxy; nothing this script can
        # read makes the default true, so it never is.
        "fetch_proxy_aware": False,
    }


def _node_major(version: str | None) -> int | None:
    if not version:
        return None
    m = _NODE_MAJOR_RE.search(version)
    return int(m.group(1)) if m else None


# --- detection sources -----------------------------------------------------

def _iter_package_jsons(root: str) -> list[str]:
    """Every package.json in the tree, workspace packages included and
    node_modules excluded (EXCLUDED_DIRS is the shared prune list)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        if "package.json" in filenames:
            out.append(os.path.join(dirpath, "package.json"))
            if len(out) >= MAX_PACKAGE_JSON:
                return out
    return out


def _scan_package_jsons(root: str, found: dict, sources: set, node_hits: set) -> set:
    """Dependencies + devDependencies of every package.json. Returns the set of
    declared package names, which the gap rules re-read for db clients and jest;
    `node_hits` collects the ids of vendors evidenced by a Node dependency, which
    is what makes the `node-fetch-proxy` gap relevant to this repository."""
    declared: set = set()
    for path in _iter_package_jsons(root):
        rel = _rel_posix(path, root)
        raw = _read_text(path)
        if raw is None:
            continue
        try:
            pkg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(pkg, dict):
            continue
        deps: dict = {}
        for section in ("dependencies", "devDependencies"):
            block = pkg.get(section)
            if isinstance(block, dict):
                for name in block:
                    deps[name] = section
        if not deps:
            continue
        declared.update(deps)
        hit = False
        for vendor in VENDORS:
            for name, section in deps.items():
                if any(_dep_matches(name, sig) for sig in vendor["deps"]):
                    _add(found, vendor, "high", f"{rel}: {section}.{name}")
                    node_hits.add(vendor["id"])
                    hit = True
                    break
        if hit:
            _add_source(sources, rel)
    return declared


def _scan_pubspec(root: str, found: dict, sources: set) -> set:
    """pubspec.yaml dependencies. Parsed line-wise rather than with a YAML
    library: stdlib only, and a dependency line is `  name: ^1.2.3`."""
    path = os.path.join(root, "pubspec.yaml")
    if not os.path.isfile(path):
        return set()
    raw = _read_text(path)
    if raw is None:
        return set()
    declared: set = set()
    in_deps = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_deps = stripped.rstrip(":") in ("dependencies", "dev_dependencies")
            continue
        if not in_deps:
            continue
        if ":" not in stripped:
            continue
        name = stripped.split(":", 1)[0].strip()
        if name and not name.startswith("-"):
            declared.add(name)
    hit = False
    for vendor in VENDORS:
        for name in sorted(declared):
            if any(_dep_matches(name, sig) for sig in vendor["deps"]):
                _add(found, vendor, "high", f"pubspec.yaml: dependencies.{name}")
                hit = True
                break
    if hit:
        _add_source(sources, "pubspec.yaml")
    return declared


def _python_manifests(root: str) -> list[str]:
    names = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return names
    for name in entries:
        if not os.path.isfile(os.path.join(root, name)):
            continue
        if name == "pyproject.toml" or (name.startswith("requirements") and name.endswith(".txt")):
            names.append(name)
    return names


def _scan_python(root: str, found: dict, sources: set) -> set:
    """requirements*.txt and pyproject.toml. A requirement line is matched on
    its name before any version specifier; pyproject is matched the same way
    over every quoted requirement string."""
    declared: set = set()
    for name in _python_manifests(root):
        raw = _read_text(os.path.join(root, name))
        if raw is None:
            continue
        tokens = set(re.findall(r"[A-Za-z0-9_.\-]+", raw))
        declared |= tokens
        hit = False
        for vendor in VENDORS:
            for dep in sorted(tokens):
                if any(_dep_matches(dep, sig) for sig in vendor["deps"]):
                    _add(found, vendor, "high", f"{name}: requires {dep}")
                    hit = True
                    break
        if hit:
            _add_source(sources, name)
    return declared


def _iter_terraform(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            if name.endswith((".tf", ".tf.json")):
                out.append(os.path.join(dirpath, name))
                if len(out) >= 200:
                    return out
    return out


def _scan_terraform(root: str, found: dict, sources: set) -> None:
    by_provider = {}
    for vendor in VENDORS:
        for provider in vendor["providers"]:
            by_provider.setdefault(provider.lower(), []).append(vendor)
    for path in _iter_terraform(root):
        rel = _rel_posix(path, root)
        raw = _read_text(path)
        if raw is None:
            continue
        names = set(_PROVIDER_RE.findall(raw)) | set(_PROVIDER_SOURCE_RE.findall(raw))
        hit = False
        for name in sorted(names):
            for vendor in by_provider.get(name.lower(), []):
                _add(found, vendor, "high", f'{rel}: provider "{name}"')
                hit = True
        if hit:
            _add_source(sources, rel)


def _scan_workflows(root: str, found: dict, sources: set) -> None:
    wf_dir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return
    try:
        names = sorted(os.listdir(wf_dir))
    except OSError:
        return
    for name in names:
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(wf_dir, name)
        raw = _read_text(path)
        if raw is None:
            continue
        rel = _rel_posix(path, root)
        uses = _USES_RE.findall(raw)
        hit = False
        for action in uses:
            for vendor in VENDORS:
                for prefix in vendor["actions"]:
                    if action.lower().startswith(prefix.lower()):
                        _add(found, vendor, "high", f"{rel}: uses {action}")
                        hit = True
                        break
        if hit:
            _add_source(sources, rel)


def _scan_files(root: str, found: dict, sources: set) -> None:
    for vendor in VENDORS:
        for rel in vendor["files"]:
            path = os.path.join(root, rel.replace("/", os.sep))
            if os.path.exists(path):
                kind = "directory" if os.path.isdir(path) else "file"
                _add(found, vendor, "high", f"{rel}: {kind} present")
                _add_source(sources, rel)


def _scan_env_examples(root: str, found: dict, sources: set) -> None:
    """Env-var NAMES from the three example files, never values.

    The line is split at the first `=` and only the left side is kept, so a
    committed placeholder secret in an example file cannot reach the output.
    A real `.env` is not in ENV_EXAMPLE_FILES and is therefore never opened.
    """
    by_env = {}
    for vendor in VENDORS:
        for key in vendor["env"]:
            by_env.setdefault(key.upper(), []).append(vendor)
    for name in ENV_EXAMPLE_FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        raw = _read_text(path)
        if raw is None:
            continue
        hit = False
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            for vendor in by_env.get(key.upper(), []):
                _add(found, vendor, "medium", f"{name}: {key}")
                hit = True
        if hit:
            _add_source(sources, name)


def _tracked_files(root: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],  # network-cli: prose
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.split("\0") if p]


def _scan_hosts(root: str, found: dict, sources: set) -> None:
    """Hostname fragments in tracked source files.

    `git ls-files` is the file list because it is the repository's own answer
    to "what is source here": generated output, vendored trees and anything
    ignored are excluded for free. Lockfiles, binaries and every `.env*` that
    is not one of the three example names are skipped on top of that, and so
    is everything `_host_scan_excluded` names — the harness's own settings, an
    agent's instruction file and a spell-check dictionary are documentation,
    not dependencies.
    """
    by_host = {}
    for vendor in VENDORS:
        for host in vendor["hosts"]:
            by_host.setdefault(host.lower(), []).append(vendor)
    if not by_host:
        return
    # One file per (vendor, host) pair: a hostname that appears in forty files
    # is not forty pieces of evidence, and naming the first is what a person
    # needs to check the claim. Without this the evidence list and `sources`
    # both grow to the size of the repository.
    seen: set = set()
    scanned = 0
    for rel in _tracked_files(root):
        if scanned >= MAX_SOURCE_FILES:
            break
        if _host_scan_excluded(rel):
            continue
        base = os.path.basename(rel)
        if base in SKIP_BASENAMES:
            continue
        if base.startswith(".env") and base not in ENV_EXAMPLE_FILES:
            continue
        if os.path.splitext(base)[1].lower() in SKIP_SUFFIXES:
            continue
        parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS for p in parts):
            continue
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        raw = _read_text(path)
        if raw is None:
            continue
        scanned += 1
        lowered = raw.lower()
        hit = False
        for host, vendors in by_host.items():
            if host not in lowered:
                continue
            for vendor in vendors:
                if (vendor["id"], host) in seen:
                    continue
                seen.add((vendor["id"], host))
                _add(found, vendor, "medium", f"{rel}: host {host}")
                hit = True
        if hit:
            _add_source(sources, rel)


# --- fragment and gaps -----------------------------------------------------

def _template_excluded_commands(explicit: str | None) -> tuple[list[str], str | None]:
    """The `sandbox.excludedCommands` list the shipped template already carries.

    The fragment may only echo entries from this list — the whole point of
    reading it is that detection can never invent a new escape from the
    sandbox. When the template cannot be read the answer is an empty list and
    a reason, never a guessed default.
    """
    path, err = _resolve_asset(explicit, "target-settings.json",
                               os.environ.get("CLAUDE_PLUGIN_ROOT"))
    if path is None:
        return [], err
    raw = _read_text(path)
    if raw is None:
        return [], f"target-settings.json: {path}: cannot read"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], f"target-settings.json: invalid JSON: {exc}"
    sandbox = data.get("sandbox") if isinstance(data, dict) else None
    commands = sandbox.get("excludedCommands") if isinstance(sandbox, dict) else None
    if not isinstance(commands, list):
        return [], "target-settings.json: no sandbox.excludedCommands list"
    return [c for c in commands if isinstance(c, str)], None


def _build_fragment(vendors: list[dict], template_commands: list[str]) -> dict:
    domains = set(BASELINE_DOMAINS)
    for vendor in vendors:
        domains.update(vendor["domains"])
    allowed = sorted(domains)

    # A CLI has TWO sanctioned spellings and a target repo wants both: `gcloud *`
    # so a top-level command runs (ADR-0004 D-2/D-3a), and `somnio-broker-gcloud *`
    # so a sandboxed script can probe (D-3). A vendor declares the bare CLI name,
    # so collect every template entry that serves it rather than the first.
    template_binaries: dict[str, list[str]] = {}
    for entry in template_commands:
        binary = entry.split()[0] if entry.split() else ""
        if not binary:
            continue
        template_binaries.setdefault(binary, []).append(entry)
        if binary.startswith("somnio-broker-"):
            template_binaries.setdefault(binary[len("somnio-broker-"):], []).append(entry)

    excluded = set()
    for vendor in vendors:
        for cli in vendor["clis"]:
            excluded.update(template_binaries.get(cli, []))

    return {
        "permissions": {
            "allow": [f"WebFetch(domain:{d})" for d in allowed],
        },
        "sandbox": {
            "network": {"allowedDomains": allowed},
            "excludedCommands": sorted(excluded),
        },
    }


def _jest_cache_dir_declared(root: str) -> bool:
    """True when jest is told to cache inside the tree.

    Checked in package.json's `jest` key and in the jest config files; the
    string `cacheDirectory` is enough, because a person who wrote it chose a
    directory deliberately and this script does not grade the choice.
    """
    for path in _iter_package_jsons(root):
        raw = _read_text(path)
        if raw is None:
            continue
        try:
            pkg = json.loads(raw)
        except ValueError:
            continue
        block = pkg.get("jest") if isinstance(pkg, dict) else None
        if isinstance(block, dict) and "cacheDirectory" in block:
            return True
    for name in ("jest.config.js", "jest.config.ts", "jest.config.mjs",
                 "jest.config.cjs", "jest.config.json"):
        raw = _read_text(os.path.join(root, name)) if os.path.isfile(os.path.join(root, name)) else None
        if raw and "cacheDirectory" in raw:
            return True
    return False


def _jest_present(root: str, declared: set) -> bool:
    if any(d == "jest" or d.startswith("jest-") or d == "ts-jest" or d == "@types/jest"
           for d in declared):
        return True
    for name in ("jest.config.js", "jest.config.ts", "jest.config.mjs",
                 "jest.config.cjs", "jest.config.json"):
        if os.path.isfile(os.path.join(root, name)):
            return True
    return False


def _build_gaps(root: str, vendors: list[dict], runtime: dict, declared: set,
                node_hits: set) -> list[dict]:
    """The three gaps this script knows how to see.

    A gap is never folded into `settings_fragment`: the fragment is what a
    person can apply as written, a gap is a decision a person makes. Mixing
    them would let detection widen a sandbox on its own, which is exactly what
    ADR-0004 D-2 and D-8 forbid.
    """
    gaps: list[dict] = []
    ids = {v["id"] for v in vendors}

    # `node_hits` is the gate, not the mere presence of a package.json: the gap
    # is about a vendor call made from Node, so at least one DETECTED vendor
    # must have been evidenced by a package.json dependency. A Flutter repo
    # whose only package.json holds build tooling with no vendor dependency
    # reaches no vendor from Node and gets no gap. An unknown Node version does
    # not close it — unknown is not proof of >= 24.
    node_major = _node_major(runtime["node_version"])
    if node_hits and (node_major is None or node_major < NODE_PROXY_MAJOR):
        seen = runtime["node_version"] or "undetected"
        declares = (
            f"this repository declares {seen}" if runtime["node_version"]
            else "this repository declares no Node version"
        )
        gaps.append({
            "id": "node-fetch-proxy",
            "severity": "medium",
            "summary": (
                f"Node's global fetch ignores the sandbox proxy (Node {seen})."
            ),
            "detail": (
                "Measured inside a sandboxed target: DNS is blocked at the kernel and "
                "every allowed request goes through an HTTP CONNECT proxy injected as "
                "HTTPS_PROXY/https_proxy. curl and python urllib honour it; Node's "
                "global fetch (undici) reads no proxy variable and fails with "
                "ENOTFOUND, which reads like a network outage rather than a policy."
            ),
            "fix": (
                "NODE_OPTIONS=--use-env-proxy makes Node's fetch read the proxy, and it "
                f"exists only from Node {NODE_PROXY_MAJOR}; {declares}. "
                f"Until the runtime is on {NODE_PROXY_MAJOR}+, a harness script "
                "that needs the network uses curl or python, never Node fetch."
            ),
        })

    db_clients = sorted(d for d in declared if d.lower() in DB_CLIENTS)
    hosted = sorted(ids & set(HOSTED_DB_VENDORS))
    if db_clients and hosted:
        gaps.append({
            "id": "raw-tcp-db",
            "severity": "high",
            "summary": (
                f"{', '.join(db_clients)} reaches {', '.join(hosted)} over a raw "
                "socket; an HTTP proxy cannot broker it."
            ),
            "detail": (
                "The sandbox brokers HTTP CONNECT only. A database client speaking the "
                "PostgreSQL/MySQL/Redis/Mongo wire protocol on its own port has nothing "
                "to connect through, and DNS is blocked at the kernel besides, so the "
                "connection cannot be made to work by adding a domain to allowedDomains."
            ),
            "fix": (
                "Run that lane outside the sandbox as a named per-command exclusion the "
                "person accepts (for example the exact test or migration command), never "
                "an interpreter wildcard such as \"node *\" or \"uv *\" — ADR-0004 D-2. "
                "The alternative is the vendor's HTTP driver, which the proxy brokers."
            ),
        })

    if _jest_present(root, declared) and not _jest_cache_dir_declared(root):
        gaps.append({
            "id": "jest-cache-write",
            "severity": "low",
            "summary": "jest has no in-tree cacheDirectory; its cache write is denied.",
            "detail": (
                "Observed on marketplace-monorepo: `pnpm run test` failed under the "
                "sandbox on jest's haste-map under /private/tmp/jest_dx with EPERM. "
                "That is a filesystem denial, not an egress one."
            ),
            "fix": (
                "Either add the cache path to sandbox.filesystem.allowWrite in the "
                "target's own settings.json (a target-owned allowance, ADR-0004 D-8), or "
                "set jest's cacheDirectory to a path under <rootDir> so the write lands "
                "inside the tree the sandbox already allows."
            ),
        })

    return gaps


# --- entry point -----------------------------------------------------------

def _list_vendors() -> int:
    payload = {
        "vendors": [
            {
                "id": v["id"],
                "name": v["name"],
                "domains": sorted(v["domains"]),
                "clis": list(v["clis"]),
                "transports": list(v["transports"]),
            }
            for v in VENDORS
        ]
    }
    print(json.dumps(payload))
    print(f"{len(VENDORS)} vendor(s) in the table", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit the network vendors a repository's own dependencies name, "
                    "the settings fragment they imply, and the gaps a person must accept.",
    )
    parser.add_argument("--root", help="path to the target repository root")
    parser.add_argument(
        "--list-vendors",
        action="store_true",
        help="print the vendor table (id, name, domains, CLIs) instead of scanning; "
             "no root is needed",
    )
    parser.add_argument(
        "--settings-template",
        help="path to target-settings.json; resolved from the repository or the "
             "plugin root when omitted",
    )
    args = parser.parse_args()

    if args.list_vendors:
        return _list_vendors()

    if args.root is None:
        print("error: --root is required unless --list-vendors is given", file=sys.stderr)
        return 2
    if not os.path.isdir(args.root):
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2
    if not os.access(args.root, os.R_OK | os.X_OK):
        print(f"error: {args.root} is not readable", file=sys.stderr)
        return 2

    root = os.path.realpath(args.root)

    found: dict = {}
    sources: set = set()

    runtime = _detect_runtime(root, sources)
    declared: set = set()
    node_hits: set = set()
    declared |= _scan_package_jsons(root, found, sources, node_hits)
    declared |= _scan_pubspec(root, found, sources)
    declared |= _scan_python(root, found, sources)
    _scan_terraform(root, found, sources)
    _scan_workflows(root, found, sources)
    _scan_files(root, found, sources)
    _scan_env_examples(root, found, sources)
    _scan_hosts(root, found, sources)

    vendors = sorted(found.values(), key=lambda v: v["id"])
    for vendor in vendors:
        vendor["evidence"] = sorted(set(vendor["evidence"]))

    template_commands, template_error = _template_excluded_commands(args.settings_template)
    fragment = _build_fragment(vendors, template_commands)
    gaps = _build_gaps(root, vendors, runtime, declared, node_hits)

    payload = {
        "root": root,
        "runtime": runtime,
        "vendors": vendors,
        "settings_fragment": fragment,
        "gaps": gaps,
        "sources": sorted(sources),
    }
    print(json.dumps(payload))

    ids = ", ".join(v["id"] for v in vendors) or "none"
    src = ", ".join(sorted(sources)[:4]) or "none"
    extra = "" if template_error is None else f" — {template_error}"
    print(
        f"network: {len(vendors)} vendor(s) ({ids}), "
        f"{len(fragment['sandbox']['network']['allowedDomains'])} domain(s), "
        f"{len(fragment['sandbox']['excludedCommands'])} excludedCommand(s), "
        f"{len(gaps)} gap(s) — sources {src}{extra}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
