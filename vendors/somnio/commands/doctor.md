---
description: Check that the Somnio developer harness (ruflo MCP, ruflo CLI, gstack) are installed and healthy.
allowed-tools: Bash, Read
---

Run the harness doctor and report the result:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"
```

Also check whether the ruflo MCP server is connected in this session (its tools
are named `mcp__*ruflo*` / `mcp__claude-flow__*`). Summarize:

- which pieces are healthy,
- which are missing (and that /install fixes them),
- whether `uv` is present — `/uat` runs all of its scripts as `uv run`, so its
  absence breaks that skill at step 1; note that a version differing from the
  harness pin is reported as `info` on purpose, because the installer never
  clobbers an existing `uv`,
- whether `python3` is present — the ship gate silently disables itself without
  it, and the harness deliberately does not install it, so relay the per-OS fix
  the doctor prints rather than offering to run it,
- whether gstack telemetry complies with the Somnio policy (off),
- whether the project ruflo runtime is initialized (optional),
- whether the MCP server is live in the current session.

The full harness contract, including the never-do list, is in
docs/DEV_HARNESS.md at the repo root.
