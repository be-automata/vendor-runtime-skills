#!/usr/bin/env bash
# Idempotent installer for the Somnio developer harness.
#
# Installs, at USER level (never inside a repository):
#   1. ruflo CLI  — npm global install (claude-flow v3; serves the ruflo MCP)
#   1b. agent-device CLI — npm global install (simulator/device automation)
#   1c. uv        — checksum-verified run of the pinned upstream installer; the
#                   /uat skill runs every one of its scripts as 'uv run'
#   2. gstack     — clone https://github.com/garrytan/gstack.git and run ./setup,
#                   which populates ~/.claude/skills with the gstack skill suite
#   3. ruflo global pointer block in ~/.claude/CLAUDE.md via 'ruflo init' run in
#      a scratch directory, so no repository is polluted with runtime files
#
# NOT installed, by decision: python3. uv provisions its own interpreters and
# the /uat scripts pin `requires-python`, so uv covers them; a system Python is
# heavyweight, OS-specific, and needs sudo on Linux. doctor.sh checks for it and
# names the per-OS fix instead — the harness never installs a system interpreter.
#
# The optional settings step (offered by the /somnio-engineering-ai:install
# command, executed by apply-settings.sh) is not run from here; note only that
# it derives the repo's network policy from the repo's own dependencies rather
# than from a hand-written list.
#
# Safe to re-run: every step checks before acting. Nothing here touches the
# repository this plugin ships from; project-local ruflo runtime state is
# covered by the repository .gitignore instead.

set -euo pipefail

log() { printf "[harness] %s\n" "$*"; }

# Supply-chain pins (area decision 2026-08-28): exact versions only, bumped by
# PR. The E2E suite verifies these very versions; keep .mcp.json's
# @claude-flow/cli pin equal to RUFLO_CLI_VERSION when bumping.
RUFLO_VERSION="3.42.5"
# The ENGINE the wrapper above delegates to, and what .mcp.json runs directly.
# The wrapper depends on @claude-flow/cli with a caret range, so pinning it alone
# pinned nothing (scripts/ruflo-engine.sh). Keep .mcp.json's
# @claude-flow/cli@<ver> equal to this; tests/broker/template-exclusions-test.sh
# asserts it.
RUFLO_CLI_VERSION="3.42.5"
AGENT_DEVICE_VERSION="0.21.12"
GSTACK_REPO="https://github.com/garrytan/gstack.git"
GSTACK_COMMIT="636175d3496e1f5087d65522e69d314584c7b236"
# somnio-ai-tools rules corpus consumed read-only by harness-init (ADR-0003); bumped by PR.
SOMNIO_AI_TOOLS_COMMIT="aa91cefe00b10b0de864164350ead79f0b9a1a66"
# The somnio CLI binary harness-init drives for the skills/commands step. Defined
# and copied on ONE line on purpose: the pin must appear exactly once in this
# file (tests/rules-source/pin-test.sh), and doctor.sh greps this line for it.
# The version is what `somnio --version` prints; the commit below is what the
# activate line pins, because upstream publishes no tag for either it or the
# content pin. The two are deliberately SEPARATE pins, each bumped by PR, and
# may sit at the same rev: SOMNIO_AI_TOOLS_COMMIT above pins the rules corpus,
# this one the binary — both CLI 3.0.0 today. doctor.sh greps both out of this
# file.
SOMNIO_CLI_VERSION="3.0.0"; _CLI_PIN="$SOMNIO_CLI_VERSION"
SOMNIO_CLI_COMMIT="aa91cefe00b10b0de864164350ead79f0b9a1a66"
# roborev: the local continuous review battery (ADR-0005, Layers 1+2 only).
# Upstream publishes NO independently verifiable checksum for its release
# assets: each ships a sibling .provenance.json, but that file is self-published
# in the same release, so using its digest to verify that release is circular —
# and `gh attestation verify` returns HTTP 404 because nothing is registered
# with GitHub (probed 2026-09-08, ADR-0005 Q-4). The sha256 per asset is
# therefore recorded HERE and reviewed as part of the bump PR. Defined once in
# this file: install-roborev.sh receives them, doctor.sh greps them back, and
# scripts/roborev-bump.sh rewrites them here (tests/roborev/install-test.sh).
ROBOREV_VERSION="0.68.1"
ROBOREV_SHA256_darwin_arm64="2ee405245311f8b5f80e756f4a3fb278b1d320f6f1e1ff29510461e0d7d9bcdc"
ROBOREV_SHA256_darwin_amd64="6ca9e99d683842008d0609425bc4d513e73336f0cd01ee5130f4a7eac196067d"
ROBOREV_SHA256_linux_amd64="4c560a49c3738a59ce4d3ea1f663ca5d6235e8adec43d35f62c1eed60cde331d"
ROBOREV_SHA256_linux_arm64="751a294d790f581a40fd1b89de0b7147646e2c5b45679c04d225337e06149fdb"
# uv is fetched through its own upstream installer, pinned by version AND by the
# sha256 of that installer script — the script hardcodes APP_VERSION plus a
# sha256 per target artifact, so verifying the script transitively pins the
# binary. Same shape as the gstack browse skill's BUN_VERSION/BUN_INSTALL_SHA.
#
# Do NOT "simplify" this to `npm install -g uv`: Astral publishes no uv package
# on npm (@astral-sh/uv is a 404), and the npm package literally named `uv` is
# unrelated software. That shortcut installs the wrong tool, silently.
UV_VERSION="0.12.18"
UV_INSTALLER_SHA="e62a5ea089de9c6a9b1f767166c9f33194e18d350e9f375c6355fac3ba0fb42b"
# The immutable release asset, not the astral.sh redirect: same bytes today, one
# less hop that could be re-pointed later.
UV_INSTALLER_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-installer.sh"

# One cleanup list for every temp dir this script makes. `trap ... EXIT` REPLACES
# any previous EXIT trap, so a second per-block trap would silently orphan the
# first block's directory. Push here; the single trap below drains it.
HARNESS_TMPDIRS=()
cleanup_tmpdirs() {
  local d
  for d in ${HARNESS_TMPDIRS+"${HARNESS_TMPDIRS[@]}"}; do
    [ -n "$d" ] && rm -rf "$d"
  done
}
trap cleanup_tmpdirs EXIT

# --- 1. ruflo CLI --------------------------------------------------------------
# Enforce the pin even over an existing install: an unpinned ruflo defeats the
# supply-chain policy and can drift from the pinned MCP server. TWO versions are
# graded: the wrapper (`ruflo --version`) and the engine it runs. The wrapper
# depends on @claude-flow/cli with a caret range, so `npm install -g ruflo@<v>`
# ran whatever engine npm resolved that day (3.44.0 on 2026-09-24, pin 3.42.5)
# and ran that engine's postinstall too. A global install honours no
# `overrides`, and npm refuses to install into a global package's own
# node_modules, so the pin is a PROJECT install the harness owns:
#   ~/.somnio/ruflo-<wrapper>-cli-<engine>/  package.json with an override
# which is then linked globally (`npm install -g <dir>` symlinks, installs
# nothing). The wrapper's own lookup then finds the pinned engine there.
# Only the engine is pinned this way; its own dependencies still resolve their
# caret ranges (no lockfile ships yet — owed).
. "$(dirname "${BASH_SOURCE[0]}")/ruflo-engine.sh"
RUFLO_HOME="$HOME/.somnio/ruflo-$RUFLO_VERSION-cli-$RUFLO_CLI_VERSION"
if command -v ruflo >/dev/null 2>&1 \
   && ruflo --version 2>/dev/null | head -1 | grep -q "v$RUFLO_VERSION\$" \
   && [ "$(ruflo_engine_version || true)" = "$RUFLO_CLI_VERSION" ]; then
  log "ruflo already at the pin: $(ruflo --version 2>/dev/null | head -1), engine @claude-flow/cli $RUFLO_CLI_VERSION"
else
  if ! command -v npm >/dev/null 2>&1; then
    log "ERROR: npm not found. Install Node.js (e.g. via nvm) and re-run."
    exit 1
  fi
  if command -v ruflo >/dev/null 2>&1; then
    log "ruflo present but not at the pin ($(ruflo --version 2>/dev/null | head -1), engine '$(ruflo_engine_version || echo unknown)'; pin v$RUFLO_VERSION, engine $RUFLO_CLI_VERSION) — reinstalling..."
  fi
  _rf_engine_at() { sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$1/node_modules/@claude-flow/cli/package.json" 2>/dev/null | head -1; }
  if [ "$(_rf_engine_at "$RUFLO_HOME")" != "$RUFLO_CLI_VERSION" ]; then
    # Staged beside the target and renamed in: an interrupted install never
    # leaves a half-built tree under the final name.
    _RF_STAGE="$RUFLO_HOME.stage.$$"
    HARNESS_TMPDIRS+=("$_RF_STAGE")
    mkdir -p "$_RF_STAGE"
    printf '{\n  "private": true,\n  "description": "somnio-engineering-ai: pinned ruflo (install.sh step 1)",\n  "dependencies": { "ruflo": "%s" },\n  "overrides": { "@claude-flow/cli": "%s" }\n}\n' \
      "$RUFLO_VERSION" "$RUFLO_CLI_VERSION" > "$_RF_STAGE/package.json"
    log "installing ruflo@$RUFLO_VERSION with engine @claude-flow/cli@$RUFLO_CLI_VERSION into $RUFLO_HOME..."
    (cd "$_RF_STAGE" && npm install --no-audit --no-fund) \
      || { log "ERROR: npm could not install the pinned ruflo into $_RF_STAGE."; exit 1; }
    [ "$(_rf_engine_at "$_RF_STAGE")" = "$RUFLO_CLI_VERSION" ] \
      || { log "ERROR: the override did not pin @claude-flow/cli (got '$(_rf_engine_at "$_RF_STAGE")', pin $RUFLO_CLI_VERSION)."; exit 1; }
    rm -rf "$RUFLO_HOME"
    mv "$_RF_STAGE" "$RUFLO_HOME"
  fi
  npm install -g "$RUFLO_HOME/node_modules/ruflo" \
    || { log "ERROR: could not link $RUFLO_HOME/node_modules/ruflo globally (a root-owned npm prefix needs a user-writable one: npm config set prefix ~/.npm-global)."; exit 1; }
  hash -r 2>/dev/null || true
  # A different ruflo earlier on PATH (a pnpm/asdf/volta shim) is reported and
  # the install continues: the pinned one is linked, and aborting here would
  # skip every later battery for a PATH-order problem.
  if [ "$(ruflo_engine_version || true)" = "$RUFLO_CLI_VERSION" ]; then
    log "ruflo installed: $(ruflo --version 2>/dev/null | head -1), engine @claude-flow/cli $RUFLO_CLI_VERSION"
  else
    log "WARN: the pinned ruflo is linked, but the ruflo first on PATH ($(command -v ruflo)) is another one. Put npm's global bin first on PATH, or remove that one."
  fi
  # Older pinned trees are NOT pruned: another Node version or npm prefix may
  # still link one (a global link is per prefix). ~/.somnio/ruflo-* may be
  # removed by hand once no prefix uses it.
fi

# --- 1b. agent-device CLI (iOS/Android simulator automation) -------------------
if command -v agent-device >/dev/null 2>&1 && [ "$(agent-device --version 2>/dev/null | head -1)" = "$AGENT_DEVICE_VERSION" ]; then
  log "agent-device already installed at the pinned version: $AGENT_DEVICE_VERSION"
elif command -v agent-device >/dev/null 2>&1; then
  log "agent-device present but not at the pinned version ($(agent-device --version 2>/dev/null | head -1) != $AGENT_DEVICE_VERSION) — reinstalling the pin..."
  npm install -g "agent-device@$AGENT_DEVICE_VERSION"
else
  log "installing agent-device@$AGENT_DEVICE_VERSION globally via npm..."
  npm install -g "agent-device@$AGENT_DEVICE_VERSION"
  log "agent-device installed: $(agent-device --version 2>/dev/null | head -1)"
fi

# --- 1c. uv (Python script runner; /uat runs every script as 'uv run') ---------
# Deliberate asymmetry with ruflo/agent-device above: an existing uv is ACCEPTED,
# never reinstalled to the pin. Those two are harness-only and their drift breaks
# the pinned MCP; uv is a general developer tool someone may own via brew or mise
# for unrelated work, and clobbering it is not the harness's call. Rule 15 governs
# what we install, not what we find. doctor.sh reports a mismatch as info, not an
# error, for the same reason.
if command -v uv >/dev/null 2>&1; then
  UV_FOUND="$(uv --version 2>/dev/null | head -1 || echo unknown)"
  log "uv already installed: $UV_FOUND (at $(command -v uv))"
  case "$UV_FOUND" in
    *"$UV_VERSION"*) : ;;
    *) log "note: harness pin is uv $UV_VERSION — your uv is left as-is on purpose (see the comment above)" ;;
  esac
else
  if command -v curl >/dev/null 2>&1; then
    UV_FETCH=(curl -fsSL -o)
  elif command -v wget >/dev/null 2>&1; then
    UV_FETCH=(wget -qO)
  else
    log "ERROR: neither curl nor wget found; cannot fetch the pinned uv installer."
    log "       Install one, or install uv yourself (https://docs.astral.sh/uv/), then re-run."
    exit 1
  fi
  if command -v shasum >/dev/null 2>&1; then
    UV_SUM=(shasum -a 256)
  elif command -v sha256sum >/dev/null 2>&1; then
    UV_SUM=(sha256sum)
  else
    log "ERROR: neither shasum nor sha256sum found; refusing to run an unverified installer."
    exit 1
  fi

  UV_TMP="$(mktemp -d)"
  HARNESS_TMPDIRS+=("$UV_TMP")
  log "fetching the pinned uv installer (uv $UV_VERSION)..."
  "${UV_FETCH[@]}" "$UV_TMP/uv-installer.sh" "$UV_INSTALLER_URL"
  UV_ACTUAL="$("${UV_SUM[@]}" "$UV_TMP/uv-installer.sh" | awk '{print $1}')"
  if [ "$UV_ACTUAL" != "$UV_INSTALLER_SHA" ]; then
    log "ERROR: uv installer checksum mismatch — refusing to run it."
    log "       expected: $UV_INSTALLER_SHA"
    log "       actual:   $UV_ACTUAL"
    log "       This is a supply-chain signal, not a transient error. Do not bypass it."
    exit 1
  fi
  # The upstream installer verifies the BINARY it downloads against a sha256 it embeds
  # per target — but only if `sha256sum` is on PATH; otherwise it prints "skipping
  # sha256 checksum verification" and returns success. Stock macOS ships `shasum`, not
  # `sha256sum`, so on the primary developer platform that verification silently
  # no-ops and our "verifying the script transitively pins the binary" claim would be
  # true only by luck. Give it a sha256sum it can use, so the claim is enforced.
  if ! command -v sha256sum >/dev/null 2>&1; then
    printf '#!/bin/sh\nexec shasum -a 256 "$@"\n' > "$UV_TMP/sha256sum"
    chmod +x "$UV_TMP/sha256sum"
    log "providing a sha256sum shim so the upstream installer verifies the binary too"
  fi
  log "checksum verified; installing uv $UV_VERSION (writes ~/.local/bin and appends a PATH line to your shell rc)..."
  UV_INSTALL_OUT="$(PATH="$UV_TMP:$PATH" bash "$UV_TMP/uv-installer.sh" 2>&1)"; UV_INSTALL_RC=$?
  printf '%s\n' "$UV_INSTALL_OUT"
  # A skipped binary verification is a silent downgrade of the whole supply-chain
  # story, so surface it rather than letting it scroll past.
  case "$UV_INSTALL_OUT" in
    *"skipping sha256 checksum verification"*)
      log "ERROR: the uv installer could not verify the binary's checksum (no usable sha256sum)."
      log "       Refusing to keep an unverified binary. Install coreutils (or ensure shasum exists) and re-run."
      rm -f "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx" 2>/dev/null || true
      exit 1
      ;;
  esac
  [ "$UV_INSTALL_RC" -eq 0 ] || { log "ERROR: the uv installer exited $UV_INSTALL_RC."; exit 1; }
  if command -v uv >/dev/null 2>&1; then
    log "uv installed: $(uv --version 2>/dev/null | head -1)"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    log "uv installed at ~/.local/bin/uv ($("$HOME/.local/bin/uv" --version 2>/dev/null | head -1))."
    log "       It is not on this shell's PATH yet — the installer's rc edit applies to new shells."
  else
    log "ERROR: the uv installer finished but no uv binary was found. Re-run, or install uv manually."
    exit 1
  fi
fi

# --- 1d. somnio-ai-tools rules source (read-only, pinned; ADR-0003) ------------
# harness-init copies stack rules out of agent-rules/rules/ in a clone pinned to
# SOMNIO_AI_TOOLS_COMMIT and detached at that exact SHA. This block only reads
# that corpus: it never runs the CLI's own rules installer or its generate
# script (ADR-0003 D-1/D-2). A failure here is reported and never changes this
# installer's exit status — the source is a hard gate for harness-init alone
# (ADR-0003 D-4). Live checks (re-run idempotency, credential failure) are
# exercised by a person; tests/rules-source/pin-test.sh covers the static shape.
_RS_DIR="$HOME/.somnio/somnio-ai-tools-$SOMNIO_AI_TOOLS_COMMIT"
_RS_SHORT="$(printf '%s' "$SOMNIO_AI_TOOLS_COMMIT" | cut -c1-7)"
if [ -d "$_RS_DIR/.git" ] && [ "$(git -C "$_RS_DIR" rev-parse HEAD 2>/dev/null)" = "$SOMNIO_AI_TOOLS_COMMIT" ]; then
  log "rules source: already at $_RS_SHORT ($_RS_DIR)."
else
  mkdir -p "$HOME/.somnio"; rm -rf "$_RS_DIR.tmp"
  _RS_OK=0
  # SSH first; then HTTPS through gh's credential helper (no token ever lands in
  # the clone's remote URL). GIT_TERMINAL_PROMPT=0 makes a missing credential a
  # clean failure instead of an interactive hang; on the SSH leg ssh itself can
  # still hang — on a host-key prompt at first contact, or a passphrase prompt
  # for an unloaded key — so BatchMode=yes turns both into a clean failure and
  # accept-new trusts github.com's key on first contact without asking.
  if GIT_TERMINAL_PROMPT=0 \
     GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new' \
     git clone -q git@github.com:somnio-software/somnio-ai-tools.git "$_RS_DIR.tmp" 2>/dev/null; then
    _RS_OK=1
  elif command -v gh >/dev/null 2>&1 && GIT_TERMINAL_PROMPT=0 git -c credential.helper='!gh auth git-credential' clone -q https://github.com/somnio-software/somnio-ai-tools.git "$_RS_DIR.tmp" 2>/dev/null; then
    _RS_OK=1
  fi
  if [ "$_RS_OK" = 1 ] && git -C "$_RS_DIR.tmp" checkout -q --detach "$SOMNIO_AI_TOOLS_COMMIT" 2>/dev/null && [ -d "$_RS_DIR.tmp/agent-rules/rules" ]; then
    rm -rf "$_RS_DIR"; mv "$_RS_DIR.tmp" "$_RS_DIR"
    log "rules source: cloned somnio-ai-tools at $_RS_SHORT -> $_RS_DIR."
  else
    rm -rf "$_RS_DIR.tmp"
    log "rules source: cannot clone somnio-software/somnio-ai-tools at $_RS_SHORT - configure SSH access or run 'gh auth login'; harness-init will refuse to run until it exists (ADR-0003 D-4). Continuing."
  fi
fi

# --- 1e. somnio CLI binary (checked, never installed; ADR-0003) ----------------
# harness-init shells out to `somnio skills install --agent <id> --project` to put
# the CLI catalog's skills and commands into a target repo. That binary is a
# separate product on its own road (business rule 17): this installer reports it
# and never installs, activates or upgrades it, and never changes its own exit
# status for it. `somnio rules install` stays forbidden (ADR-0003 D-1/D-2) — this
# block does not run the CLI at all, it only reads `somnio --version`.
# The install line is the one the CLI's own README at the pin prints.
_CLI_FIX="dart pub global activate -sgit https://github.com/somnio-software/somnio-ai-tools.git --git-path cli --git-ref $SOMNIO_CLI_COMMIT"
if command -v somnio >/dev/null 2>&1; then
  _CLI_V="$(somnio --version 2>/dev/null | head -1 | sed -E 's/^somnio v?//' | tr -d '[:space:]')"
  if [ -n "$_CLI_V" ] && [ "$_CLI_V" = "$_CLI_PIN" ]; then
    log "somnio CLI: already at $_CLI_PIN."
  else
    log "somnio CLI: on ${_CLI_V:-(version unknown)}, harness pin is $_CLI_PIN - harness-init records the drift and continues. To match the pin: $_CLI_FIX. Continuing."
  fi
else
  log "somnio CLI: not on PATH - harness-init installs rules without it and skips the skills step. Fix: $_CLI_FIX. Continuing."
fi

# --- 1f. roborev (local continuous review battery; ADR-0005 Layers 1+2) --------
# The step lives in its own script because install.sh cannot be driven by a
# fixture: it also npm-installs ruflo, clones gstack and runs 'ruflo init'.
# install-roborev.sh is separately executable, so tests/roborev/ can exercise
# the download, verification, config and consent paths directly.
#
# The pin and the four per-asset sha256 values are defined ONCE, above, and
# exported here. install-roborev.sh reads them and refuses to run without them;
# tests/rules-source/pin-test.sh's rule (one definition, in this file) is why.
#
# SOMNIO_SKIP_ROBOREV is read HERE, at the call site, not inside the step
# script: the e2e container (node:22-bookworm, no init system) must never enter
# the step at all, and this script runs under 'set -euo pipefail'.
if [ -n "${SOMNIO_SKIP_ROBOREV:-}" ]; then
  log "skipping the roborev step (SOMNIO_SKIP_ROBOREV set)"
else
  export ROBOREV_VERSION
  export ROBOREV_SHA256_darwin_arm64 ROBOREV_SHA256_darwin_amd64
  export ROBOREV_SHA256_linux_amd64 ROBOREV_SHA256_linux_arm64
  # Telemetry off by area policy (business rule 14), the same posture forced for
  # gstack. Persisted by the step, not merely exported here: an installer-process
  # export is inert for a daemon the installer does not own.
  export ROBOREV_TELEMETRY_ENABLED=0
  # NON-FATAL FOR AN ENVIRONMENT FAILURE, NEVER SILENT FOR AN INTEGRITY ONE.
  #
  # The step runs under this script's 'set -euo pipefail', so an unguarded call
  # aborts install.sh before gstack — which business rule 13 makes mandatory —
  # and before `ruflo init`. roborev is a review battery under ADR-0005; gstack
  # is a required one, so a machine that cannot install roborev must still get
  # the mandatory batteries. That places this step with 1d/1e (somnio-ai-tools,
  # somnio CLI), not with the fatal ones (ruflo, uv, gstack).
  #
  # But "non-fatal" is not one bucket. The step exits 2 for an ENVIRONMENT
  # failure (unsupported arch, no curl/wget, no hashing tool, no sha pinned for
  # the platform) and 3 for an INTEGRITY failure (checksum mismatch, no
  # executable in a verified tarball, post-install version disagreeing with the
  # pin). Answering a digest mismatch with "re-run it" would be wrong advice —
  # re-running re-downloads the same mismatching artifact — and it would
  # contradict the stance this same installer takes on the identical event for
  # uv above: "a supply-chain signal, not a transient error. Do not bypass it."
  ROBOREV_STEP="$(dirname "${BASH_SOURCE[0]}")/install-roborev.sh"
  if bash "$ROBOREV_STEP"; then
    :
  else
    ROBOREV_RC=$?
    case "$ROBOREV_RC" in
      3)
        log "ERROR: roborev artifact integrity check FAILED (exit 3)."
        log "       This is a supply-chain signal, not a transient error. Do not bypass it."
        log "       Do NOT simply re-run: the same pinned artifact will be fetched again."
        log "       Verify the pinned sha256 values in this file against the release, and"
        log "       treat a persistent mismatch as an incident, not an install problem."
        log "       Continuing so the mandatory batteries still install; roborev is NOT installed." ;;
      *)
        log "roborev: the step could not complete on this machine (exit $ROBOREV_RC) - roborev is not installed."
        # Point at THIS installer, not at the step. The step reads the pin from
        # its environment and refuses to run without it, so `bash install-roborev.sh`
        # on its own dies with "ROBOREV_VERSION is not set" — a second, unrelated
        # looking failure for a developer who followed the advice verbatim.
        log "         Re-run this installer once the cause is fixed: bash ${BASH_SOURCE[0]}"
        log "         The doctor (#44) reports roborev as MISSING until it succeeds. Continuing." ;;
    esac
  fi
fi

# --- 1g. the ADR-0004 D-3 broker ------------------------------------------------
# One implementation, one entry point per CLI. `excludedCommands` matches the
# command TEXT, so `somnio-broker-gcloud *` grants that CLI's two fixed read-only
# probes and nothing else — which is what "one wrapper per CLI is the unit of
# adoption" means. The entry points are symlinks so the logic and its hardening
# live in exactly one file; ten copies is how copy #7 drifts into a wider hole.
#
# NON-FATAL, like 1d/1e: a machine without the broker loses infra probing under
# the sandbox and degrades to the person-confirms path (ADR-0004 D-3a). It must
# never cost the developer gstack (business rule 13).
BROKER_SRC="$(dirname "${BASH_SOURCE[0]}")/broker/somnio-broker"
BROKER_ALLOW="$(dirname "${BASH_SOURCE[0]}")/broker/allowlist.json"
if [ -f "$BROKER_SRC" ] && [ -f "$BROKER_ALLOW" ] && command -v python3 >/dev/null 2>&1; then
  mkdir -p "$HOME/.local/bin"
  # Read-only: the ADR's hardening list says plugin-shipped and read-only. This
  # stops accident and drift, NOT an attacker who already runs as this user and
  # could chmod it back — stated plainly in ADR-0004 D-3 rather than implied away.
  chmod 0555 "$BROKER_SRC" 2>/dev/null || true
  chmod 0444 "$BROKER_ALLOW" 2>/dev/null || true
  _BROKER_N=0
  for _cli in $(python3 -c 'import json,sys; print(" ".join(sorted({s["cli"] for s in json.load(open(sys.argv[1]))["providers"].values()})))' "$BROKER_ALLOW" 2>/dev/null); do
    ln -sf "$(cd "$(dirname "$BROKER_SRC")" && pwd)/somnio-broker" "$HOME/.local/bin/somnio-broker-$_cli" 2>/dev/null \
      && _BROKER_N=$((_BROKER_N+1))
  done
  log "broker: $_BROKER_N entry points in ~/.local/bin (ADR-0004 D-3; each excluded per repo, never the raw CLI)"
else
  log "broker: not installed - the implementation, its allowlist or python3 is missing. Infra probes degrade to the person-confirms path (ADR-0004 D-3a). Continuing."
fi

# --- 2. gstack skill suite -----------------------------------------------------
GSTACK_DIR="$HOME/.claude/skills/gstack"
# VERSION alone can survive a failed ./setup; require the config binary too so
# a partial install is repaired instead of reported healthy forever. Both files
# existing is still not proof of a real install: the E2E suite's quick-mode
# fixture creates exactly these two as stubs, and a stub satisfied this check
# forever — the documented repair path (/somnio-engineering-ai:install) became a
# no-op. Grade the VERSION string too, and prefer the commit pin when the
# install is a git checkout.
# A broken install is MOVED, never deleted: this path can be a person's own
# gstack checkout with uncommitted work. The backup lives outside skills/ so
# Claude Code does not load it as a second copy of every gstack skill.
# The path it went to is kept in GSTACK_ASIDE_TO so a failed setup can put it back.
GSTACK_ASIDE_TO=""
_gstack_aside() {
  [ -e "$GSTACK_DIR" ] || return 0
  local to="$HOME/.gstack/install-backups/gstack.$1.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mkdir -p "$(dirname "$to")"
  mv "$GSTACK_DIR" "$to"
  GSTACK_ASIDE_TO="$to"
  log "moved $GSTACK_DIR aside to $to (nothing deleted)"
}
# Written into the checkout before ./setup and removed after it succeeds. A run
# killed mid-setup leaves it behind, and its VERSION + bin/gstack-config would
# otherwise grade healthy forever. Only this installer writes it, so a checkout
# carrying it is this installer's own and is replaced, not moved aside.
GSTACK_SETUP_MARK=".somnio-setup-incomplete"
GSTACK_OK=0
if [ -f "$GSTACK_DIR/$GSTACK_SETUP_MARK" ]; then
  log "gstack at $GSTACK_DIR is an interrupted install of this installer's own — reinstalling..."
  rm -rf "$GSTACK_DIR"
elif [ -f "$GSTACK_DIR/VERSION" ] && [ -x "$GSTACK_DIR/bin/gstack-config" ]; then
  GSTACK_V="$(cat "$GSTACK_DIR/VERSION" 2>/dev/null)"
  case "$GSTACK_V" in
    ''|*stub*|0.0.0*)
      log "gstack install at $GSTACK_DIR looks stubbed or invalid (VERSION='$GSTACK_V') — reinstalling..."
      _gstack_aside invalid
      ;;
    *) GSTACK_OK=1 ;;
  esac
fi
if [ "$GSTACK_OK" -eq 1 ]; then
  log "gstack already installed: v$(cat "$GSTACK_DIR/VERSION") (upgrade with /gstack-upgrade)"
  # An existing install is never replaced for being at another commit (it may be
  # a person's own checkout), but the drift is said out loud: the pin reaches
  # only machines that install fresh.
  _GS_HEAD="$(git -C "$GSTACK_DIR" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$_GS_HEAD" ] && [ "$_GS_HEAD" != "$GSTACK_COMMIT" ]; then
    log "note: gstack is at ${_GS_HEAD:0:7}, the harness pin is ${GSTACK_COMMIT:0:7}. Nothing changed; move it aside and re-run to take the pin."
  fi
else
  # Clone INTO $GSTACK_DIR, never into a temp dir: run from outside a skills/
  # directory, ./setup symlinks ~/.claude/skills/gstack to wherever it ran, so
  # a mktemp clone left that link dangling the moment the EXIT trap deleted it.
  # The clone is staged OUTSIDE skills/ (a live session would load a staged
  # copy as a second set of gstack skills, and a killed run would leave it
  # there) and renamed in, so a failed clone never leaves a half-written
  # install behind.
  mkdir -p "$(dirname "$GSTACK_DIR")" "$HOME/.gstack"
  [ -L "$GSTACK_DIR" ] && rm -f "$GSTACK_DIR"   # a dangling link from that old bug
  GSTACK_STAGE="$HOME/.gstack/install-stage.$$"
  HARNESS_TMPDIRS+=("$GSTACK_STAGE")   # shared list, not a second EXIT trap (see above)
  log "cloning gstack (pinned to ${GSTACK_COMMIT})..."
  git clone "$GSTACK_REPO" "$GSTACK_STAGE"
  git -C "$GSTACK_STAGE" checkout --quiet "$GSTACK_COMMIT"
  _gstack_aside incomplete
  mv "$GSTACK_STAGE" "$GSTACK_DIR"
  # Telemetry off BEFORE setup: setup emits onboarding events (_setup_welcome,
  # _setup_playwright) that honour the setting, so forcing it only afterwards
  # let a machine whose ~/.gstack/config.yaml said `community` send them.
  "$GSTACK_DIR/bin/gstack-config" set telemetry off >/dev/null 2>&1 || true
  : > "$GSTACK_DIR/$GSTACK_SETUP_MARK"
  log "running gstack setup (in place at $GSTACK_DIR)..."
  # A failed setup must not leave a checkout behind: its VERSION and
  # bin/gstack-config would pass the health check above on the next run, which
  # would then skip setup for good. It is this run's own pristine clone, so it
  # is removed (not backed up: a machine without bun fails every run, and a
  # backup per run would only accumulate). The next run clones and retries.
  # A person's install moved aside above is put back: a failed fresh setup must
  # not leave them with no gstack at all.
  if ! (cd "$GSTACK_DIR" && ./setup); then
    rm -rf "$GSTACK_DIR"
    if [ -n "$GSTACK_ASIDE_TO" ] && [ -e "$GSTACK_ASIDE_TO" ]; then
      mv "$GSTACK_ASIDE_TO" "$GSTACK_DIR" && log "restored the previous install from $GSTACK_ASIDE_TO"
    fi
    log "ERROR: gstack ./setup failed (see its output above). Fix the cause and re-run the installer."
    exit 1
  fi
  rm -f "$GSTACK_DIR/$GSTACK_SETUP_MARK"
  GSTACK_V="$(cat "$GSTACK_DIR/VERSION" 2>/dev/null)"
  case "$GSTACK_V" in
    ''|*stub*|0.0.0*)
      log "ERROR: gstack setup finished but the install looks incomplete or stubbed (VERSION='$GSTACK_V'). Re-run the installer."
      exit 1
      ;;
  esac
  [ -x "$GSTACK_DIR/bin/gstack-config" ] || {
    log "ERROR: gstack setup finished but bin/gstack-config is missing. Re-run the installer."
    exit 1
  }
  log "gstack installed: v$(cat "$GSTACK_DIR/VERSION" 2>/dev/null || echo unknown)"
fi

# Somnio policy: gstack telemetry stays off. Enforced on every install AND
# update run (gstack upgrades preserve ~/.gstack/config.yaml, so this sticks).
if [ -x "$GSTACK_DIR/bin/gstack-config" ]; then
  "$GSTACK_DIR/bin/gstack-config" set telemetry off >/dev/null 2>&1 || true
  log "gstack telemetry: $("$GSTACK_DIR/bin/gstack-config" get telemetry 2>/dev/null || echo unknown) (Somnio default: off)"
fi

# --- 3. ruflo user-level harness pieces (pointer block, no repo pollution) ----------
# Marker owned by upstream ruflo (block header written by 'ruflo init'); kept
# in sync with doctor.sh and tests/e2e-harness/container-test.sh.
RUFLO_MARKER="Ruflo Integration"
if grep -q "$RUFLO_MARKER" "$HOME/.claude/CLAUDE.md" 2>/dev/null; then
  log "ruflo integration block already present in ~/.claude/CLAUDE.md"
else
  SCRATCH="$(mktemp -d)"
  HARNESS_TMPDIRS+=("$SCRATCH")   # on the shared list too, so a failure mid-init cannot leak it
  log "running 'ruflo init' in a scratch directory to install the global pointer block..."
  (cd "$SCRATCH" && ruflo init --minimal --no-signup --no-skills-sh >/dev/null 2>&1) || \
    log "WARN: 'ruflo init' reported a problem; run it manually in a scratch directory if the block is still missing."
  rm -rf "$SCRATCH"
fi

log "settings step (offered by /somnio-engineering-ai:install, or run scripts/apply-settings.sh): the repo's own dependencies decide its network policy, so the step runs skills/harness-init/scripts/detect_network.py over the repo and seeds the detected hosts and CLI exclusions into .claude/settings.json for you to review."
log "done. Restart the Claude Code session so the ruflo MCP server and gstack skills load."
