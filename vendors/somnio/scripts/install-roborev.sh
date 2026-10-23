#!/usr/bin/env bash
# The roborev step of the Somnio harness installer (ADR-0005, Layers 1+2).
#
# Extracted from install.sh on purpose. install.sh is a single linear script
# that also npm-installs ruflo, clones gstack and runs `ruflo init`; the
# download/verify/config fixtures cannot be driven against that. This script is
# invoked by install.sh and is separately executable, so tests/roborev/ can run
# it directly with PATH shims for curl and shasum.
#
# THE PIN LIVES IN install.sh, NOT HERE. tests/rules-source/pin-test.sh enforces
# that a supply-chain literal appears exactly once, in install.sh, and that
# every other executable reads it back — and this file sits under the
# plugins/**/*.sh glob that check scans. So the pin and the four per-asset
# sha256 values arrive as environment, and this script fails loudly if they do
# not. tests/roborev/install-test.sh asserts the same rule for roborev's pins.
#
# Why the sha256 is recorded in our repository at all: upstream publishes no
# independently verifiable checksum. Each asset ships a sibling
# .provenance.json, but it is self-published in the same release — using its
# digest to verify that release is circular — and `gh attestation verify`
# returns HTTP 404 because nothing is registered with GitHub (ADR-0005 Q-4).
set -euo pipefail

# stderr, not stdout: detect_platform and sha256_of are called inside command
# substitutions, which capture stdout — so every fatal diagnostic they raised was
# swallowed and the installer died silently under set -e.
log() { printf "[harness] %s\n" "$*" >&2; }

# EXIT CODE CONTRACT, read by install.sh's call site.
#
#   1  the harness is misconfigured (no pin, a malformed pin) — our bug.
#   2  ENVIRONMENT: this machine cannot install roborev. Unsupported OS or
#      architecture, no curl/wget, no hashing tool, no sha pinned for the
#      platform. Re-running changes nothing until the machine changes; the
#      installer WARNs and continues so the mandatory batteries still land.
#   3  INTEGRITY: the artifact is not what the pin says it is. Checksum
#      mismatch, no executable inside a verified tarball, or a post-install
#      version that disagrees with the pin.
#
# 2 and 3 are deliberately NOT the same code. install.sh already takes the hard
# line on this event for uv — "a supply-chain signal, not a transient error. Do
# not bypass it." — and a single non-zero code let the roborev step answer a
# digest mismatch with a WARN and "just re-run it", which is the wrong advice:
# re-running re-downloads the same mismatching artifact.
ROBOREV_EXIT_ENV=2
ROBOREV_EXIT_INTEGRITY=3

ROBOREV_INSTALL_DIR="${ROBOREV_INSTALL_DIR:-$HOME/.local/bin}"
ROBOREV_RELEASE_BASE="${ROBOREV_RELEASE_BASE:-https://github.com/kenn-io/roborev/releases/download}"

# --- the pin arrives from install.sh, it is never defined here ---------------
require_pin() {
  if [ -z "${ROBOREV_VERSION:-}" ]; then
    log "ERROR: ROBOREV_VERSION is not set."
    log "       This script is invoked by install.sh, which owns the pin. Running it"
    log "       standalone requires exporting ROBOREV_VERSION and the four"
    log "       ROBOREV_SHA256_<os>_<arch> values first."
    exit 1
  fi
  case "$ROBOREV_VERSION" in
    v*) log "ERROR: ROBOREV_VERSION carries a leading 'v'. Store it bare; the release"
        log "       tag's 'v' is prepended at use."
        exit 1 ;;
  esac
}

# --- platform ----------------------------------------------------------------
# Only the tarballs are used. The Linux .deb/.rpm exist but install to /usr/bin
# and need root; this harness installs per user.
detect_platform() {
  local os arch
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux)  os=linux ;;
    *) log "ERROR: unsupported OS '$(uname -s)' — roborev ships darwin and linux tarballs only."
       exit $ROBOREV_EXIT_ENV ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64)  arch=amd64 ;;
    *) log "ERROR: unsupported architecture '$(uname -m)'."
       exit $ROBOREV_EXIT_ENV ;;
  esac
  printf '%s_%s\n' "$os" "$arch"
}

expected_sha_for() {
  local platform="$1" var
  var="ROBOREV_SHA256_${platform}"
  printf '%s\n' "${!var:-}"
}

# --- the hashing tool --------------------------------------------------------
# Never a warn-and-continue. The recorded pitfall is uv-installer.sh, which
# skips verification when sha256sum is absent and returns success — and stock
# macOS ships shasum, not sha256sum, so on the primary developer platform that
# check silently no-ops. Refuse instead.
sha256_of() {
  local file="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  else
    log "ERROR: neither shasum nor sha256sum found; refusing to install an unverified binary."
    exit $ROBOREV_EXIT_ENV
  fi
}

# --- current state -----------------------------------------------------------
installed_version() {
  local bin="$ROBOREV_INSTALL_DIR/roborev"
  [ -x "$bin" ] || return 1
  # `version --json` needs no repository and no running daemon.
  # First match only: a greedy .* takes the LAST "version" on a minified line, so
  # a future nested {"build":{"version":...}} would silently win.
  "$bin" version --json 2>/dev/null \
    | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
    | sed -E 's/.*"v?([^"]+)"$/\1/'
}

install_roborev() {
  require_pin
  local platform expected url tmp tarball actual current
  platform="$(detect_platform)"
  expected="$(expected_sha_for "$platform")"

  if [ -z "$expected" ]; then
    log "ERROR: no sha256 pinned for platform '$platform'."
    log "       install.sh must define ROBOREV_SHA256_${platform}. Add it with"
    log "       scripts/roborev-bump.sh, which reads the value from the release's"
    log "       provenance and rewrites install.sh."
    exit $ROBOREV_EXIT_ENV
  fi

  if current="$(installed_version)"; then
    if [ "$current" = "$ROBOREV_VERSION" ]; then
      log "roborev $ROBOREV_VERSION already installed"
      ROBOREV_BINARY_CHANGED=0
      return 0
    fi
    # Pin drift is a reinstall, matching how install.sh treats ruflo and
    # agent-device: the pin is the authority, not whatever is on disk.
    log "roborev $current installed, pinned at $ROBOREV_VERSION — reinstalling"
  fi

  url="$ROBOREV_RELEASE_BASE/v${ROBOREV_VERSION}/roborev_${ROBOREV_VERSION}_${platform}.tar.gz"
  tmp="$(mktemp -d)"
  # RETURN alone leaks on every `exit 1` path below — a shell exit bypasses it.
  trap 'rm -rf "$tmp"' RETURN
  trap 'rm -rf "${tmp:-}"' EXIT
  tarball="$tmp/roborev.tar.gz"

  log "fetching roborev $ROBOREV_VERSION ($platform)..."
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$tarball" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tarball" "$url"
  else
    log "ERROR: neither curl nor wget found; cannot fetch the pinned roborev release."
    exit $ROBOREV_EXIT_ENV
  fi

  actual="$(sha256_of "$tarball")"
  if [ "$actual" != "$expected" ]; then
    log "ERROR: roborev checksum mismatch — refusing to install."
    log "       expected: $expected"
    log "       actual:   $actual"
    log "       This is a supply-chain signal, not a transient error. Do not bypass it."
    log "       If the release was legitimately re-cut, re-run scripts/roborev-bump.sh"
    log "       and review the new digest in the bump PR."
    exit $ROBOREV_EXIT_INTEGRITY
  fi
  log "checksum verified"

  tar -xzf "$tarball" -C "$tmp"
  local extracted
  extracted="$(find "$tmp" -type f -name roborev -perm -u+x 2>/dev/null | head -1)"
  if [ -z "$extracted" ]; then
    log "ERROR: the verified tarball contains no executable named 'roborev'."
    exit $ROBOREV_EXIT_INTEGRITY
  fi

  mkdir -p "$ROBOREV_INSTALL_DIR"
  # Move into place via a UNIQUE temp name in the SAME directory, so an
  # interrupted install can never leave a half-written binary on PATH. The name
  # must be unique, not merely dotted: a fixed ".roborev.new" lets two concurrent
  # installs write the same inode, and the first `mv` can publish a file the
  # second process is still writing.
  local staged
  staged="$(mktemp "$ROBOREV_INSTALL_DIR/.roborev.new.XXXXXX")"
  # Cleaned up explicitly on failure. A UNIQUE staging name fixes the
  # concurrency hazard a fixed ".roborev.new" had, but it replaces it with an
  # accumulation one: without this, a developer whose disk or quota is full
  # retries the installer and leaves a 40 MB orphan behind each time, where the
  # fixed name at least overwrote itself.
  if ! install -m 0755 "$extracted" "$staged" \
     || ! mv -f "$staged" "$ROBOREV_INSTALL_DIR/roborev"; then
    rm -f "$staged"
    log "ERROR: could not publish the roborev binary into $ROBOREV_INSTALL_DIR."
    exit $ROBOREV_EXIT_ENV
  fi

  # Verify by ABSOLUTE PATH. ~/.local/bin is often not on the installing
  # shell's PATH yet — the same trap install.sh already documents for uv — so
  # `command -v roborev` here proves nothing.
  local got
  got="$(installed_version || true)"
  if [ "$got" != "$ROBOREV_VERSION" ]; then
    log "ERROR: installed roborev reports '${got:-nothing}', expected $ROBOREV_VERSION."
    exit $ROBOREV_EXIT_INTEGRITY
  fi
  log "roborev $ROBOREV_VERSION installed to $ROBOREV_INSTALL_DIR/roborev"
  ROBOREV_BINARY_CHANGED=1
  case ":$PATH:" in
    *":$ROBOREV_INSTALL_DIR:"*) ;;
    *) log "note: $ROBOREV_INSTALL_DIR is not on this shell's PATH yet — open a new shell, or run it by full path." ;;
  esac
}

# --- supervisor (ADR-0005 D-11) ----------------------------------------------
# D-11 authorizes this as a CONVENIENCE, not a correctness fix: upstream's daemon
# starts on demand for any command that needs it, the post-commit hook included,
# and the docs call that "sufficient for most users". What supervision buys is
# reboot survival and restart-on-failure — and, because the hook exits silently
# so it never blocks a commit, a down daemon yields a review that never ran and
# is indistinguishable from one that passed.
#
# Hand-written, because upstream ships roborev.service/roborev.socket only via
# its deb/rpm packages and this harness installs the release tarball. The label
# is ours (io.somnio.*), not a vendor-shaped com.roborev.*, so the rollback
# procedure is unambiguous about whose unit is being removed. That procedure
# lives in docs/DEV_HARNESS.md (§ roborev), NOT in ADR-0005 — the ADR records
# the decision to supervise, not the removal steps.
#
# GUARDED AND NON-FATAL. Where no supervisor exists — containers, CI — this
# prints a manual-start line and returns 0. install.sh runs under
# 'set -euo pipefail', so a failure here would abort the whole installer.
ROBOREV_LAUNCHD_LABEL="io.somnio.roborev-daemon"
ROBOREV_SYSTEMD_UNIT="somnio-roborev"

detect_supervisor() {
  case "$(uname -s)" in
    Darwin)
      command -v launchctl >/dev/null 2>&1 && printf 'launchd\n' && return 0 ;;
    Linux)
      # A systemd USER manager needs more than the binary: no session bus (the
      # container case) means `systemctl --user` cannot talk to anything.
      if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        printf 'systemd\n'; return 0
      fi ;;
  esac
  printf 'none\n'
}

install_supervisor() {
  local sup bin
  bin="$ROBOREV_INSTALL_DIR/roborev"
  sup="$(detect_supervisor)"

  # A launchd/systemd unit is USER-SCOPE and outlives this shell, so the path it
  # names must outlive it too. A unit pointing into a temp dir survives only
  # while the original process happens to still be resident: the next reboot
  # starts nothing and reports success, with no warning anywhere. Observed in
  # the field - a plist naming /var/folders/.../T/tmp.XXXXXX/roborev with
  # `state = spawn scheduled, active count = 0`, one reboot from silent death.
  # Refuse rather than write it; a missing unit is loud, a dead one is not.
  case "$bin" in
    /var/folders/*|/tmp/*|/private/tmp/*|/private/var/folders/*)
      log "WARN: refusing to write a $sup unit naming a temporary path ($bin)."
      log "       A user-scope unit must name a durable binary. Re-run the installer"
      log "       with ROBOREV_INSTALL_DIR pointing at the installed location."
      return 0 ;;
  esac
  if [ ! -x "$bin" ]; then
    log "WARN: refusing to write a $sup unit naming $bin — it is not an executable file."
    return 0
  fi

  if [ "$sup" = none ]; then
    log "no user-level supervisor detected — skipping the roborev daemon unit (ADR-0005 D-11 makes this non-fatal)."
    log "       start it by hand when you need it:  $bin daemon start"
    return 0
  fi

  # Every roborev init call site passes --no-daemon, because upstream warns that
  # an auto-started daemon conflicts with a service-managed one. Nothing in this
  # step registers a repository, so there is no init call here at all.
  if [ "$sup" = launchd ]; then
    local plist="$HOME/Library/LaunchAgents/${ROBOREV_LAUNCHD_LABEL}.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/roborev"
    cat > "$plist.new.$$" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${ROBOREV_LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>${bin}</string><string>daemon</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${ROBOREV_INSTALL_DIR}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>ROBOREV_TELEMETRY_ENABLED</key><string>0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOME}/Library/Logs/roborev/daemon.log</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/roborev/daemon.err</string>
</dict>
</plist>
PLIST
    _supervisor_commit_unit "$plist" "launchd unit"
    return 0
  fi

  local unit="$HOME/.config/systemd/user/${ROBOREV_SYSTEMD_UNIT}.service"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$unit.new.$$" <<UNIT
[Unit]
Description=roborev review daemon (Somnio harness, ADR-0005 D-11)
After=default.target

[Service]
Type=simple
ExecStart=${bin} daemon run
Environment=ROBOREV_TELEMETRY_ENABLED=0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  _supervisor_commit_unit "$unit" "systemd user unit"
  return 0
}

# Move a freshly rendered unit into place and record whether it CHANGED, in
# SUPERVISOR_UNIT_CHANGED (1/0), for start_supervisor: reloading a running job
# whose unit is byte-identical buys nothing and, on launchd, is what dropped the
# daemon on every re-run of this installer (see start_supervisor).
# Unset until install_supervisor writes a unit: start_supervisor then reads it as
# "changed" (reload), restart_daemon_if_running as "no unit reason" (a refused or
# skipped unit write must not trigger a restart).
SUPERVISOR_UNIT_CHANGED="${SUPERVISOR_UNIT_CHANGED:-}"
_supervisor_commit_unit() { # _supervisor_commit_unit <path> <label>
  if [ -f "$1" ] && cmp -s "$1.new.$$" "$1"; then
    rm -f "$1.new.$$"
    SUPERVISOR_UNIT_CHANGED=0
    log "$2 unchanged at $1"
  else
    mv -f "$1.new.$$" "$1"
    SUPERVISOR_UNIT_CHANGED=1
    log "$2 written to $1"
  fi
}

# Bootstrapping is separate from writing the unit, so the daemon starts only
# after write_device_config has produced a config for it to read.
start_supervisor() {
  local bin="$ROBOREV_INSTALL_DIR/roborev"
  case "$(detect_supervisor)" in
    launchd)
      local plist="$HOME/Library/LaunchAgents/${ROBOREV_LAUNCHD_LABEL}.plist"
      local target="gui/$(id -u)/${ROBOREV_LAUNCHD_LABEL}" i=0
      [ -f "$plist" ] || return 0
      if launchctl print "$target" >/dev/null 2>&1; then
        # Loaded with the same unit: nothing to reload. A new binary at the
        # same path is picked up by restart_daemon_if_running's kickstart.
        if [ "${SUPERVISOR_UNIT_CHANGED:-1}" = 0 ]; then
          log "roborev daemon already supervised by launchd (${ROBOREV_LAUNCHD_LABEL}, unit unchanged)"
          return 0
        fi
        # `bootout` returns BEFORE launchd has removed the job, and a bootstrap
        # issued meanwhile fails. Bootout-then-bootstrap back to back therefore
        # left NO daemon on every re-run (verified on this harness, 2026-09-24:
        # `state = running` before, "Could not find service" after). Wait for the
        # job to be gone, bounded, before loading the new unit.
        launchctl bootout "$target" >/dev/null 2>&1 || true
        while [ "$i" -lt 20 ] && launchctl print "$target" >/dev/null 2>&1; do
          sleep 0.5; i=$((i + 1))
        done
      fi
      i=0
      until launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 3 ]; then
          log "WARN: wrote $plist but launchctl bootstrap failed 3 times — start it by hand: launchctl bootstrap gui/$(id -u) $plist"
          return 0
        fi
        sleep 1
      done
      ROBOREV_DAEMON_FRESH=1   # a new process: it already runs this binary and config
      log "roborev daemon supervised by launchd (${ROBOREV_LAUNCHD_LABEL})" ;;
    systemd)
      if [ "${SUPERVISOR_UNIT_CHANGED:-1}" = 0 ] \
         && systemctl --user is-active --quiet "${ROBOREV_SYSTEMD_UNIT}.service" 2>/dev/null; then
        log "roborev daemon already supervised by systemd --user (${ROBOREV_SYSTEMD_UNIT}, unit unchanged)"
        return 0
      fi
      local was_active=0
      systemctl --user is-active --quiet "${ROBOREV_SYSTEMD_UNIT}.service" 2>/dev/null && was_active=1
      systemctl --user daemon-reload >/dev/null 2>&1 || true
      if systemctl --user enable --now "${ROBOREV_SYSTEMD_UNIT}.service" >/dev/null 2>&1; then
        # enable --now STARTS an inactive unit (a fresh process on this binary and
        # config) but does not restart an active one, whose new unit then still
        # needs restart_daemon_if_running (SUPERVISOR_UNIT_CHANGED).
        [ "$was_active" = 0 ] && ROBOREV_DAEMON_FRESH=1
        log "roborev daemon supervised by systemd --user (${ROBOREV_SYSTEMD_UNIT})"
      else
        log "WARN: 'systemctl --user enable --now' failed — start it by hand: $bin daemon start"
      fi ;;
  esac
  return 0
}

# --- device config (ADR-0005 D-7, D-9) ---------------------------------------
# Per device, never per repository: harness-init cannot own this file.
#
# WHY NOT `roborev config set --global`? It exists, and it is the obvious deeper
# fix — but probed against the real binary (0.67.0) it rewrites the WHOLE file:
# an 8-line config came back as ~250 lines with every default materialised and
# its doc-comment inlined, and the developer's own comments gone. Values are
# preserved; intent is not. Worse, freezing every default into the file means a
# future roborev release's changed default silently stops applying. A targeted
# key-set write is the shallower-looking but correct depth here. Do not
# "simplify" this into `config set` without re-running that probe.
#
# NEVER-CLOBBER (ADR-0002 D-3 / ADR-0005 D-3). An existing config is backed up
# with a timestamp whose path is printed, and keys we do not own are preserved
# byte for byte. The repo's only merge tool,
# plugins/somnio-engineering-ai/scripts/merge-settings.py, is JSON-only and
# cannot be reused for TOML, so this is a targeted key-set write: each key we
# own is replaced in place if present and appended if not. Everything else in
# the file is untouched.
ROBOREV_CONFIG_DIR="${ROBOREV_CONFIG_DIR:-$HOME/.roborev}"

# A snapshot of the config as it was BEFORE this run. Empty when there was no
# config to begin with. One variable holds the whole rule: "is there a pre-run
# version worth diffing against at the end", so a fresh install does not back up
# a file this very run caused to exist and report it as the developer's own.
#
# TAKEN IN A FUNCTION, NOT AT TOP LEVEL. As a bare top-level block this ran on
# mere `source`, so every test that sourced this file for one helper copied the
# DEVELOPER'S REAL ~/.roborev/config.toml into $TMPDIR and left it there —
# twice per `make test`, unreferenced and never cleaned up. A snapshot is an
# action; sourcing a script must not perform actions.
ROBOREV_CONFIG_PRERUN=""

snapshot_config() {
  [ -f "$ROBOREV_CONFIG_DIR/config.toml" ] || return 0
  ROBOREV_CONFIG_PRERUN="$(mktemp)" || return 1
  cp -p "$ROBOREV_CONFIG_DIR/config.toml" "$ROBOREV_CONFIG_PRERUN"
  # The developer's only pre-run copy. If the run dies before write_device_config
  # consumes it, restore it rather than leaving the original as an anonymous
  # $TMPDIR file the OS will sweep.
  trap 'roborev_restore_prerun' EXIT
}

roborev_restore_prerun() {
  [ -n "${ROBOREV_CONFIG_PRERUN:-}" ] || return 0
  [ -f "$ROBOREV_CONFIG_PRERUN" ] || return 0
  local keep="$ROBOREV_CONFIG_DIR/config.toml.interrupted.$(date -u +%Y%m%dT%H%M%SZ).bak"
  mkdir -p "$ROBOREV_CONFIG_DIR" 2>/dev/null || true
  if cp -p "$ROBOREV_CONFIG_PRERUN" "$keep" 2>/dev/null; then
    log "the run ended before the config was finished — your pre-run config is at $keep"
  fi
  rm -f "$ROBOREV_CONFIG_PRERUN"
  ROBOREV_CONFIG_PRERUN=""
}

# The TOML section-header pattern, defined ONCE.
#
# This regex has been the source of the same defect twice: a header carrying a
# trailing comment (`[web] # off`) is valid TOML, and a pattern that misses it
# makes the caller append a SECOND table of that name — after which roborev
# refuses to load the config and every command on the device fails, while
# `config validate` still returns 0. It was fixed for [web], then again for
# [agent_hook]. Two fixes for one defect is the argument for one definition.
#
# It also allows whitespace INSIDE the brackets: `[ web ]` names the same table
# in TOML, and every previous spelling of this pattern missed it — the third
# instance of the same class, fixed here before it could be reported a third
# time. Leading whitespace is allowed for the same reason (an indented header
# is valid). The optional quotes cover `["web"]`, a third legal spelling of
# the same table name.
ROBOREV_TOML_SECTION_RE='^[[:space:]]*\[[[:space:]]*"?%s"?[[:space:]]*\][[:space:]]*(#.*)?$'

toml_section_re() {  # $1 = section name -> the ERE matching its header
  printf "$ROBOREV_TOML_SECTION_RE" "$1"
}

toml_has_section() {  # $1 = section name, $2 = file
  grep -qE "$(toml_section_re "$1")" "$2"
}

# Set a top-level key without disturbing the rest of the file. Section-scoped
# keys are handled separately below, because a bare `key = value` line inside a
# [section] belongs to that section, not to the root.
set_root_key() {  # $1 = file, $2 = key, $3 = value (already quoted if a string)
  local f="$1" k="$2" v="$3" t
  # A UNIQUE temp file in the config's own directory. A fixed "$f.tmp" lets two
  # concurrent installs interleave and publish a half-merged config — exactly
  # what the NEVER-CLOBBER contract below promises not to do. Same directory, so
  # the mv stays a rename on one filesystem and therefore atomic.
  # "$f.XXXXXX" lands in the file's own directory by construction, so the mv
  # stays a rename on one filesystem and therefore atomic — without the two
  # subshells a dirname/basename pair costs on every call.
  t="$(mktemp "$f.XXXXXX")" || return 1
  # The value travels through the ENVIRONMENT, not `awk -v`: -v performs escape
  # processing, so a backslash in a value is silently eaten.
  # Section headers are matched with leading whitespace allowed — an indented
  # [section] is valid TOML, and missing it made this rewrite a foreign
  # section-scoped key while leaving the root key unwritten.
  # ONE pass. This replaced a pre-scan awk that only computed found/not-found
  # plus one of two near-identical rewriting awks — three processes per call,
  # with the emit line written three times. The single program covers both
  # cases: rewrite the key in place if it appears at the root, otherwise insert
  # it before the first [section] (or at EOF when there is none) so it stays a
  # root key.
  RV_V="$v" awk -v k="$k" '
    !insec && /^[[:space:]]*\[/ { if (!done) { print k " = " ENVIRON["RV_V"]; done=1 } insec=1 }
    !insec && $0 ~ "^[[:space:]]*"k"[[:space:]]*=" { print k " = " ENVIRON["RV_V"]; done=1; next }
    { print }
    END { if (!done) print k " = " ENVIRON["RV_V"] }' "$f" > "$t" || { rm -f "$t"; return 1; }
  mv "$t" "$f"
}

write_device_config() {
  local cfg="$ROBOREV_CONFIG_DIR/config.toml" backup
  mkdir -p "$ROBOREV_CONFIG_DIR"

  # Backed up only when the file PRE-EXISTED this run and its content will
  # actually change. A backup per run accumulates .bak files that never held
  # anything worth keeping, and two runs in the same second overwrite each
  # other's anyway (the stamp is second-granularity).
  [ -f "$cfg" ] || : > "$cfg"

  # D-7: socket-only. The 0600 socket (0700 parent) is how this coexists with
  # ADR-0004's sandbox — reaching it is a filesystem permission, never an
  # excludedCommands entry, so business rule 18 is untouched.
  #
  # The parent is chmod 0700 HERE, before the daemon ever binds, because the
  # daemon REFUSES to bind a socket whose parent is group/other-readable and
  # reports "unsafe permissions 755". An installer that writes an explicit
  # server_addr without tightening the directory ships a daemon that cannot
  # start, and the failure surfaces only at first use.
  chmod 0700 "$ROBOREV_CONFIG_DIR" 2>/dev/null || \
    log "WARN: could not chmod 0700 $ROBOREV_CONFIG_DIR — the daemon refuses to bind a socket under a group-readable parent."
  # ABSOLUTE, not the empty "unix://". Empty makes roborev derive the path from
  # $TMPDIR, and $TMPDIR is not stable: Claude Code's sandbox rewrites it to
  # /tmp/claude-<uid>, so the daemon bound one path while every probe looked at
  # another and reported "nothing listening" against a daemon serving fine.
  set_root_key "$cfg" server_addr "\"unix://$ROBOREV_CONFIG_DIR/daemon.sock\""
  # D-9: explicit. Upstream's terminal fallback in the agent chain is `codex`;
  # inheriting it would silently change provider and defeat D-1's record.
  set_root_key "$cfg" default_agent '"claude-code"'
  set_root_key "$cfg" review_agent  '"claude-code"'
  # Daemon-wide, so one saturating run silences reviews for every registered
  # repository. An accepted limit, recorded rather than solved.
  set_root_key "$cfg" agent_quota_cooldown '"30m"'
  # D-9 wants a backup agent configured, because of that daemon-wide cooldown.
  # Only written when the device actually has a second provider — an empty
  # string is not a configured value, and #44's doctor reports it unset.
  if [ -n "${ROBOREV_BACKUP_AGENT:-}" ]; then
    set_root_key "$cfg" default_backup_agent "\"$ROBOREV_BACKUP_AGENT\""
  fi

  # D-7: the browser listener defaults to TRUE upstream, on an ephemeral
  # loopback port serving job logs, patches and review content. server_addr
  # does not close it, so it is turned off explicitly.
  # Header matching goes through toml_has_section / toml_section_re so the
  # trailing-comment and `[ web ]` cases are handled in one place, not here.
  if toml_has_section web "$cfg"; then
    local webtmp
    webtmp="$(mktemp "$cfg.XXXXXX")" || return 1
    awk -v hdr="$(toml_section_re web)" '
      $0 ~ hdr { print; inweb=1; next }
      inweb && /^[[:space:]]*\[/ { if (!wrote) { print "enabled = false"; wrote=1 } inweb=0 }
      inweb && /^[[:space:]]*enabled[[:space:]]*=/ { print "enabled = false"; wrote=1; next }
      { print }
      END { if (inweb && !wrote) print "enabled = false" }' "$cfg" > "$webtmp" || { rm -f "$webtmp"; return 1; }
    mv "$webtmp" "$cfg"
  else
    printf '\n[web]\nenabled = false\n' >> "$cfg"
  fi

  # Keep the pre-run snapshot as a backup only if the writes actually changed
  # something. A backup per run accumulates .bak files that never held anything
  # worth keeping, and two runs in the same second would overwrite each other's
  # anyway (the stamp is second-granularity).
  # The same comparison tells restart_daemon_if_running whether the daemon has
  # anything to pick up: server_addr and [web] are not hot-reloaded.
  ROBOREV_CONFIG_CHANGED=1
  if [ -n "$ROBOREV_CONFIG_PRERUN" ]; then
    if cmp -s "$ROBOREV_CONFIG_PRERUN" "$cfg"; then
      rm -f "$ROBOREV_CONFIG_PRERUN"
      ROBOREV_CONFIG_CHANGED=0
    else
      backup="$cfg.$(date -u +%Y%m%dT%H%M%SZ).bak"
      mv "$ROBOREV_CONFIG_PRERUN" "$backup"
      log "existing roborev config backed up to $backup"
    fi
    ROBOREV_CONFIG_PRERUN=""
  fi

  if [ "$ROBOREV_CONFIG_CHANGED" = 1 ]; then
    log "roborev device config written to $cfg"
  else
    log "roborev device config unchanged at $cfg"
  fi

  # Telemetry (business rule 14's posture). roborev reads ROBOREV_TELEMETRY_ENABLED
  # from the environment; it documents no config key for it. An installer-process
  # export is therefore INERT for a daemon the installer does not own — so the
  # value is persisted in the supervisor unit's environment (written above), and
  # where no supervisor exists we say so rather than implying it is handled.
  if [ "$(detect_supervisor)" = none ]; then
    log "note: telemetry-off could not be persisted (no supervisor unit on this machine)."
    log "      export ROBOREV_TELEMETRY_ENABLED=0 in your shell rc, or start the daemon with it set."
  fi
}

# Restart only when a daemon is ALREADY running and a supervisor exists.
# server_addr and [web] are two of the four settings roborev does not hot-reload,
# but on a fresh install nothing has started a daemon — no repository is
# registered and this step never calls `roborev init` — so an unconditional
# restart would either no-op or spuriously START one on a machine where the
# supervisor step just skipped. Non-fatal either way.
restart_daemon_if_running() {
  local bin="$ROBOREV_INSTALL_DIR/roborev"
  [ -x "$bin" ] || return 0
  local sup; sup="$(detect_supervisor)"
  [ "$sup" != none ] || return 0
  # Ask the SUPERVISOR whether its job runs, never `roborev daemon status`: that
  # command STARTS a daemon when none is up, and since 0.68 it also RESTARTS one
  # whose version differs from the CLI — right after a binary swap, that is a
  # restart outside the supervisor, the thing this function exists to avoid.
  local running=0
  case "$sup" in
    launchd) launchctl print "gui/$(id -u)/${ROBOREV_LAUNCHD_LABEL}" 2>/dev/null \
               | grep -q '^[[:space:]]*state = running' && running=1 ;;
    systemd) systemctl --user is-active --quiet "${ROBOREV_SYSTEMD_UNIT}.service" 2>/dev/null && running=1 ;;
  esac
  [ "$running" = 1 ] || return 0
  # Restart only for something the running process cannot pick up on its own: a
  # new binary, or a config it does not hot-reload. A restart on every run
  # (`kickstart -k`) killed whatever review was in flight, on every re-run of
  # this installer (observed 2026-09-24: a new daemon PID after each run). The
  # flags are set by this run's own steps; unset means a caller outside that
  # flow, which keeps the old behaviour and restarts.
  #
  # A restart that FAILED leaves a marker, so the next run retries it even though
  # it then sees nothing changed; without it a failed kickstart meant the daemon
  # kept a stale server_addr or [web] listener with nothing ever flagging it.
  local pending="$ROBOREV_CONFIG_DIR/.somnio-restart-pending" why="" ok=1
  if [ "${ROBOREV_DAEMON_FRESH:-0}" = 1 ]; then
    rm -f "$pending"
    log "roborev daemon just started under its supervisor — no restart needed"
    return 0
  fi
  [ "${ROBOREV_BINARY_CHANGED:-1}" = 1 ] && why="a new binary"
  [ "${ROBOREV_CONFIG_CHANGED:-1}" = 1 ] && why="${why:+$why and }a changed device config"
  # launchd reloads a changed unit by bootstrapping it (DAEMON_FRESH above);
  # systemd's enable --now leaves an active unit on its OLD ExecStart/Environment.
  [ "${SUPERVISOR_UNIT_CHANGED:-0}" = 1 ] && why="${why:+$why and }a changed supervisor unit"
  [ -f "$pending" ] && why="${why:+$why and }a restart a previous run could not complete"
  if [ -z "$why" ]; then
    log "roborev daemon running, binary and config unchanged — left alone (no review interrupted)"
    return 0
  fi
  log "restarting the roborev daemon to pick up $why"
  # Ask the SUPERVISOR. Killing the process directly makes a KeepAlive=true
  # launchd job respawn its own copy, and the CLI restart can lose the socket.
  case "$sup" in
    launchd) launchctl kickstart -k "gui/$(id -u)/${ROBOREV_LAUNCHD_LABEL}" >/dev/null 2>&1 || ok=0 ;;
    systemd) systemctl --user restart "${ROBOREV_SYSTEMD_UNIT}.service" >/dev/null 2>&1 || ok=0 ;;
    *)       "$bin" daemon restart >/dev/null 2>&1 || ok=0 ;;
  esac
  if [ "$ok" = 1 ]; then
    rm -f "$pending"
  else
    mkdir -p "$ROBOREV_CONFIG_DIR" && : > "$pending"
    log "WARN: the roborev daemon restart failed; the next installer run retries it, or restart it by hand."
  fi
  return 0
}

# --- user-scope consent (ADR-0005 D-3, D-4) ----------------------------------
# ONE prompt for both writes, because both land in ~/.claude/ outside this
# plugin: the agent hook (which BLOCKS a session from stopping while a fix
# session is unfinished) and roughly ten slash commands.
#
# Non-interactive default is N. install.sh runs under 'set -euo pipefail' and is
# executed headless by the e2e suite; a read on a closed stdin must decline, not
# hang and not abort. Declining writes NOTHING — which is why the [agent_hook]
# thresholds are written on the accept path only.
install_user_scope_assets() {
  local bin="$ROBOREV_INSTALL_DIR/roborev" answer settings backup
  [ -x "$bin" ] || return 0

  if [ -n "${ROBOREV_ASSUME_NO:-}" ]; then
    answer=n
  elif [ ! -t 0 ]; then
    # A non-interactive install now INSTALLS rather than skips. The old default
    # was measured wrong on 2026-09-18: this machine ran nine days with Layer 1
    # reviewing every commit and Layer 2 absent, so 17 reviews — 12 of them
    # finding something — landed in reviews.db and reached nobody. A scripted
    # install silently reproducing that is a fleet of half-installed machines.
    # ROBOREV_ASSUME_NO is the scripted opt-OUT, and it is honoured above.
    log "non-interactive session — installing the user-scope roborev assets (agent hook, skills)."
    log "       these write to ~/.claude/ (backed up first). Set ROBOREV_ASSUME_NO=1 to decline."
    answer=y
  else
    cat <<'PROMPT'
[harness] This is LAYER 2, and without it roborev is half-installed: reviews still
[harness]   run on every commit, and their findings reach nobody — they land in
[harness]   ~/.roborev/reviews.db and stay there. Feeding findings into the live
[harness]   session, before a PR exists, is what ENG-REV-05 is for.
[harness]
[harness] roborev will write into your USER config (~/.claude/), outside this plugin:
[harness]   - an agent hook that BLOCKS a session from stopping while a roborev
[harness]     fix session is unfinished (ADR-0005 D-3)
[harness]   - ~10 slash commands under ~/.claude/skills/ (ADR-0005 D-4)
[harness] Accepting also writes an [agent_hook] threshold block into
[harness]   ~/.roborev/config.toml (declining writes nothing).
[harness] Removal: delete the roborev keys from ~/.claude/settings.json, the
[harness]   roborev-* skill directories, and that [agent_hook] block. The daemon
[harness]   unit installed earlier (io.somnio.roborev-daemon / somnio-roborev) is
[harness]   separate and is removed too — full procedure in the harness docs,
[harness]   docs/DEV_HARNESS.md (roborev).
PROMPT
    printf '[harness] Install them? [Y/n] '
    read -r answer || answer=y
  fi

  # Default Y (area decision, 2026-09-18). N was the conservative-looking choice
  # for a user-scope write, and it produced a battery whose default install has
  # no value — measured, not argued. ADR-0002 D-2 is unchanged and still holds:
  # the hazard prints BEFORE any mutation, and declining still writes nothing.
  case "${answer:-y}" in
    y|Y|yes|YES|"") ;;
    *) log "declined — no user-scope roborev assets installed, and no [agent_hook] config written."
       return 0 ;;
  esac

  settings="$HOME/.claude/settings.json"
  # Copied BEFORE the install (nothing says in advance whether it will write),
  # kept only if the install CHANGED the file. A backup per run piled up one
  # .roborev.bak per installer run on every machine: seven on the machine that
  # found it (2026-09-24), all byte-identical to the file they "protected".
  # Same rule as write_device_config's config backup.
  backup=""
  if [ -f "$settings" ]; then
    backup="$settings.$(date -u +%Y%m%dT%H%M%SZ).roborev.bak"
    cp -p "$settings" "$backup"
  fi

  "$bin" agent-hook install --agent claude >/dev/null 2>&1 \
    && log "roborev agent hook installed for Claude Code" \
    || log "WARN: 'roborev agent-hook install' failed — re-run it by hand."
  if [ -n "$backup" ]; then
    if cmp -s "$backup" "$settings"; then
      rm -f "$backup"
      log "~/.claude/settings.json unchanged by the agent hook install — no backup kept"
    else
      log "~/.claude/settings.json changed by the agent hook install — previous version backed up to $backup"
    fi
  fi
  # --agent + --path, never bare: since 0.68 a bare `skills install` writes into
  # every agent config dir it finds (Cursor, Gemini, Qwen, Hermes, ...) — the
  # same trap as a bare `agent-hook install`. Claude Code only.
  "$bin" skills install --agent claude --path "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills" >/dev/null 2>&1 \
    && log "roborev agent skills installed" \
    || log "WARN: 'roborev skills install' failed — re-run it by hand."

  # Written only here, on the accept path: config for a hook that was never
  # installed would contradict "declining writes nothing".
  local cfg="$ROBOREV_CONFIG_DIR/config.toml"
  # Same shared matcher as [web] — see toml_section_re for why this is not an
  # inline regex.
  if [ -f "$cfg" ] && ! toml_has_section agent_hook "$cfg"; then
    printf '\n[agent_hook]\nturn_threshold = 5\ncommit_threshold = 0\nfailed_review_threshold = 4\n' >> "$cfg"
    log "wrote [agent_hook] thresholds (upstream defaults, pinned so a vendor default change cannot alter behaviour silently)"
  fi
}

# Business rule 15: every externally-sourced tool is pinned to an exact version
# and bumps are PRs. `roborev update`, Homebrew and the upstream curl|bash
# installer all track latest, so none of them may appear on an executable path
# here. This function exists so the refusal is testable, and so a future edit
# that reaches for the convenient path fails a fixture instead of shipping.
roborev_self_update_is_forbidden() {
  log "ERROR: 'roborev update' floats to the latest release."
  log "       Business rule 15: bump ROBOREV_VERSION and the four sha256 pins in"
  log "       install.sh via scripts/roborev-bump.sh, and land it as a PR."
  exit 1
}

# Only run when executed, so the suite can source the file and test one function
# at a time.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  # Snapshotted HERE: once per real run, before anything this run does can
  # create a config. Inside write_device_config it would be too late — a config
  # the daemon wrote earlier in this same run would look like the developer's
  # own and get backed up, which is the confusion the snapshot exists to avoid.
  # At top level it would run on mere `source` and leak the real config.
  snapshot_config
  install_roborev "$@"
  # ORDER IS LOAD-BEARING (D-7). write_device_config comes FIRST: the moment
  # install_supervisor returns, a RunAtLoad/KeepAlive unit is on disk, and
  # launchd/systemd will start it at the next login whatever happens to this
  # run. With the old order, a config write that failed after the unit was
  # written left a daemon that comes up on UPSTREAM defaults — including the
  # web listener serving job logs and patches on a loopback port, which is
  # exactly the posture D-7 exists to close.
  write_device_config
  install_supervisor
  start_supervisor
  restart_daemon_if_running
  install_user_scope_assets
fi
