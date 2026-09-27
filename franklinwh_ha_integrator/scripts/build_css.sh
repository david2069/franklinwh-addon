#!/usr/bin/env bash
# Build the Tailwind stylesheet ahead of time.
#
# admin.html used to load the Tailwind Play CDN build — a 403 KB browser JIT
# that scans every element's classes, generates CSS at runtime, and installs a
# document-wide MutationObserver that recompiles on every DOM change. Alpine
# mutates the DOM constantly, so that is a compounding CPU and memory load, and
# it is the leading suspect for the iOS Safari crashes in GH #4. Tailwind
# documents the Play build as development-only.
#
# The output is COMMITTED. The add-on image has no node, and the Supervisor
# runs no build step, so the stylesheet has to be in the repository.
#
#   ./scripts/build_css.sh
#
# The CLI is a standalone binary — no node required. It is downloaded on first
# run into build/tools/, which is gitignored.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$ROOT/build/tools/tailwindcss"
VERSION="v3.4.1"          # matches the runtime build it replaces
OUT="$ROOT/src/static/css/tailwind.build.css"

if [ ! -x "$CLI" ]; then
    mkdir -p "$(dirname "$CLI")"
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64)  ASSET=tailwindcss-macos-arm64 ;;
        Darwin-x86_64) ASSET=tailwindcss-macos-x64 ;;
        Linux-aarch64) ASSET=tailwindcss-linux-arm64 ;;
        Linux-x86_64)  ASSET=tailwindcss-linux-x64 ;;
        *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
    esac
    echo "  fetching $ASSET $VERSION"
    curl -sSL -o "$CLI" \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/$ASSET"
    chmod +x "$CLI"
fi

cd "$ROOT"
# Deliberately NOT --minify.
#
# Minifying dropped 59 selectors. The cause is upstream: this config maps the
# gray palette onto CSS custom properties, and Tailwind cannot apply an alpha
# modifier to a variable, so `bg-gray-500/10` compiles to a declaration the
# minifier then discards as invalid. Those classes never worked under the
# browser JIT either — but shipping what Tailwind generated, rather than what a
# minifier decided to keep, removes a variable from an already subtle change.
#
# The cost is ~49 KB uncompressed, most of which gzip recovers, against a
# 403 KB JS compiler removed.
"$CLI" -c tailwind.config.js -i src/static/css/tailwind.src.css -o "$OUT"

printf '  %s  %s KB\n' "${OUT#$ROOT/}" "$(( $(wc -c < "$OUT") / 1024 ))"
