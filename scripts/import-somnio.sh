#!/usr/bin/env bash
# Import the runtime content of Somnio's engineering plugin into vendors/somnio/.
#
#   scripts/import-somnio.sh <somnio-engineering-ai checkout> <40-hex commit>
#
# Run it from a machine with read access to the upstream repository; nothing
# here needs a token. It takes the plugin's skills, agents, commands, scripts
# and templates at that commit and turns them into plain runtime content:
#   - symlinks become real files (the plugin links its skills and templates
#     into the repository's top-level trees);
#   - no plugin manifest, hooks/, .mcp.json or laptop install command;
#   - skill and agent names lose the plugin namespace, because the runtime
#     links them flat into ~/.claude (an agent dispatched as
#     `somnio-engineering-ai:harness-analyzer` would not be found there);
#   - client names are replaced (scripts/scrub.py).
# It then rewrites vendors/somnio/VENDOR.json and the somnio entry of
# catalog.json from what it imported. Review the diff before committing.
set -euo pipefail

die() { echo "import-somnio: $*" >&2; exit 1; }

[ $# -eq 2 ] || die "usage: $0 <somnio-engineering-ai checkout> <40-hex commit>"
src=$1
sha=$2
[[ $sha =~ ^[0-9a-f]{40}$ ]] || die "not a full commit sha: $sha"
git -C "$src" cat-file -e "$sha^{commit}" 2>/dev/null || die "$sha is not a commit in $src (fetch it first)"

repo=$(cd "$(dirname "$0")/.." && pwd)
dest=$repo/vendors/somnio
plugin=plugins/somnio-engineering-ai
paths=("$plugin" skills templates)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$src" archive --format=tar "$sha" "${paths[@]}" | tar -x -C "$work"

out=$work/out
mkdir -p "$out"
for kind in skills agents commands scripts templates; do
  cp -RL "$work/$plugin/$kind" "$out/$kind"
done
rm -f "$out/commands/install.md"
if [ -n "$(find "$out" -type l)" ]; then die "a symlink survived the copy"; fi

# Plain names: `somnio-engineering-ai:<name>` -> `<name>` in prose and dispatch prompts.
find "$out" -type f -name '*.md' -exec perl -pi -e 's/\bsomnio-engineering-ai:(?=[a-z])//g' {} +

python3 "$repo/scripts/scrub.py" --fix "$out"

for kind in skills agents commands scripts templates; do
  rm -rf "${dest:?}/$kind"
  mv "$out/$kind" "$dest/$kind"
done

version=$(git -C "$src" show "$sha:$plugin/.claude-plugin/plugin.json" | jq -r .version)
trees=$(for p in "${paths[@]}"; do
  jq -n --arg p "$p" --arg t "$(git -C "$src" rev-parse "$sha:$p")" '{($p): $t}'
done | jq -s add)
jq -n --arg sha "$sha" --arg version "$version" --argjson trees "$trees" '{
  vendor: "somnio",
  upstream: {
    repo: "https://github.com/somnio-software/somnio-engineering-ai",
    commit: $sha,
    version: $version,
    trees: $trees
  },
  transform: "scripts/import-somnio.sh"
}' >"$dest/VENDOR.json"

names() { find "$dest/$1" -mindepth 1 -maxdepth 1 "${@:2}" | sed 's|.*/||; s|\.md$||' | LC_ALL=C sort | jq -R . | jq -s .; }
tmp=$(mktemp)
jq --argjson skills "$(names skills -type d)" \
   --argjson agents "$(names agents -name '*.md')" \
   --argjson commands "$(names commands -name '*.md')" \
   '.vendors.somnio.skills = $skills | .vendors.somnio.agents = $agents | .vendors.somnio.commands = $commands' \
   "$repo/catalog.json" >"$tmp"
mv "$tmp" "$repo/catalog.json"

"$repo/scripts/verify.sh"
echo "import-somnio: imported $version ($sha); review git diff, then commit"
