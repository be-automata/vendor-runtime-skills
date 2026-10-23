#!/usr/bin/env bash
# Doctor for the Somnio developer harness.
#
# Modes:
#   doctor.sh          full report, always prints every check
#   doctor.sh --quiet  SessionStart mode: prints only when something is missing,
#                      so a healthy machine adds zero noise to the session context
#
# This script only reads state. It never installs anything; installation is a
# person's explicit action via /somnio-engineering-ai:install.

set -u

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

MISSING=0
# Set when something is missing that /somnio-engineering-ai:install does NOT own, so the
# closing banner never tells a developer to run an installer that cannot fix their problem.
MISSING_NOT_INSTALLABLE=0
REPORT=""

add() { REPORT="${REPORT}${1}\n"; }

# --- ruflo CLI (claude-flow v3) ------------------------------------------------
# The engine is graded, not the wrapper: `ruflo --version` names the wrapper,
# and the wrapper's caret dependency runs whatever @claude-flow/cli npm resolved
# (scripts/ruflo-engine.sh). The pin is read from install.sh, never copied here.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_SCRIPT_DIR/ruflo-engine.sh"
_RUFLO_ENGINE_PIN="$(sed -n 's/^RUFLO_CLI_VERSION="\([^"]*\)".*/\1/p' "$_SCRIPT_DIR/install.sh" 2>/dev/null | head -1)"
if command -v ruflo >/dev/null 2>&1; then
  _RUFLO_ENGINE="$(ruflo_engine_version 2>/dev/null || true)"
  if [ -n "$_RUFLO_ENGINE_PIN" ] && [ "$_RUFLO_ENGINE" = "$_RUFLO_ENGINE_PIN" ]; then
    add "ok  ruflo CLI $(ruflo --version 2>/dev/null | head -1 || echo '(version unknown)') (engine @claude-flow/cli $_RUFLO_ENGINE)"
  else
    add "WARN  ruflo CLI runs @claude-flow/cli ${_RUFLO_ENGINE:-(not found)}, pin ${_RUFLO_ENGINE_PIN:-(unreadable)} — re-run /somnio-engineering-ai:install"
  fi
else
  MISSING=1
  add "MISSING  ruflo CLI — the ruflo MCP server will fall back to a slow npx fetch"
fi

# --- agent-device CLI (iOS/Android simulator automation) -----------------------
if command -v agent-device >/dev/null 2>&1; then
  add "ok  agent-device CLI v$(agent-device --version 2>/dev/null | head -1 || echo '?')"
else
  MISSING=1
  add "MISSING  agent-device CLI — iOS/Android simulator automation unavailable"
fi

# --- uv (Python script runner; /uat runs every script as 'uv run') -------------
# Three reachable states, and the middle one is real: the upstream installer
# writes ~/.local/bin and appends a PATH line to the shell rc, so a session
# started before that edit has the binary on disk and nothing on PATH. Reporting
# only "missing" there sends the developer to reinstall something they already
# have. The pin is read from install.sh rather than copied, so a bump PR that
# touches one file cannot leave the doctor grading against a stale literal.
_INSTALL_SH="$_SCRIPT_DIR/install.sh"
# Every pin this script reports is read the same way: the first `NAME="value"`
# assignment in install.sh, one grep per pin. Pass the name WITH its `=` so the
# literal `NAME=` still appears here — pin-test.sh greps for exactly that, and
# so does anyone auditing which pins the doctor grades against.
# The first quoted value on the first line matching an anchor. Extracted because
# the roborev block needs the identical read against ~/.roborev/config.toml, and
# four copies of this sed is how the two spellings silently drift apart.
_quoted_value() { grep -E "$2" "$1" 2>/dev/null | head -1 | sed -E 's/^[^"]*"([^"]*)".*/\1/'; }
_pin_from_install() { _quoted_value "$_INSTALL_SH" "^$1"; }
# Pin read and version interrogation are full-report only: --quiet throws away every
# ok/info line, and this script runs on every SessionStart. Presence (`command -v`,
# `[ -x ]`) is a builtin and a stat; the version strings cost subprocesses. The
# MISSING verdict below never depends on them.
_UV_PIN=""
[ "$QUIET" -eq 0 ] && _UV_PIN="$(_pin_from_install 'UV_VERSION=')"
if command -v uv >/dev/null 2>&1 && [ "$QUIET" -eq 1 ]; then
  add "ok  uv"
elif command -v uv >/dev/null 2>&1; then
  # `uv --version` prints "uv <ver> (...)"; strip the leading name so the line
  # does not read "ok  uv uv 0.12.9". No `|| echo` fallback here: it would bind to
  # sed, which succeeds with empty output, so it could never fire — the :- default
  # below is what actually covers an unreadable version.
  _UV_V="$(uv --version 2>/dev/null | head -1 | sed 's/^uv //')"
  add "ok  uv ${_UV_V:-(version unknown)}"
  # A mismatch is info, never MISSING: the installer deliberately leaves an
  # existing uv alone, so flagging it as broken would contradict the installer.
  # Both operands must be non-empty — an unreadable version is not a mismatch.
  if [ -z "$_UV_PIN" ]; then
    add "info  could not read the uv pin from $_INSTALL_SH — version comparison skipped"
  elif [ -n "$_UV_V" ]; then
    case "$_UV_V" in
      *"$_UV_PIN"*) : ;;
      *) add "info  uv is not the harness pin ($_UV_PIN) — kept on purpose; the installer never clobbers an existing uv" ;;
    esac
  fi
elif [ -x "$HOME/.local/bin/uv" ]; then
  MISSING=1
  add "MISSING  uv is installed at ~/.local/bin but not on this shell's PATH — open a new shell, or add ~/.local/bin to PATH"
else
  MISSING=1
  add "MISSING  uv — /uat runs all of its scripts as 'uv run'. Fix: /somnio-engineering-ai:install"
fi

# --- somnio-ai-tools rules source (read-only pinned content; ADR-0003) ---------
# Read from install.sh like the other pins. Absence is MISSING for harness-init
# alone and never flips this doctor's overall verdict (ADR-0003 D-4).
_RS_PIN="$(_pin_from_install 'SOMNIO_AI_TOOLS_COMMIT=')"
_RS_ROOT=""; _RS_TIER=""
if [ -n "${SOMNIO_AI_TOOLS_ROOT:-}" ] && [ -d "${SOMNIO_AI_TOOLS_ROOT:-}/agent-rules/rules" ]; then
  _RS_ROOT="${SOMNIO_AI_TOOLS_ROOT:-}"; _RS_TIER="SOMNIO_AI_TOOLS_ROOT"
elif [ -n "$_RS_PIN" ] && [ -d "$HOME/.somnio/somnio-ai-tools-$_RS_PIN/agent-rules/rules" ]; then
  _RS_ROOT="$HOME/.somnio/somnio-ai-tools-$_RS_PIN"; _RS_TIER="owned clone"
elif [ -n "$_RS_PIN" ] && [ -d "$HOME/.pub-cache/git/somnio-ai-tools-$_RS_PIN/agent-rules/rules" ]; then
  _RS_ROOT="$HOME/.pub-cache/git/somnio-ai-tools-$_RS_PIN"; _RS_TIER="pub-cache (fallback)"
fi
if [ -z "$_RS_PIN" ]; then
  add "warn rules source — no SOMNIO_AI_TOOLS_COMMIT pin found in install.sh"
elif [ -n "$_RS_ROOT" ]; then
  _RS_N="$(find "$_RS_ROOT/agent-rules/rules" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
  _RS_REV="$(git -C "$_RS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  add "ok  rules source ($_RS_TIER) rev $_RS_REV, $_RS_N stacks — $_RS_ROOT"
else
  add "MISSING  rules source (harness-init only) — somnio-ai-tools at $(printf '%s' "$_RS_PIN" | cut -c1-7) not found. Fix: /somnio-engineering-ai:install"
fi

# --- somnio CLI binary (harness-init's skills step only) -----------------------
# The pin is read from install.sh, never copied here — same reason as the corpus
# pin above: a bump PR touches one file. Drift is warn, never MISSING for the
# overall verdict; absence is MISSING scoped to harness-init's skills step, the
# way the rules source is scoped to harness-init.
_CLI_PIN="$(_pin_from_install 'SOMNIO_CLI_VERSION=')"
_CLI_COMMIT="$(_pin_from_install 'SOMNIO_CLI_COMMIT=')"
_CLI_FIX="dart pub global activate -sgit https://github.com/somnio-software/somnio-ai-tools.git --git-path cli --git-ref $_CLI_COMMIT"
if command -v somnio >/dev/null 2>&1; then
  _CLI_V="$(somnio --version 2>/dev/null | head -1 | sed -E 's/^somnio v?//' | tr -d '[:space:]')"
  if [ -z "$_CLI_PIN" ]; then
    add "warn somnio CLI ${_CLI_V:-(version unknown)} — no SOMNIO_CLI_VERSION pin found in install.sh"
  elif [ -n "$_CLI_V" ] && [ "$_CLI_V" = "$_CLI_PIN" ]; then
    add "ok  somnio CLI $_CLI_V"
  else
    add "warn somnio CLI ${_CLI_V:-(version unknown)} (pin $_CLI_PIN) — harness-init warns and continues on drift"
  fi
else
  add "MISSING  somnio CLI (harness-init skills step only) — rules still install without it. Fix: $_CLI_FIX"
fi

# --- python3 (checked, never installed) ----------------------------------------
# Deliberately not installed by the harness: a system interpreter is heavyweight,
# OS-specific and needs sudo on Linux. It is still load-bearing here — ship-gate.sh
# exits 0 when python3 is absent, so the ship gate silently disables ITSELF. A
# gate that quietly stops gating is exactly what a doctor exists to surface.
if command -v python3 >/dev/null 2>&1; then
  # Version only in the full report: --quiet discards ok lines, and this runs on
  # every SessionStart — no reason to pay an interpreter cold start to build a
  # string nobody reads. The MISSING verdict never depends on the version.
  if [ "$QUIET" -eq 0 ]; then
    add "ok  python3 $(python3 --version 2>&1 | awk '{print $2}')"
  else
    add "ok  python3"
  fi
else
  MISSING=1
  MISSING_NOT_INSTALLABLE=1   # the harness deliberately does not install python3
  add "MISSING  python3 — the ship gate silently disables itself without it, and project-init refuses to run. Fix: macOS 'xcode-select --install' · Debian/Ubuntu 'sudo apt install python3'"
fi

# --- gstack skill suite --------------------------------------------------------
GSTACK_DIR="$HOME/.claude/skills/gstack"
# Marker owned by upstream ruflo: the header of the block 'ruflo init' writes
# into ~/.claude/CLAUDE.md. If a ruflo upgrade rewords it, update it here, in
# install.sh, and in tests/e2e-harness/container-test.sh together.
RUFLO_MARKER="Ruflo Integration"
# VERSION alone can survive a failed setup; require the config binary too — and
# grade the VERSION string, because a stubbed install (the E2E fixture writes
# exactly VERSION + bin/gstack-config) satisfies mere existence and would be
# reported healthy while every gstack skill fails at runtime.
GSTACK_VALID=0
if [ -f "$GSTACK_DIR/VERSION" ] && [ -x "$GSTACK_DIR/bin/gstack-config" ]; then
  GSTACK_V="$(cat "$GSTACK_DIR/VERSION" 2>/dev/null)"
  case "$GSTACK_V" in
    ''|*stub*|0.0.0*)
      MISSING=1
      add "MISSING  gstack skill suite — install at ~/.claude/skills/gstack looks stubbed (VERSION='$GSTACK_V'). Fix: rm -rf ~/.claude/skills/gstack && /somnio-engineering-ai:install"
      ;;
    *)
      GSTACK_VALID=1
      add "ok  gstack v$GSTACK_V at ~/.claude/skills/gstack"
      ;;
  esac
elif [ -f "$GSTACK_DIR/VERSION" ]; then
  MISSING=1
  add "MISSING  gstack skill suite — install at ~/.claude/skills/gstack looks incomplete (VERSION present, bin/gstack-config missing)"
else
  MISSING=1
  add "MISSING  gstack skill suite — expected at ~/.claude/skills/gstack"
fi

# --- gstack telemetry (Somnio policy: off) -------------------------------------
# Gated on GSTACK_VALID: a stub answers 'off' from its own default and would
# fake compliance with business rule 14 while the real ~/.gstack/config.yaml
# goes unread. An unverified install gets no policy verdict at all.
if [ "$GSTACK_VALID" -eq 1 ]; then
  TELEMETRY="$("$GSTACK_DIR/bin/gstack-config" get telemetry 2>/dev/null || echo unknown)"
  if [ "$TELEMETRY" = "off" ]; then
    add "ok  gstack telemetry off (Somnio policy)"
  else
    MISSING=1
    add "POLICY  gstack telemetry is '$TELEMETRY' — Somnio policy is 'off'. Fix: $GSTACK_DIR/bin/gstack-config set telemetry off"
  fi
fi

# --- ruflo global pointer block ------------------------------------------------
if grep -q "$RUFLO_MARKER" "$HOME/.claude/CLAUDE.md" 2>/dev/null; then
  add "ok  ruflo integration block in ~/.claude/CLAUDE.md"
else
  MISSING=1
  add "MISSING  ruflo integration block in ~/.claude/CLAUDE.md (installed by 'ruflo init')"
fi

# --- runtime hygiene (any repo): ruflo state must be gitignored ----------------
# The plugin is user-scoped, so the MCP server can create runtime dirs in any
# repo — but only this repo ships the .gitignore guardrails. Warn wherever the
# state exists unguarded, before it lands in someone's commit.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  for d in .claude-flow .swarm .hive-mind agentdb.rvf agentdb.rvf.lock; do
    if [ -e "$d" ] && ! git check-ignore -q "$d" 2>/dev/null; then
      MISSING=1
      add "HYGIENE  $d exists but is not gitignored — ruflo runtime state could be committed. Add it to this repo's .gitignore."
    fi
  done
fi

# --- project-scoped ruflo runtime (optional; full report only) -----------------
if [ "$QUIET" -eq 0 ]; then
  if [ -f .swarm/memory.db ] || [ -d .claude-flow/data ]; then
    add "ok  project ruflo runtime initialized (.swarm / .claude-flow present)"
  else
    add "info  project ruflo runtime not initialized (optional) — run /somnio-engineering-ai:install and accept the project step"
  fi
fi

# --- target-repo settings: secret deny list + OS-enforced sandbox --------------
# A plugin cannot ship permissions, so a harnessed repo must commit its own
# .claude/settings.json. Detect a repo that is missing it, or has the deny list
# but not the sandbox block that makes that deny OS-enforced and stops the
# grep-after-cd prompt. apply-settings.sh writes it; never sets MISSING because
# a repo may deliberately manage its own permissions. Under the sandbox the
# doctor also grades excludedCommands (gh, gcloud, terraform: proxy-unaware CLIs
# that must run outside it) and the two WebFetch(domain:github.com|api.github.com)
# seeds — info-level, never MISSING. It also counts surviving Write(...) rules
# in allow/deny/ask: Claude Code >= 2.1.261 only matches Edit(path) rules for
# file-editing tools, so each Write(...) entry is dead and warns at session
# start; merge-settings.py rewrites them, so the fix is re-running
# apply-settings.sh. Info-level, never MISSING.
#
# Three network lines join them, all info-level and all silent when there is
# nothing to say: (a) allowedDomains is only a no-prompt list until
# sandbox.network.strictAllowlist is true, so a sandbox without it is named
# together with that one setting; (b) .claude/harness.json's `network` block is
# what harness-init detected from the repo's dependencies, so any domain or
# excluded command recorded there but absent from settings.json is a
# recorded-vs-actual gap the settings step would close; (c) each gap the
# detector recorded (Node fetch below 24, a raw database socket, ...) is
# repeated with its severity, because no settings file can close those.
if [ "$QUIET" -eq 0 ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
  S="$ROOT/.claude/settings.json"
  if [ ! -f "$S" ]; then
    add "info  no .claude/settings.json — run /somnio-engineering-ai:install and accept the settings step to add the secret deny list + sandbox (apply-settings.sh)"
  elif command -v python3 >/dev/null 2>&1; then
    STATE="$(python3 - "$S" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("unreadable"); sys.exit()
deny = (d.get("permissions") or {}).get("deny") or []
has_deny = any(".env" in str(r) for r in deny)
# Only the JSON boolean true counts: the string "false" is truthy in Python.
has_sbox = (d.get("sandbox") or {}).get("enabled") is True
excl = (d.get("sandbox") or {}).get("excludedCommands") or []
# Prefix rule: "gh", "gh *", "gh*" and "gh pr *" cover gh; "ghc *" must not.
def covers(e, c):
    # RAW spelling only, deliberately. This line grades whether a TOP-LEVEL
    # `gh pr create` can run outside the sandbox, and only `gh *` buys that
    # (ADR-0004 D-2/D-3b). A brief 2026-09-22 version stripped a
    # `somnio-broker-` prefix here so the broker entry counted as coverage;
    # that made a repo carrying ONLY broker spellings print
    # "ok ... excludes gh, gcloud, terraform" while every top-level gh command
    # was still sandboxed and the fixed-argv broker could not carry it. That is
    # the same false-green shape this release exists to remove, pointed the
    # other way. The broker half is graded by the decoration check below.
    e = str(e)
    return e == c or (e.startswith(c) and e[len(c):len(c) + 1] in (" ", "*"))
cov = [c for c in ("gh", "gcloud", "terraform") if any(covers(e, c) for e in excl)]
allow = (d.get("permissions") or {}).get("allow") or []
seeds = [h for h in ("github.com", "api.github.com") if "WebFetch(domain:%s)" % h in allow]
perms = d.get("permissions") or {}
dead = sum(1 for k in ("allow", "deny", "ask") for r in (perms.get(k) or [])
           if isinstance(r, str) and r.startswith("Write("))
state = "ok" if has_deny and has_sbox else "deny-no-sandbox" if has_deny else "no-deny"
net = (d.get("sandbox") or {}).get("network") or {}
strict = 1 if net.get("strictAllowlist") is True else 0
# The recorded network policy: harness-init writes it from the dependencies
# of the repo. Missing or malformed is not an error here, just nothing to say.
def _clean(v):
    return "".join(c if (c.isalnum() or c in "-._:/*") else "_" for c in str(v))[:60] or "-"
missing, gaps = [], []
try:
    import os
    hp = os.path.join(os.path.dirname(sys.argv[1]), "harness.json")
    hn = json.load(open(hp))
    rec = hn.get("network") if isinstance(hn, dict) else None
    if isinstance(rec, dict):
        domains = net.get("allowedDomains") or []
        for dom in (rec.get("domains") or [])[:20]:
            if isinstance(dom, str) and dom not in domains:
                missing.append(_clean(dom))
        for ec in (rec.get("excluded_commands") or [])[:20]:
            if not isinstance(ec, str):
                continue
            base = ec.rstrip("*").strip()
            if not any(e == ec or covers(e, base) for e in excl):
                # Report the command, not its glob: the list is read by a person.
                missing.append(_clean(base or ec))
        for g in (rec.get("gaps") or [])[:20]:
            if isinstance(g, dict) and g.get("id"):
                gaps.append(_clean(g["id"]) + ":" + _clean(g.get("severity") or "-"))
            elif isinstance(g, str) and g:
                gaps.append(_clean(g) + ":-")
except Exception:
    pass
print(state, ",".join(cov) or "-", len(seeds), 1 if has_sbox else 0, ",".join(seeds) or "-", dead,
      strict, ",".join(missing) or "-", ",".join(gaps) or "-")
PY
)"
    # One line: state, covered CLIs (comma list or '-'), WebFetch seed count,
    # sandbox flag, seeded hosts (comma list or '-'), dead Write(...) rule count,
    # strictAllowlist flag, recorded-but-absent network items (comma list or
    # '-'), recorded gaps as id:severity (comma list or '-'). The last three are
    # absent on the "unreadable" line and default to nothing to report.
    set -- $STATE; STATE="${1:-}"; EXCL="${2:-}"; WF="${3:-0}"; SBOX="${4:-0}"; SEEDS="${5:-}"; DEAD="${6:-0}"
    STRICT="${7:-1}"; NETMISS="${8:--}"; NETGAPS="${9:--}"
    case "$STATE" in
      ok) add "ok  target-repo settings present (secret deny list + OS-enforced sandbox)" ;;
      deny-no-sandbox) add "info  .claude/settings.json has the secret deny list but no sandbox block — grep-after-cd will still prompt; add it with apply-settings.sh (via /somnio-engineering-ai:install)" ;;
      no-deny) add "info  .claude/settings.json has no secret deny list — add the harness settings with apply-settings.sh (via /somnio-engineering-ai:install)" ;;
      unreadable|"") add "info  .claude/settings.json could not be parsed — the sandbox/deny checks were skipped" ;;
    esac
    if [ "$DEAD" -gt 0 ] 2>/dev/null; then
      add "info  $DEAD Write(...) permission rule(s) are dead on Claude Code >= 2.1.261 and warn at session start — re-run scripts/apply-settings.sh (merge-settings rewrites them to Edit(...))"
    fi
    if [ "$SBOX" = "1" ]; then
      _MISS=""
      for _c in gh gcloud terraform; do
        case ",$EXCL," in *",$_c,"*) ;; *) _MISS="${_MISS:+$_MISS, }$_c" ;; esac
      done
      if [ -z "$_MISS" ]; then
        add "ok  target-repo sandbox excludes gh, gcloud, terraform (proxy-unaware CLIs run outside the sandbox)"
      else
        case ",$EXCL," in
          *",gh,"*) add "info  sandbox block does not exclude $_MISS (docs/TARGET-REPO-SETUP.md)" ;;
          *) add "info  sandbox block does not exclude $_MISS — shipping steps call gh directly; add \"gh *\" (docs/TARGET-REPO-SETUP.md)" ;;
        esac
      fi
      if [ "$WF" = "2" ]; then
        add "ok  WebFetch(domain:github.com)/api.github.com allow rules present (sandboxed curl/git egress pre-allowed)"
      else
        _H=""
        for _h in github.com api.github.com; do
          case ",$SEEDS," in *",$_h,"*) ;; *) _H="${_H:+$_H, }$_h" ;; esac
        done
        add "info  no WebFetch(domain:$_H) allow rule — the sandboxed PR watch will prompt on first egress; seed it with apply-settings.sh"
      fi
      if [ "$STRICT" != "1" ]; then
        add "info  sandbox.network.strictAllowlist is not set — allowedDomains decides what prompts, not what is reachable, so a host outside the list still resolves; set sandbox.network.strictAllowlist to true to make the list a boundary (docs/TARGET-REPO-SETUP.md)"
      fi
    fi
    if [ "$NETMISS" != "-" ]; then
      add "info  .claude/harness.json records network policy this settings.json does not carry: $(printf '%s' "$NETMISS" | tr ',' ' ') — re-run scripts/apply-settings.sh to seed it"
    fi
    if [ "$NETGAPS" != "-" ]; then
      _OLDIFS="$IFS"; IFS=','
      for _g in $NETGAPS; do
        add "info  recorded network gap: ${_g%%:*} [${_g#*:}] — no settings file closes this one (docs/TARGET-REPO-SETUP.md)"
      done
      IFS="$_OLDIFS"
    fi
  fi
fi

# --- ship gate (per-repo work state; full report only, never sets MISSING) -----
# A legitimately blocked ship is not a broken toolchain: keep it out of
# --quiet SessionStart output and out of the MISSING verdict.
if [ "$QUIET" -eq 0 ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
  if [ -n "$ROOT" ] && [ -f "$ROOT/.claude/ship-gate.json" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SHIP_LINE="$("$SCRIPT_DIR/ship-gate.sh" --status 2>/dev/null | grep '^SHIP:' | head -1)"
    add "info  ship gate: ${SHIP_LINE:-status unavailable} — details: ship-gate.sh --status"
  fi
fi

# --- leftover worktrees (ENG-EXEC-07; full report only, never sets MISSING) ----
# Work state, not a toolchain fault, and the classifier reads GitHub once per
# branch, so it stays out of the SessionStart budget. Silent when the pool is empty.
if [ "$QUIET" -eq 0 ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  _WT_MAIN="$(git worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"
  _WT_SH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/skills/worktree-recycle/scripts/worktree-recycle.sh"
  if [ -n "$_WT_MAIN" ] && [ -d "$_WT_MAIN/.claude/worktrees" ] && [ -f "$_WT_SH" ] \
     && git worktree list --porcelain 2>/dev/null | grep -q "^worktree $_WT_MAIN/.claude/worktrees/"; then
    _WT_LINE="$(sh "$_WT_SH" --summary 2>/dev/null | head -1)"
    add "info  ${_WT_LINE:-worktrees: summary unavailable} — review with /somnio-engineering-ai:worktree-recycle"
  fi
fi

# --- autonomy gates (full report only; informational) ---------------------------
# The record is .claude/harness.json `execution.gates` (harness-init Step 6c). A
# .loop/config.yaml is one team's own file, read where it exists and never expected:
# its absence is not a gap, so the line never tells anyone to create it.
if [ "$QUIET" -eq 0 ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
  _EXEC_GATE=""
  if [ -n "$ROOT" ] && [ -f "$ROOT/.claude/harness.json" ] && command -v python3 >/dev/null 2>&1; then
    _EXEC_GATE="$(python3 -c 'import json,sys
g=(json.load(open(sys.argv[1])).get("execution") or {}).get("gates") or {}
print(((g.get("adr_rule_violation_gate") or {}).get("on_violation") or "set") if g else "")' \
      "$ROOT/.claude/harness.json" 2>/dev/null || true)"
  fi
  if [ -n "$_EXEC_GATE" ]; then
    if [ "$_EXEC_GATE" = abort ]; then
      add "ok  autonomy gates recorded in .claude/harness.json (adr_rule_violation_gate: abort — 'no ADR, no development' enforced)"
    else
      add "info  autonomy gates recorded in .claude/harness.json but adr_rule_violation_gate is not 'abort' — unattended runs may proceed past ADR violations (docs/LOOP-GATES.md)"
    fi
  elif [ -n "$ROOT" ] && [ -f "$ROOT/.loop/config.yaml" ]; then
    # Match the gate's OWN value only: the inline form on the key's line, or the
    # on_violation line inside its block. A loose -A3 grep would false-OK when a
    # neighboring gate's value (or a comment) says 'abort'.
    if awk '/adr_rule_violation_gate:/{f=1; if (/abort/) {found=1}; next} f&&/on_violation:/{if (/abort/) found=1; exit} f&&/^[[:space:]]*[a-z_]+_gate:/{exit} END{exit !found}' "$ROOT/.loop/config.yaml" 2>/dev/null; then
      add "ok  .loop autonomy gates present (adr_rule_violation_gate: abort — 'no ADR, no development' enforced)"
    else
      add "info  .loop autonomy gates present but adr_rule_violation_gate is not 'abort' — unattended runs may proceed past ADR violations (docs/LOOP-GATES.md)"
    fi
  else
    add "info  no autonomy gates recorded — /dev-execution and /loop-mode ask at each escalation; nothing to add unless the team wants unattended runs (harness-init Step 6c)"
  fi
fi

# --- MCP surface (full report only) --------------------------------------------
# ADR-0002 D-2: the hazard prints where the operator can see it. Liveness is a
# real stdio handshake against the pinned server — configuration alone is not
# a probe ("a probe that could not run is not a probe that succeeded").
if [ "$QUIET" -eq 0 ]; then
  add "info  MCP: plugin attaches the pinned ruflo server — ⚠ exposes 400+ tools incl. terminal_execute, http_fetch, system_reset; undo by disabling the plugin"
  # Stock macOS ships no GNU timeout; without one the probe cannot run —
  # report that honestly instead of a false WARN against a healthy server.
  _TO="$(command -v timeout || command -v gtimeout || true)"
  # Probe the exact server the plugin attaches: the pin comes from .mcp.json,
  # never a second hardcoded copy that could drift on a bump PR.
  _PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  _PIN="$(grep -o '@claude-flow/cli@[0-9][0-9A-Za-z.+-]*' "$_PLUGIN_DIR/.mcp.json" 2>/dev/null | head -1)"
  # Prefer the INSTALLED CLI over npx. npx must write ~/.npm/_cacache, which the
  # sandbox denies (it allows ~/.npm/_logs only), and npm reports that EPERM as
  # "Your cache folder contains root-owned files... run sudo chown -R". That
  # message is wrong here and acting on it means a needless recursive sudo chown
  # of the npm cache. The installed CLI answers in ~80ms and writes no cache.
  # Business rule 15: everything executed from outside this repo is PINNED.
  # The installed CLI is preferred only when its version MATCHES the pin the
  # plugin actually attaches; otherwise probing it would certify a different
  # server than sessions receive, hiding pin-specific startup failures in the
  # one users get. Version mismatch falls back to npx on the pin.
  _MCP_CMD=""
  _PIN_VER="${_PIN#@claude-flow/cli@}"
  # Matched on the ENGINE the installed wrapper runs, since that is what .mcp.json
  # attaches; the wrapper's own version says nothing about it.
  if command -v ruflo >/dev/null 2>&1 \
     && [ -n "$_PIN_VER" ] \
     && [ "$(ruflo_engine_version 2>/dev/null)" = "$_PIN_VER" ]; then
    _MCP_CMD="ruflo"
  elif command -v npx >/dev/null 2>&1 && [ -n "$_PIN" ]; then
    _MCP_CMD="npx -y $_PIN"
  fi
  if [ -n "$_MCP_CMD" ] && [ -n "$_TO" ]; then
    # stderr is MERGED rather than written to a temp file: mktemp is not
    # guaranteed on a reduced PATH, and the earlier temp-file version silently
    # lost denial detection whenever mktemp was absent - the same could-not-run
    # confusion one layer down.
    _HS="$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"doctor","version":"1"}}}\n' \
      | "$_TO" 8 $_MCP_CMD mcp start 2>&1 | head -c 400)"
    # A probe that could not RUN is not a probe that failed. Never let a denial
    # become a WARN about the server: that is the noise that teaches operators
    # to stop reading the report.
    _MCP_DENIED=""
    case "$_HS" in
      *EPERM*|*EACCES*|*"operation not permitted"*|*"Operation not permitted"*|\
      *"permission denied"*|*"Permission denied"*|*cacache*) _MCP_DENIED=1 ;;
    esac
    # Order matters: a real handshake wins, then COULD-NOT-RUN, and only then a
    # verdict about the server. Denial output is non-empty, so it must be tested
    # before the empty and unexpected branches, not inside them.
    if [ "${_HS#*\"result\"}" != "$_HS" ]; then
      add "ok  ruflo MCP server handshake (live stdio initialize via ${_MCP_CMD%% *})"
    elif [ -n "$_MCP_DENIED" ]; then
      add "info  ruflo MCP handshake could not RUN — the runner was denied (sandbox/permissions), which says nothing about the server. Re-run outside the sandbox to grade it. If npm advised a recursive sudo chown of ~/.npm, that advice does not apply here: the denial is the sandbox, not file ownership"
    elif [ -z "$_HS" ]; then
      add "WARN  ruflo MCP server did not answer the stdio handshake within 8s — sessions may lack MCP tools"
    else
      add "WARN  ruflo MCP server handshake returned unexpected output — verify with a session"
    fi
  elif command -v npx >/dev/null 2>&1; then
    add "info  MCP handshake probe skipped — no timeout/gtimeout binary (brew install coreutils to enable it)"
  fi
  # Floating-tag scan: vendor-derived agents must not shell out to floating tags.
  PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  _FLOAT=$(grep -rEo 'npx (claude-flow|ruflo)@(v3)?alpha' "$PLUGIN_DIR/agents" 2>/dev/null | wc -l | tr -d ' ')
  [ "${_FLOAT:-0}" -gt 0 ] && add "WARN  $_FLOAT agent call site(s) name a floating claude-flow/ruflo tag — behaviour can change under you; pin them"
fi

# --- the ADR-0004 D-3 broker ---------------------------------------------------
# D-3 requires "a doctor check that the exemption did not silently become the
# boundary". That sentence has a precise failure in mind: a repo that excludes
# `gcloud *` DIRECTLY instead of `somnio-broker-gcloud *` gets every gcloud
# command outside the sandbox, and the broker becomes decoration. Detecting that
# is the whole point of this block, so it is checked first.
#
# Full report only, and never MISSING: the broker is a convenience for infra
# probing, and its absence degrades to D-3a's person-confirms path.
if [ "$QUIET" -eq 0 ]; then
  _BK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/broker"
  _BK_ALLOW="$_BK_DIR/allowlist.json"

  if [ -f "$_BK_ALLOW" ] && command -v python3 >/dev/null 2>&1; then
    _BK_CLIS="$(python3 -c 'import json,sys; print(" ".join(sorted({s["cli"] for s in json.load(open(sys.argv[1]))["providers"].values()})))' "$_BK_ALLOW" 2>/dev/null)"
  else
    _BK_CLIS=""
  fi

  if [ -n "$_BK_CLIS" ]; then
    # THE check D-3 names. A raw CLI exclusion in the session repo's settings
    # makes the broker pointless: the exemption became the boundary.
    _BK_SETTINGS=""
    _BK_TOP="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$_BK_TOP" ] && [ -f "$_BK_TOP/.claude/settings.json" ] && _BK_SETTINGS="$_BK_TOP/.claude/settings.json"
    if [ -n "$_BK_SETTINGS" ]; then
      _BK_RAW="$(python3 - "$_BK_SETTINGS" "$_BK_CLIS" <<'RAWCHECK' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
ex = d.get("sandbox", {}).get("excludedCommands", []) or d.get("excludedCommands", []) or []
clis = set(sys.argv[2].split())
heads = {str(e).strip().split()[0] for e in ex if str(e).strip()}
# The D-3 harm is "reduces the broker to decoration", which happens only when
# the RAW name is excluded and the broker entry point is NOT. The two serve
# different callers and are complementary, not alternatives: "gh *" is what
# lets a TOP-LEVEL "gh pr create" run (D-2, D-3a), while "somnio-broker-gh *"
# is what lets a SANDBOXED script probe, since "uv run check_infra_cli.py"
# never matches "gh *". Warning on the raw name alone told every repo to drop
# the exclusion its real work depends on, and the broker cannot substitute:
# fixed argv takes no caller input, so "gh pr create" through it is impossible.
# NOTE: no apostrophes in this heredoc. bash 3.2 (stock macOS) applies quote
# parsing to a QUOTED heredoc body when it is nested in $(...), so a lone
# apostrophe opens a string that never closes, swallows the rest of the file,
# and reports a syntax error ~80 lines later on an unrelated line. Verified by
# bisection 2026-09-22: deleting this comment block made the file parse.
raw = [c for c in clis if c in heads and ("somnio-broker-" + c) not in heads]
print(",".join(sorted(set(raw))))
RAWCHECK
)"
      if [ -n "${_BK_RAW:-}" ]; then
        add "WARN  broker is decoration here — this repo excludes $_BK_RAW but not somnio-broker-<cli>, so sandboxed scripts still cannot probe them; ADD the broker entry point (keep the raw one: it is what lets a top-level gh/gcloud command run at all, ADR-0004 D-2/D-3a)"
      fi
    fi

    # Inventory: which entry points are actually installed.
    # `-x` FOLLOWS the symlink, so it is false for a dangling one; `-L` does
    # not, and testing it FIRST counted a dead entry point as installed. Seen
    # in the field 2026-09-22: 10/10 reported ok while every link pointed into
    # a removed git worktree and `somnio-broker-gh` was command-not-found. A
    # probe that could not run is not a probe that succeeded - the script says
    # so about MCP three blocks up, and then did this.
    # Dangling is reported apart from absent because they have different fixes:
    # absent means never installed, dangling means installed then the target
    # moved, which is the silent one.
    _BK_N=0; _BK_MISS=""; _BK_DEAD=""
    for _c in $_BK_CLIS; do
      _BK_EP="$HOME/.local/bin/somnio-broker-$_c"
      if [ -x "$_BK_EP" ]; then
        _BK_N=$((_BK_N+1))
      elif [ -L "$_BK_EP" ]; then
        _BK_DEAD="$_BK_DEAD $_c"
      else
        _BK_MISS="$_BK_MISS $_c"
      fi
    done
    _BK_TOTAL="$(printf '%s\n' $_BK_CLIS | wc -l | tr -d ' ')"
    if [ -n "$_BK_DEAD" ]; then
      add "WARN  broker entry points are UNUSABLE:$_BK_DEAD — the name exists but does not resolve to an executable (missing target, or present and non-executable), so every call fails and the probes that depend on it fail silently. Fix: /somnio-engineering-ai:install"
    fi
    # `ok` requires ALL of them resolving. A dangling entry point already got a
    # WARN above, and pairing that with an `ok N/total` line is the same false
    # reassurance in a quieter voice.
    if [ "$_BK_N" -eq 0 ] && [ -z "$_BK_DEAD" ]; then
      add "info  broker not installed — infra probes under the sandbox degrade to the person-confirms path (ADR-0004 D-3a). Fix: /somnio-engineering-ai:install"
    elif [ -n "$_BK_MISS" ] || [ -n "$_BK_DEAD" ]; then
      add "info  broker $_BK_N/$_BK_TOTAL entry points resolve${_BK_MISS:+ — missing:$_BK_MISS}${_BK_DEAD:+ — dangling:$_BK_DEAD}"
    else
      add "ok  broker $_BK_N/$_BK_TOTAL entry points (ADR-0004 D-3, fixed argv, read-only probes)"
    fi

    # The hardening is only real while the files stay read-only. This catches
    # accident and drift; it cannot stop the owner, which D-3 says out loud.
    if [ -w "$_BK_DIR/somnio-broker" ] 2>/dev/null; then
      add "WARN  broker implementation is writable — the fixed-argv guarantee holds only while it is not. Fix: chmod 0555 $_BK_DIR/somnio-broker"
    fi
    if [ -n "${SOMNIO_BROKER_PATH_FOR_TESTS:-}" ]; then
      add "WARN  SOMNIO_BROKER_PATH_FOR_TESTS is set in this session — that override exists for the test suite and lets a caller choose which binary the broker runs"
    fi
  fi
fi

# --- roborev — local continuous review (ENG-REV-05, ADR-0005) ------------------
# PRESENCE IS GRADED IN BOTH MODES; DIAGNOSIS IS FULL-REPORT ONLY (ADR-0005 D-14,
# 2026-09-21). This block used to be full-report only in its entirety, and set
# neither MISSING nor MISSING_NOT_INSTALLABLE, so a machine with no roborev at all
# was byte-identical at SessionStart to a machine with a healthy one. ENG-REV-05's
# whole premise is that every commit is reviewed before a PR exists; a battery
# whose total absence the harness declines to mention is one nobody installs.
#
# The split that replaces it:
#   * resolving the binary is TWO cheap tests (-x and command -v, no subprocess),
#     so it runs in both modes, and its absence sets MISSING and offers the fix.
#   * everything that INTERROGATES roborev — version, daemon socket, web listener,
#     skills inventory, Layer 2, [[hooks]], cooldown — costs subprocesses and stays
#     behind the quiet guard, matching the convention stated at the uv step above.
#
# What this does NOT change, stated because the distinction is the whole argument:
# grading whether the battery is INSTALLED is not grading a review. D-9's "a
# missing review is not a clean review" and D-12.1's "no gate consumes a verdict"
# are untouched, and so is D-1 — the doctor offers the INSTALL, never a
# registration, because registering a repository sends its diffs to a provider and
# stays a person's act on the record.
#
# MISSING_NOT_INSTALLABLE stays 0: /somnio-engineering-ai:install owns roborev, so
# the banner naming that command is true rather than a dead end.
#
# Default from install-roborev.sh's ROBOREV_INSTALL_DIR. Read as an env var with
# the same default rather than grepped out of that file: the assignment there is
# a ${VAR:-default} shape with no quoted literal, so _pin_from_install's sed
# cannot extract it.
_RB_DIR="${ROBOREV_INSTALL_DIR:-$HOME/.local/bin}"

# ONE resolved binary for every probe that needs one. Without this, a
# user-managed install (state 3) would still be probed at the harness path,
# every probe would fail, and "user-managed" would come with a spurious warn.
_RB_BIN=""; _RB_OURS=0; _RB_OK=0
if [ -x "$_RB_DIR/roborev" ]; then _RB_BIN="$_RB_DIR/roborev"; _RB_OURS=1
elif command -v roborev >/dev/null 2>&1; then _RB_BIN="$(command -v roborev)"
fi

# The graded line, in both modes. Phrased as an offer because the doctor installs
# nothing (see this file's header) and ENG-HARNESS-02 keeps the install a person's
# explicit act: it names what is lost and the one command that fixes it.
if [ -z "$_RB_BIN" ]; then
  MISSING=1
  add "MISSING  roborev — commit-time review (ENG-REV-05) is unavailable, so defects written now surface a whole pipeline later, at PR review. Install it? Run /somnio-engineering-ai:install   (installs the pinned binary only; no repository is registered and nothing is reviewed until you opt one in)"
fi

if [ "$QUIET" -eq 0 ]; then
  _RB_PIN="$(_pin_from_install 'ROBOREV_VERSION=')"

  if [ -n "$_RB_BIN" ]; then
    # Version carries a leading "v" (`version --json` -> "v0.67.0"); the pin is
    # bare. Strip once, here, so the mismatch line never reads "v0.68.0 — pinned
    # 0.68.0". `version --json` needs no repository and no running daemon.
    # Same extraction as install-roborev.sh:installed_version() — deliberately, so
    # a future change to the payload shape has one shape to find, not two. Neither
    # form is a JSON parser: this one takes the FIRST "version" key, a greedy sed
    # would take the LAST, and which is right depends on where a nested
    # {"build":{"version":…}} sits. Flat at 0.67.0, so the choice is consistency.
    _RB_VER="$("$_RB_BIN" version --json 2>/dev/null \
      | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
      | sed -E 's/.*"v?([^"]+)"$/\1/')"
    if [ "$_RB_OURS" -eq 0 ]; then
      add "INFO  roborev $_RB_BIN — user-managed, not graded by this harness"
    elif [ -z "$_RB_PIN" ]; then
      # Same shape as the uv step's unreadable-pin line: say the comparison did
      # not happen rather than staying silent, which would make a healthy install
      # indistinguishable from a broken one.
      add "info  could not read the roborev pin from $_INSTALL_SH — version comparison skipped"
    elif [ -z "$_RB_VER" ]; then
      add "WARN  roborev at $_RB_BIN did not report a version — pinned $_RB_PIN"
    elif [ "$_RB_VER" != "$_RB_PIN" ]; then
      add "WARN  roborev $_RB_VER — pinned $_RB_PIN; see the pin-bump checklist in docs/DEV_HARNESS.md"
    else
      # The one healthy case. Recorded HERE, where the chain decides it, rather
      # than restating the predicate beside the OK line seventy lines below: a
      # fifth arm added to this chain would otherwise leave that copy silently
      # printing OK.
      _RB_OK=1
    fi

    # --- daemon reachability, WITHOUT starting one -----------------------------
  # `roborev daemon status` has no --no-start flag and STARTS a daemon when none
  # is running: probed against both a missing and an unreadable socket, it
  # returned running:true with a populated web_url, and left a `roborev daemon
  # run` process behind. Three consequences: this script promises at the top that
  # it only reads state; a daemon the doctor starts on a config with no [web]
  # block comes up with the web listener ENABLED, i.e. the doctor would create
  # the very condition the web warning exists to report; and "not running" is
  # unobservable that way, because the command has just made it run.
  #
  # So reachability is decided by connecting to the socket ourselves, and
  # `daemon status --json` runs ONLY once that connect has succeeded — an
  # already-running daemon cannot be autostarted, so the call is safe there and
  # it is the only source of web_url.
    _RB_ADDR="$(_quoted_value "$HOME/.roborev/config.toml" '^[[:space:]]*server_addr[[:space:]]*=')"
    _RB_SOCK=""; _RB_KIND=unix; _RB_DERIVED=0
    case "$_RB_ADDR" in
      unix://?*) _RB_SOCK="${_RB_ADDR#unix://}" ;;
      # `unix://` with no path is what our own installer writes, so this is the
      # normal case on every harness machine, not an edge case: it means "the
      # default socket", not "the empty path".
      unix://|"") _RB_SOCK="" ;;
      # host:port (or :port) is a TCP address; anything else is unparseable and
      # must not be reported as TCP.
      *:[0-9]*) _RB_KIND=tcp ;;
      *) _RB_KIND=junk ;;
    esac
    if [ -z "$_RB_SOCK" ] && [ "$_RB_KIND" = unix ]; then
      # ${TMPDIR:-/tmp}: TMPDIR is routinely unset on Linux, where CI runs, and a
      # bare expansion under `set -u` would abort this whole script, not just this
      # block. macOS sets it with a trailing slash, hence the %/ strip.
      _RB_TMP="${TMPDIR:-/tmp}"; _RB_TMP="${_RB_TMP%/}"
      _RB_SOCK="$_RB_TMP/roborev-$(id -u)/daemon.sock"
      # Inferred from upstream's observed default, not read from config. Only an
      # inferred path can disagree with the daemon's own, so only an inferred
      # path earns the hedged wording below.
      _RB_DERIVED=1
    fi

    if [ "$_RB_KIND" = junk ]; then
      add "WARN  roborev server_addr in ~/.roborev/config.toml is not a unix:// path or host:port — reachability not graded"
    elif [ "$_RB_KIND" = tcp ]; then
      add "info  roborev daemon address $_RB_ADDR is TCP — reachability not graded by this harness"
    elif ! command -v python3 >/dev/null 2>&1; then
      # python3 is optional on this machine (graded above, never installed), and
      # bash cannot open a unix socket — /dev/tcp is TCP-only.
      add "info  roborev daemon reachability not probed — python3 is absent and bash cannot open a unix socket"
    else
      _RB_STATE="$(python3 - "$_RB_SOCK" <<'PY' 2>/dev/null || echo error
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2)
try:
    s.connect(sys.argv[1]); print("reachable"); s.close()
except FileNotFoundError:      print("no-socket")
except PermissionError:        print("denied")
except ConnectionRefusedError: print("refused")
except Exception:              print("error")
PY
)"
      case "$_RB_STATE" in
        reachable)
          # "(unix)" is a constant on this path: we are here BECAUSE a unix socket
          # connect succeeded. It is not re-derived from the daemon's own report.
          # _RB_OK was decided by the version chain above — a user-managed binary
          # or a version mismatch already produced its own line there, and grading
          # it again here would contradict it.
          [ "${_RB_OK:-0}" -eq 1 ] && add "OK    roborev $_RB_VER — daemon reachable (unix)"
          # --server PINS the query to the socket we just probed. Without it the
          # command resolves its own daemon from $HOME, which can be a DIFFERENT
          # daemon than the socket path we derived from $TMPDIR — and when they
          # diverge, "reachable" is not evidence that the call will not autostart.
          # Observed: a run with a temp HOME probed the real socket, reported
          # reachable, and then started a second daemon under that temp HOME.
          _RB_WEB="$("$_RB_BIN" --server "unix://$_RB_SOCK" daemon status --json 2>/dev/null \
            | grep -o '"web_url"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
            | sed -E 's/.*"([^"]*)"$/\1/')"
          # Non-empty web_url ONLY. The reason field can be empty while the url is
          # also empty, which would print a dangling "enabled at ".
          [ -n "$_RB_WEB" ] && add "WARN  roborev web listener enabled at $_RB_WEB — serves job logs and patches; set web.enabled = false"
          ;;
        denied)
          # The daemon is up; this session cannot reach it. Deliberately never says
          # "stopped": telling a developer to restart a healthy daemon is the exact
          # misdiagnosis this state exists to prevent.
          add "WARN  roborev daemon running, not reachable from this session — socket $_RB_SOCK" ;;
        no-socket|refused)
          # The path may have been DERIVED from this session's $TMPDIR, and the
          # supervised daemon does not inherit it: launchd and the systemd user
          # unit both start `roborev daemon run` with only PATH and the telemetry
          # variable set, so the daemon's default socket can sit under a different
          # $TMPDIR than the doctor's. Asserting "not running" from that would be
          # the same misdiagnosis the denied branch exists to avoid, in reverse.
          # So cross-check the supervisor first — both probes are read-only and
          # start nothing.
          _RB_UNIT=""
          if command -v launchctl >/dev/null 2>&1 \
             && launchctl print "gui/$(id -u)/io.somnio.roborev-daemon" >/dev/null 2>&1; then
            _RB_UNIT="io.somnio.roborev-daemon"
          elif command -v systemctl >/dev/null 2>&1 \
             && systemctl --user is-active somnio-roborev >/dev/null 2>&1; then
            _RB_UNIT="somnio-roborev"
          fi
          if [ -n "$_RB_UNIT" ] && [ "$_RB_DERIVED" -eq 1 ]; then
            add "WARN  roborev daemon unit $_RB_UNIT is loaded but nothing is listening at $_RB_SOCK — that path was derived from this session's \$TMPDIR, which a supervised daemon does not inherit; set server_addr to an explicit unix:// path to make both agree"
          else
            add "WARN  roborev daemon not running — commits are not being reviewed; the supervised unit is io.somnio.roborev-daemon (ADR-0005 D-11)"
          fi ;;
        *)
          add "WARN  roborev daemon status could not be read — socket $_RB_SOCK" ;;
      esac
    fi

    # D-4: inventory only, never graded. Counted from the directories the harness
    # docs name as the artifact, because `roborev skills` has no --json and prints
    # prose. Silent at zero rather than printing a count of none.
    _RB_SK=$(ls -d "$HOME"/.claude/skills/roborev-* 2>/dev/null | wc -l | tr -d ' ')
    # Count and path only. The earlier draft printed "pin <pin>" beside a count
    # derived purely from directory existence — a claim about currency that
    # nothing here checks, and exactly the half-applied-bump blindness ADR-0005
    # D-8 wants visible. D-4 says these assets are inventoried, never graded.
    [ "$_RB_SK" -gt 0 ] && add "info  roborev skills $_RB_SK installed at ~/.claude/skills (vendor assets, never graded — currency not checked)"
  fi
  # No `else` here any more: the absent case is graded above, in both modes, so
  # repeating it inside the quiet guard would print the same MISSING line twice
  # on a full report.

  # --- reported even with no binary --------------------------------------------
  # A leftover Stop hook, a repo [[hooks]] block or a stale config outlives an
  # uninstall, and that is exactly when a developer needs to be told.
  #
  # D-3: the removal is a key deletion. There is no `agent-hook uninstall`
  # subcommand at the pin (dump/install/reset/run/status), so naming one would
  # print a command that fails.
  # A substring match is wrong here: a settings file whose only mention of
  # roborev is a deny rule like Read(~/.roborev/**) would be reported as an
  # installed Stop hook, telling the developer to delete keys that are not one.
  # The claim is specifically "a hook entry that runs roborev", so look inside
  # the hooks object. python3 is optional on this machine, so when it is absent
  # the line is skipped rather than guessed.
  _RB_SETTINGS="$HOME/.claude/settings.json"
  if [ -f "$_RB_SETTINGS" ] && command -v python3 >/dev/null 2>&1; then
    if python3 - "$_RB_SETTINGS" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if "roborev" in json.dumps(d.get("hooks", {})) else 1)
PY
    then
      add "info  roborev Stop hook installed at user scope — remove by deleting the roborev keys from ~/.claude/settings.json (ADR-0005 D-3)"
    else
      # THE SILENT HALF. Reporting the hook only when it is present makes its
      # absence invisible, and absence is the failure: Layer 1 keeps reviewing
      # every commit while the findings reach nobody. Measured on this machine
      # 2026-09-18 — nine days, 17 reviews, 12 of them finding something, all
      # of it sitting in reviews.db unread. ADR-0005 adopted Layers 1 AND 2;
      # a machine running only Layer 1 is half-installed, not configured.
      # Not MISSING: this is a user-scope opt-in a person may decline on purpose
      # (D-3), so the doctor names the cost and the fix rather than failing.
      add "WARN  roborev Layer 2 is NOT installed — reviews run on every commit and their findings reach no session (they stay in ~/.roborev/reviews.db). ENG-REV-05 exists to feed them into the session before a PR. Fix: roborev agent-hook install --agent claude   (NEVER bare: it writes nine agent configs)"
    fi
  elif [ ! -f "$_RB_SETTINGS" ]; then
    add "WARN  roborev Layer 2 is NOT installed (~/.claude/settings.json absent) — reviews would reach no session. Fix: roborev agent-hook install --agent claude"
  fi

  # D-6: reported, never removed, and scoped to THIS repository. The doctor never
  # scans the filesystem for registered repos and never reads one it is not in.
  _RB_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$_RB_ROOT" ] && [ -f "$_RB_ROOT/.roborev.toml" ] \
     && grep -qE '^[[:space:]]*\[\[hooks\]\]' "$_RB_ROOT/.roborev.toml" 2>/dev/null; then
    # Parsed per block, not with `grep -A4` + two independent seds: that spliced
    # the event of one block onto the command of another whenever the first block
    # omitted a field, reporting a pairing that exists in no block at all. It also
    # silently missed fields more than four lines below the header.
    # Only the FIRST block is described, and the line says how many there are.
    _RB_HK="$(awk '
      /^[[:space:]]*\[\[hooks\]\]/ { n++; inhk=1; next }
      /^[[:space:]]*\[/ { inhk=0 }
      inhk && n==1 && ev==""  && /^[[:space:]]*event[[:space:]]*=/   { sub(/^[^"]*"/,""); sub(/".*$/,""); ev=$0;  next }
      inhk && n==1 && cmd=="" && /^[[:space:]]*command[[:space:]]*=/ { sub(/^[^"]*"/,""); sub(/".*$/,""); cmd=$0; next }
      END { printf "event %s, command %s%s",
              (ev=="" ? "unnamed" : ev), (cmd=="" ? "unnamed" : cmd),
              (n>1 ? " (first of " n " blocks)" : "") }
    ' "$_RB_ROOT/.roborev.toml" 2>/dev/null)"
    # awk failing to run at all must not render as "… .roborev.toml — ; reported".
    [ -n "$_RB_HK" ] || _RB_HK="unparseable"
    add "WARN  roborev [[hooks]] in $_RB_ROOT/.roborev.toml — $_RB_HK; reported, not removed (ADR-0005 D-6)"
  fi

  # D-9: read from the config, because no cooldown or quota field exists anywhere
  # in `daemon status --json` at this pin. A root-key grep, not a TOML parser.
  _RB_CFG="$HOME/.roborev/config.toml"
  if [ -f "$_RB_CFG" ]; then
    # Quoted values only. sed's fallback is to echo the line unchanged, so a
    # hand-edited `agent_quota_cooldown = 300` would otherwise render as
    # "cooldown (agent_quota_cooldown = 300)".
    # ROOT keys only. A bare `key = value` inside a [section] belongs to that
    # section, not to the root — the rule install-roborev.sh:set_root_key is built
    # around. Matching anywhere let a section-scoped default_backup_agent suppress
    # the very warning the missing root key should raise (verified).
    _RB_ROOTCFG="$(awk '/^[[:space:]]*\[/ { exit } { print }' "$_RB_CFG" 2>/dev/null)"
    _RB_COOL="$(printf '%s\n' "$_RB_ROOTCFG" | grep -E '^[[:space:]]*agent_quota_cooldown[[:space:]]*=[[:space:]]*"' | head -1 | sed -E 's/^[^"]*"([^"]*)".*/\1/')"
    _RB_BACKUP="$(printf '%s\n' "$_RB_ROOTCFG" | grep -E '^[[:space:]]*default_backup_agent[[:space:]]*=[[:space:]]*"' | head -1 | sed -E 's/^[^"]*"([^"]*)".*/\1/')"
    if [ -n "$_RB_COOL" ] && [ -z "$_RB_BACKUP" ]; then
      add "info  roborev no backup agent configured — the daemon-wide quota cooldown ($_RB_COOL) silences reviews for every repository until it expires (ADR-0005 D-9)"
    fi
  fi
fi

# --- known tool collision (full report only) -----------------------------------
# Field-learned: 'somnio update' (the CLI product) deletes gstack's ship skill
# by name. Detect the aftermath and name the fix.
# GSTACK_VALID, not merely "bin/gstack-config is executable": on an install that
# already failed validation the missing ship dir is a symptom, and this WARN
# would name the wrong cause.
if [ "$QUIET" -eq 0 ] && [ "$GSTACK_VALID" -eq 1 ] && [ ! -e "$GSTACK_DIR/ship" ]; then
  add "WARN  gstack is installed but its ship skill is missing (a 'somnio update' collision deletes it). Fix: cd ~/.claude/skills/gstack && ./setup"
fi

# gstack against its pin (full report only). The installer never replaces an
# existing install for being at another commit, so without this line a pin bump
# reaches only fresh machines and an out-of-band upgrade is invisible.
if [ "$QUIET" -eq 0 ] && [ "$GSTACK_VALID" -eq 1 ]; then
  _GS_PIN="$(_pin_from_install 'GSTACK_COMMIT=')"
  _GS_HEAD="$(git -C "$GSTACK_DIR" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$_GS_PIN" ] && [ -n "$_GS_HEAD" ] && [ "$_GS_HEAD" != "$_GS_PIN" ]; then
    add "warn gstack is at ${_GS_HEAD:0:7} (v$GSTACK_V), the harness pin is ${_GS_PIN:0:7}. Move ~/.claude/skills/gstack aside and re-run /somnio-engineering-ai:install to take the pin."
  fi
fi

# Aside (ADR-0009, proposed): gstack drives the Aside real browser and sends
# research to its agent unless GSTACK_SKIP_ASIDE=1. The settings template sets
# it per repo; a repo without it, on a machine with Aside, is uncovered.
if [ "$QUIET" -eq 0 ] && command -v aside >/dev/null 2>&1 && [ "${GSTACK_SKIP_ASIDE:-}" != 1 ]; then
  add "WARN  the Aside browser is installed and GSTACK_SKIP_ASIDE is not 1 in this session — gstack skills here would drive your real browser and send research to Aside's agent (ADR-0009 is only proposed). Fix: run scripts/apply-settings.sh in this repo, or export GSTACK_SKIP_ASIDE=1"
fi

if [ "$QUIET" -eq 1 ] && [ "$MISSING" -eq 0 ]; then
  exit 0
fi

if [ "$MISSING" -eq 1 ]; then
  # Do not promise the installer fixes everything: it deliberately does not own python3,
  # so a banner naming /install as THE fix would send the developer in a circle.
  if [ "$MISSING_NOT_INSTALLABLE" -eq 1 ]; then
    printf "somnio-engineering-ai: developer toolchain incomplete. Run /somnio-engineering-ai:install for what it owns; items marked with their own Fix line are yours to install.\n"
  else
    printf "somnio-engineering-ai: developer toolchain incomplete. Run /somnio-engineering-ai:install to set it up.\n"
  fi
fi
printf "%b" "$REPORT"
exit 0
