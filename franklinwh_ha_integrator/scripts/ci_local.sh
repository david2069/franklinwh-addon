#!/usr/bin/env bash
# Run what CI runs, locally, in a throwaway virtualenv.
#
# GitHub Actions minutes ran out on 2026-09-19 and reset on 2026-10-01, so for
# twelve days "check CI after pushing" verified nothing. The working venv is not
# a substitute: it mounts ../franklinwh-cloud over the installed package, so it
# can pass against library code that no published version contains, and it runs
# whatever Python happens to be default — 3.14 here, while CI uses 3.11 and 3.12.
#
# This builds a clean environment from requirements.txt on CI's Python and runs
# CI's exact pytest invocation.
#
#   ./scripts/ci_local.sh            # CI's Python if present, else the default
#   ./scripts/ci_local.sh 3.11       # a specific version
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# CI's matrix. First one present wins unless the caller names a version.
WANTED="${1:-}"
CANDIDATES=("3.12" "3.11")
[[ -n "$WANTED" ]] && CANDIDATES=("$WANTED")

PY=""
for v in "${CANDIDATES[@]}"; do
    for c in "python${v}" "/opt/homebrew/bin/python${v}" "/usr/local/bin/python${v}"; do
        if command -v "$c" >/dev/null 2>&1; then PY="$c"; break 2; fi
    done
done

if [[ -z "$PY" ]]; then
    PY="$(command -v python3)"
    printf '\033[33m!\033[0m no CI Python (%s) found — falling back to %s (%s)\n' \
        "${CANDIDATES[*]}" "$PY" "$("$PY" -V 2>&1)"
    printf '  A pass here does not prove CI would pass.\n'
fi

VENV="$(mktemp -d)/ci"
trap 'rm -rf "$(dirname "$VENV")"' EXIT

printf '\n\033[1mCI, locally\033[0m — %s\n' "$("$PY" -V 2>&1)"
printf '  venv: %s\n\n' "$VENV"

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

# Exactly what .github/workflows/ci.yml runs.
"$VENV/bin/python" -m pytest tests/ --tb=short -q
