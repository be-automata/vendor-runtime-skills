#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The toolchain a repository PINS, the launcher its commands need, and the
verification commands it actually DECLARES.

Read-only: writes nothing, anywhere.

WHY THIS EXISTS. `harness-init` derives a repository's commands from its CI
workflows' `run:` lines, and the execution skills run those commands verbatim.
Neither knows that a repository can pin a toolchain VERSION that the bare
command on PATH does not satisfy. Measured on acme-frontend, 2026-09-18:

    $ flutter pub get --enforce-lockfile
    Because acme requires SDK version >=3.12.2 <4.0.0, version solving failed.

The machine had Dart 3.11.5. The repository pins Flutter 3.44.8 in `.fvmrc`, and
`fvm flutter pub get` exits 0. **Bare PATH did not fail safe — it failed
misleadingly**, with an error about SDK constraints rather than about the
launcher, which is the expensive kind of failure. That is the whole point of
this detector: a pin whose launcher is missing must be a NAMED gap, never a
silent fall-through to whatever happens to be installed.

The pattern is not new here. `detect_network.py` already reads `.nvmrc` for the
Node version and records which file answered, with the same ordering argument:
the pin file is what a developer's shell obeys, the manifest is what the package
declares. This does the same for the rest of the toolchains, and adds the half
that file does not need — the LAUNCHER, because `fvm flutter` is a different
command from `flutter` while `node` is just `node`.

SECOND SOURCE, DELIBERATELY. A repository that uses reusable CI workflows has no
`run:` lines to extract at all — acme calls
`VeryGoodOpenSource/very_good_workflows/.../flutter_package.yml@v1.20.1` with
`flutter_version: 3.44.8` as an INPUT. The commands are unknowable without
fetching a workflow we do not control, but the PIN is right there, and it agrees
with `.fvmrc`. So `with:` inputs are read as a second source and reported
alongside the first; when they disagree, that is a finding, not a tiebreak.

THE SCRIPT NAMES, READ RATHER THAN ASSUMED. `templates/target-settings.json`
used to ship seven `Bash(pnpm run ...)` allow rules lifted from one backend
repository — `lint`, `lint:fix`, `build`, `test`, `test:cov`, `format:check`,
`openapi:check`. Measured on acme-core, 2026-09-21 (PR #403): five of the seven
name nothing that exists there. It is a Fastify repo whose real scripts are
`test`, `test:coverage`, `build`, `swagger:check`, `swagger:update` — note
`test:cov` vs `test:coverage` and `openapi:check` vs `swagger:check`, near-misses
of the real names, which is what makes template residue expensive to spot. An
allow rule for a script that does not exist is inert, so nothing broke; it just
means the harness told the repository about itself and was wrong.

So the names are read: `package.json`'s `scripts` map (workspaces included) and
a `Makefile`'s targets, with the runner taken from the LOCKFILE rather than from
habit. Two filters, and they are the point:

  * only the VERIFICATION family is offered for allowlisting (`test`, `lint`,
    `build`, `typecheck`, `format:check`, `swagger:check`, …). A repository's
    script map is not a list of safe commands.
  * anything whose name carries a side effect — `deploy`, `publish`, `release`,
    `migrate`, `seed`, `reset`, `start` — is refused even when it also matches
    the verification family (`test:e2e:deploy` is a deploy), and the refusal is
    reported so the filter is visible rather than silent.

A repository whose scripts cannot be read contributes NO allow rules. That is
the whole correction: no names is right, invented names are not.

Usage:
    uv run detect_toolchain.py <root>

stdout: one JSON object. stderr: one human line per toolchain.
Exit: 0 whenever it emitted JSON (advisory — it never fails a run) ·
      2 bad args or unreadable root.
"""
from __future__ import annotations
import json, os, re, shutil, sys

# A pin file, the launcher it implies, and the command that launcher wraps.
# Ordered: the first that answers for a toolchain wins, and the file that
# answered is reported so a person knows which one to edit.
PINS = [
    # (toolchain, filename, launcher prefix, wrapped command, how to read it)
    ("flutter", ".fvmrc",         "fvm",  "flutter", "json:flutter"),
    ("flutter", ".flutter-version", "fvm", "flutter", "text"),
    ("node",    ".nvmrc",         "",     "node",    "text"),
    ("ruby",    ".ruby-version",  "",     "ruby",    "text"),
    ("python",  ".python-version", "",    "python",  "text"),
]

# One file, many toolchains: `<tool> <version>` per line. asdf and mise both
# read it, so the launcher depends on which is installed rather than on the file.
TOOL_VERSIONS = ".tool-versions"

# CI inputs that name a toolchain version. The KEY is the evidence; the value is
# the pin. Extended by PR when a new reusable workflow shows up, deliberately —
# guessing at arbitrary input names would report noise as evidence.
CI_VERSION_KEYS = {
    "flutter_version": "flutter",
    "flutter-version": "flutter",
    "node-version": "node",
    "node_version": "node",
    "ruby-version": "ruby",
    "python-version": "python",
}


# --- The commands a repository declares ------------------------------------
# The runner comes from the lockfile: a repo with a `pnpm-lock.yaml` is run with
# pnpm whatever the author happens to type. Ordered — a repo carrying two
# lockfiles is answered by the first, and the file that answered is reported.
LOCKFILES = [
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
]

# A script is offered for allowlisting only when its name carries one of these.
# Deliberately a vocabulary of VERIFICATION, not of safety in general: the point
# of an allow rule here is that an agent can run the repo's own gates without a
# prompt, and nothing else.
VERIFY_WORDS = {
    "test", "tests", "lint", "build", "typecheck", "tsc", "types",
    "format", "fmt", "check", "analyze", "analyse", "coverage", "cov",
    "verify", "validate", "audit", "spec", "specs", "e2e", "unit",
    "integration", "swagger", "openapi", "contract", "doctor",
}

# …and refused when the name carries one of these, even if it also matched
# above. `test:e2e:deploy` is a deploy. The refusal is reported, never silent —
# a filter nobody can see is a filter nobody can correct.
SIDE_EFFECT_WORDS = {
    "deploy", "publish", "release", "start", "serve", "dev", "watch",
    "migrate", "migration", "migrations", "seed", "reset", "drop", "clean",
    "prune", "push", "install", "preinstall", "postinstall", "prepare",
    "prod", "production", "staging", "sign", "notarize", "upload", "docker",
    "generate", "gen", "codegen", "write", "fix", "update", "bump",
}

# A guard against a repo whose script map is enormous: an allowlist is read by
# people, and 80 rules is not a list anyone reads.
MAX_ALLOW_RULES = 24


def _name_words(name: str) -> set[str]:
    """`format:check` and `test-cov` both split into their parts, lowercased."""
    return {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w}


def _classify(name: str) -> tuple[bool, str]:
    """(offer it for allowlisting?, why not). The side-effect filter wins."""
    words = _name_words(name)
    hit = sorted(words & SIDE_EFFECT_WORDS)
    if hit:
        return False, f"side effect: `{hit[0]}`"
    if not (words & VERIFY_WORDS):
        return False, "not a verification command"
    return True, ""


def _package_scripts(root: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Every `scripts` key of every package.json at depth <= 3, as
    (name, source-relative-path). Workspaces included, because a monorepo's
    gates live in the packages, and node_modules excluded, because a
    dependency's scripts are not this repository's."""
    found: list[tuple[str, str]] = []
    sources: list[str] = []
    skip = {"node_modules", ".git", "dist", "build", "vendor", ".dart_tool"}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [] if depth >= 3 else [d for d in dirnames if d not in skip and not d.startswith(".")]
        if "package.json" not in filenames:
            continue
        text = _read(os.path.join(dirpath, "package.json"), 200000)
        try:
            scripts = (json.loads(text or "{}") or {}).get("scripts")
        except Exception:
            continue
        if not isinstance(scripts, dict):
            continue
        src = os.path.join(rel, "package.json") if rel != "." else "package.json"
        sources.append(src)
        for name in scripts:
            if isinstance(name, str) and name.strip():
                found.append((name.strip(), src))
    return found, sources


def _make_targets(root: str) -> list[tuple[str, str]]:
    """A Makefile's own targets. `.PHONY` and pattern rules are not targets a
    person runs by name, and a variable assignment is not a target at all."""
    out: list[tuple[str, str]] = []
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = _read(os.path.join(root, name), 60000)
        if text is None:
            continue
        for raw in text.splitlines():
            if raw[:1].isspace() or not raw.strip() or raw.lstrip().startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*:(?!=)", raw)
            if m:
                out.append((m.group(1), name))
        break
    return out


def _commands(root: str) -> dict:
    """The runner, the declared scripts, and the settings fragment they imply.

    The fragment is this detector's own — the same contract `detect_network.py`
    carries, and for the same reason: the script that READS the repository is
    the one that decides what may be seeded from it, and the writer merges what
    it is handed rather than re-deriving it."""
    runner, runner_source = "", ""
    for fname, pm in LOCKFILES:
        if os.path.isfile(os.path.join(root, fname)):
            runner, runner_source = pm, fname
            break

    scripts: list[dict] = []
    seen: set[str] = set()
    pkg_scripts, pkg_sources = _package_scripts(root)
    if pkg_scripts and not runner:
        # package.json with no lockfile: npm is the invocation every Node
        # runner understands, and it is recorded as an assumption, not a read.
        runner, runner_source = "npm", "package.json (no lockfile — assumed)"
    for name, src in pkg_scripts:
        command = f"{runner} run {name}"
        if command in seen:
            continue
        seen.add(command)
        offer, why = _classify(name)
        scripts.append({"name": name, "command": command, "source": src,
                        "verification": offer, **({"refused": why} if why else {})})
    make_targets = _make_targets(root)
    for name, src in make_targets:
        command = f"make {name}"
        if command in seen:
            continue
        seen.add(command)
        offer, why = _classify(name)
        scripts.append({"name": name, "command": command, "source": src,
                        "verification": offer, **({"refused": why} if why else {})})

    verification = [s["command"] for s in scripts if s["verification"]]
    gaps: list[str] = []
    if len(verification) > MAX_ALLOW_RULES:
        gaps.append(
            f"{len(verification)} verification commands declared; only the first "
            f"{MAX_ALLOW_RULES} are seeded into the allowlist. Add the rest to the "
            f"target's own .claude/settings.json if they are wanted — an allowlist "
            f"nobody reads is not a safety control.")
        verification = verification[:MAX_ALLOW_RULES]
    if not pkg_scripts and not make_targets:
        gaps.append(
            "no package.json `scripts` map and no Makefile: this repository "
            "declares no commands to allowlist, so none are seeded. That is the "
            "correct outcome, not a failure — a templated guess would be wrong "
            "in the same way it was wrong on acme-core (#403).")

    fragment: dict = {}
    if verification:
        fragment = {"permissions": {"allow": [f"Bash({c})" for c in verification]}}
    return {
        "runner": runner,
        "runner_source": runner_source,
        "sources": sorted(set(pkg_sources)),
        "scripts": scripts,
        "verification": verification,
        "settings_fragment": fragment,
        "gaps": gaps,
    }


def _read(path: str, cap: int = 4096) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return None


def _pin_from_file(root: str, name: str, how: str) -> str | None:
    text = _read(os.path.join(root, name))
    if not text:
        return None
    if how.startswith("json:"):
        try:
            return str(json.loads(text).get(how.split(":", 1)[1]) or "") or None
        except Exception:
            return None
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line.lstrip("vV").strip() or None


def _tool_versions(root: str) -> dict:
    """`<tool> <version>` per line, comments and blanks skipped."""
    out = {}
    text = _read(os.path.join(root, TOOL_VERSIONS))
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0].lower()] = parts[1]
    return out


def _ci_pins(root: str) -> dict:
    """Versions named as INPUTS to CI workflows.

    A repository on reusable workflows has no `run:` lines to extract, so this
    is often the only thing CI says about the toolchain. Read as evidence, never
    as the commands — what a workflow we do not control runs is not knowable
    from here, and guessing would be worse than saying so.
    """
    found = {}
    wf = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf):
        return found
    for entry in sorted(os.listdir(wf))[:40]:
        if not entry.endswith((".yml", ".yaml")):
            continue
        text = _read(os.path.join(wf, entry), 20000) or ""
        for key, tool in CI_VERSION_KEYS.items():
            m = re.search(rf'^\s*{re.escape(key)}\s*:\s*["\']?([0-9][^\s"\'#]*)', text, re.M)
            if m and tool not in found:
                found[tool] = {"pin": m.group(1), "source": f".github/workflows/{entry}:{key}"}
    return found


def detect(root: str) -> dict:
    tools: dict = {}

    def note(tool, pin, source, launcher, wraps):
        if tool in tools:
            return
        tools[tool] = {"pin": pin, "source": source, "launcher": launcher,
                       "wraps": wraps, "command": f"{launcher} {wraps}".strip()}

    for tool, fname, launcher, wraps, how in PINS:
        pin = _pin_from_file(root, fname, how)
        if pin:
            note(tool, pin, fname, launcher, wraps)

    tv = _tool_versions(root)
    if tv:
        # asdf and mise both read .tool-versions; the launcher is whichever is
        # installed, so it is resolved here rather than assumed from the file.
        runner = "mise" if shutil.which("mise") else ("asdf" if shutil.which("asdf") else "")
        for tool, pin in tv.items():
            note(tool, pin, TOOL_VERSIONS,
                 f"{runner} exec --" if runner else "", tool)

    ci = _ci_pins(root)
    for tool, info in ci.items():
        if tool in tools:
            # Two sources. Agreement is reassurance; disagreement is a finding,
            # never a tiebreak — a repo whose CI and pin file differ is telling
            # you something, and picking one silently hides it.
            tools[tool]["ci_pin"] = info["pin"]
            tools[tool]["ci_source"] = info["source"]
            if info["pin"] != tools[tool]["pin"]:
                tools[tool]["conflict"] = (
                    f'{tools[tool]["source"]} pins {tools[tool]["pin"]} but '
                    f'{info["source"]} pins {info["pin"]}')
        else:
            note(tool, info["pin"], info["source"], "", tool)
            tools[tool]["ci_only"] = True

    # The load-bearing half: a pin whose launcher is missing is a NAMED gap.
    gaps = []
    for tool, t in tools.items():
        parts = (t["launcher"] or "").split()
        launcher = parts[0] if parts else ""
        t["launcher_installed"] = (not launcher) or bool(shutil.which(launcher))
        if not t["launcher_installed"]:
            gaps.append(
                f'{tool}: {t["source"]} pins {t["pin"]} and expects `{launcher}`, which is not '
                f'installed. Running the bare command uses whatever is on PATH, which is how a '
                f'toolchain mismatch reaches you as a confusing error about something else. '
                f'Install {launcher}, or record the bare command deliberately.')
        if t.get("conflict"):
            gaps.append(f'{tool}: {t["conflict"]}')
    commands = _commands(root)
    # One `gaps` list, two producers: a caller that reports gaps reports both
    # without knowing there are two, which is why the commands reader does not
    # get a gaps list of its own.
    gaps.extend(commands.pop("gaps"))
    return {"root": root, "toolchains": tools, "commands": commands, "gaps": gaps}


# The public name `verify_harness.py` imports: check-11 grades the target's
# allowlist against the repository's real script names, and it must grade
# against the SAME reading that seeded them. One definition, two callers.
declared_commands = _commands


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        print("usage: detect_toolchain.py <root>", file=sys.stderr)
        return 2
    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    rep = detect(root)
    print(json.dumps(rep, indent=2, sort_keys=True))
    for tool, t in sorted(rep["toolchains"].items()):
        mark = "ok " if t["launcher_installed"] else "GAP"
        print(f'  {mark} {tool}: {t["command"]} (pin {t["pin"]} from {t["source"]})',
              file=sys.stderr)
    cmds = rep["commands"]
    if cmds["runner"]:
        print(f'  ok  runner: {cmds["runner"]} (from {cmds["runner_source"]})', file=sys.stderr)
    offered = len(cmds["verification"])
    refused = sum(1 for s_ in cmds["scripts"] if not s_["verification"])
    print(f'  ok  commands: {len(cmds["scripts"])} declared, {offered} allowlisted, '
          f'{refused} refused', file=sys.stderr)
    for s_ in cmds["scripts"]:
        if not s_["verification"]:
            print(f'      refused {s_["command"]} — {s_["refused"]}', file=sys.stderr)
    for g in rep["gaps"]:
        print(f"  gap: {g}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
