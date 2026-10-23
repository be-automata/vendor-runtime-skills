#!/usr/bin/env bash
# Check that every vendor tree is plain runtime content and that catalog.json
# describes it exactly. CI runs this on every push; installers trust it.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
fail=0
bad() { echo "verify: $*" >&2; fail=1; }

jq -e '.schema == 1 and (.vendors | type == "object")' catalog.json >/dev/null || bad "catalog.json: schema 1 with a vendors object expected"

seen=""  # "kind/name vendor" lines; no associative arrays (macOS ships bash 3.2)
for vendor in $(jq -r '.vendors | keys[]' catalog.json); do
  root=$(jq -r --arg v "$vendor" '.vendors[$v].root' catalog.json)
  [ "$root" = "vendors/$vendor" ] || bad "$vendor: root must be vendors/$vendor"
  [ -d "$root" ] || { bad "$vendor: $root missing"; continue; }
  [ -f "$root/VENDOR.json" ] || bad "$vendor: VENDOR.json missing"

  for kind in skills agents commands; do
    listed=$(jq -r --arg v "$vendor" --arg k "$kind" '.vendors[$v][$k] // [] | .[]' catalog.json | LC_ALL=C sort)
    if [ "$kind" = skills ]; then
      present=$(find "$root/skills" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sed 's|.*/||' | LC_ALL=C sort)
    else
      present=$(find "$root/$kind" -mindepth 1 -maxdepth 1 -name '*.md' 2>/dev/null | sed 's|.*/||; s|\.md$||' | LC_ALL=C sort)
    fi
    [ "$listed" = "$present" ] || bad "$vendor: catalog $kind differ from $root/$kind"
    for name in $listed; do
      [[ $name =~ ^[a-z0-9][a-z0-9-]*$ ]] || bad "$vendor: bad $kind name '$name'"
      owner=$(printf '%s\n' "$seen" | awk -v k="$kind/$name" '$1 == k { print $2 }')
      [ -z "$owner" ] || bad "$kind/$name is in both $owner and $vendor"
      seen=$(printf '%s\n%s %s' "$seen" "$kind/$name" "$vendor")
      if [ "$kind" = skills ]; then
        [ -f "$root/skills/$name/SKILL.md" ] || bad "$vendor: skills/$name/SKILL.md missing"
      fi
    done
  done

  for safe in $(jq -r --arg v "$vendor" '.vendors[$v].headlessSafe // [] | .[]' catalog.json); do
    jq -e --arg v "$vendor" --arg s "$safe" '.vendors[$v].skills | index($s)' catalog.json >/dev/null \
      || bad "$vendor: headlessSafe '$safe' is not one of its skills"
  done

  [ -z "$(find "$root" -type l)" ] || bad "$vendor: symlinks are not allowed"
  [ -z "$(find "$root" \( -name hooks -type d -o -name .mcp.json -o -name .claude-plugin -o -path '*/commands/install.md' \))" ] \
    || bad "$vendor: plugin wiring (hooks/, .mcp.json, .claude-plugin, commands/install.md) is not allowed"

  while IFS= read -r -d '' md; do
    awk 'NR == 1 { if ($0 != "---") exit; fm = 1; next }
         fm && /^---[ \t]*$/ { exit }
         fm && /^["\047]?(hooks|mcpServers|permissionMode)["\047]?[ \t]*:/ { print FILENAME ": frontmatter " $1; found = 1 }
         END { exit found }' "$md" || bad "$md: frontmatter may not register hooks, MCP servers or a permission mode"
  done < <(find "$root" \( -path "$root/skills/*/SKILL.md" -o -path "$root/agents/*.md" -o -path "$root/commands/*.md" \) -print0)

  if grep -rIl --include='*.md' 'somnio-engineering-ai:[a-z]' "$root" >/dev/null; then
    bad "$vendor: a plugin-namespaced name is left in markdown"
  fi
done

python3 scripts/scrub.py --check "$repo" || bad "client names found (see above)"

if [ "$fail" -ne 0 ]; then exit 1; fi
echo "verify: ok"
