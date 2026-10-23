# vendor-runtime-skills

Skills, agents, commands and tools from vendors, prepared for Automata's agent
runtime. The batteries image and the execution box install this repository at a
pinned commit and link what `catalog.json` lists into the agent's Claude config
(`~/.claude/{skills,agents,commands}`), with the vendor's environment.

This is runtime content, not a plugin catalog: nothing here registers hooks or
MCP servers, nothing is namespaced, and nothing is fetched at run time.

## Layout

```
catalog.json               what each vendor ships, its env and PATH entries
vendors/<vendor>/
  VENDOR.json              upstream repository, commit, version and tree ids
  skills/<name>/SKILL.md
  agents/<name>.md
  commands/<name>.md
  bin/                     runtime shims (added to PATH)
  scripts/, templates/     files the vendor's skills read
scripts/
  import-<vendor>.sh       upstream commit -> vendors/<vendor>/
  verify.sh                the rules below; CI runs it on every push
  scrub.py                 keeps client names out (see "Client names")
```

`catalog.json` placeholders: `{{root}}` is the absolute path of
`vendors/<vendor>` in the installed checkout.

## Rules (`scripts/verify.sh`)

- `catalog.json` lists exactly the skills, agents and commands on disk.
- Names are lowercase and unique across vendors: the runtime links them flat.
- `headlessSafe` names only the vendor's own skills.
- No symlinks, no `hooks/`, `.mcp.json`, `.claude-plugin/` or laptop install command.
- No frontmatter `hooks`, `mcpServers` or `permissionMode`.
- No client names.

## Updating a vendor

```bash
git -C <upstream checkout> fetch origin <commit>
scripts/import-somnio.sh <upstream checkout> <commit>
git diff            # review, then commit and open a PR
```

Then bump the pinned commit of this repository where it is consumed (the
batteries image and the execution box's harness manifest).

## Client names

This repository is public. Vendor content written for a client names that
client; `scripts/scrub.py --fix` replaces those names with placeholders on
import, deletes files named after them, and `--check` fails CI if one comes
back. The names are stored only as sha256 digests in
`scripts/banned-tokens.json`, so the repository never spells them out. To ban
another: `printf '%s' <token> | shasum -a 256`, add the digest with a
placeholder, and re-run the import.

## Vendors

| Vendor | Upstream | Terms |
|:--|:--|:--|
| `somnio` | `somnio-software/somnio-engineering-ai` (private) | © Somnio Software. Redistributed here with Somnio's authorization (2026-10-23); no license is granted for other use. |

`vendors/somnio` keeps the vendor's own variable name for its root,
`CLAUDE_PLUGIN_ROOT`, because its scripts resolve their files from it. It is
an environment variable only; nothing is installed as a plugin.
