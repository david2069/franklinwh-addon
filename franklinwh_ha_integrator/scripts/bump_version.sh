#!/usr/bin/env bash
# Cut a release: bump the version and close off the changelog.
#
# Home Assistant offers an update when `version:` in the published config.yaml
# changes — nothing else triggers it. Changing the changelog alone republishes
# quietly and nobody is told, which is what happened after the setup wizard
# landed.
#
#   ./scripts/bump_version.sh 0.6.1
#
# Moves [Unreleased] under the new version with today's date, leaves a fresh
# [Unreleased] on top, and updates VERSION, config.yaml and src/__init__.py so
# the three cannot disagree — test_addon_version_matches_the_application_version
# fails if they do.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NEW="${1:-}"
if ! [[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'usage: %s X.Y.Z\n' "$0" >&2
    exit 2
fi

OLD="$(cat VERSION)"
[[ "$NEW" != "$OLD" ]] || { printf 'already at %s\n' "$NEW" >&2; exit 2; }

TODAY="$(date +%Y-%m-%d)"

printf '%s\n' "$NEW" > VERSION
sed -i '' -E "s/^version: \"$OLD\"/version: \"$NEW\"/" franklinwh_ha_integrator/config.yaml
sed -i '' -E "s/__version__ = \"$OLD\"/__version__ = \"$NEW\"/" src/__init__.py

"$ROOT/.venv/bin/python" - "$NEW" "$TODAY" <<'PY'
import sys

version, today = sys.argv[1:3]
path = "CHANGELOG.md"
text = open(path).read()

marker = "## [Unreleased]"
i = text.index(marker)

# Everything under [Unreleased] becomes the release; a fresh one takes its place
# so the next change has somewhere to go without anyone having to remember.
text = (
    text[:i]
    + "## [Unreleased]\n\n"
    + f"## [{version}] - {today}\n"
    + text[i + len(marker):]
)
open(path, "w").write(text)
print(f"  CHANGELOG: [Unreleased] closed as [{version}] - {today}")
PY

cp CHANGELOG.md franklinwh_ha_integrator/CHANGELOG.md
# VERSION is mirrored as a file in its own right, not via the src/scripts/db
# directories below — missing it leaves the add-on reporting the old version.
cp VERSION franklinwh_ha_integrator/VERSION
for d in src scripts db; do
    rsync -a --delete --exclude=__pycache__ --exclude='*.pyc' --exclude=.DS_Store \
        "$d/" "franklinwh_ha_integrator/$d/"
done

printf '  VERSION      %s -> %s\n' "$OLD" "$NEW"
printf '  config.yaml  %s\n' "$(grep -E '^version:' franklinwh_ha_integrator/config.yaml)"
printf '\n  Next: commit, then ./scripts/publish_addon.sh\n'
printf '  Home Assistant offers the update once the published version changes.\n'
