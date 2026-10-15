#!/usr/bin/env python3
"""
trim_svg_dictionary.py — FHAI SVG icon payload optimiser.

Two modes:
  python scripts/trim_svg_dictionary.py          # trim (in-place)
  python scripts/trim_svg_dictionary.py --check  # audit only, exit 1 if over budget

What it does:
  1. Scans src/templates/ and src/static/js/ for all href="#<id>" icon references.
  2. Extracts only those <symbol> blocks from svg_dictionary.html.
  3. Overwrites svg_dictionary.html with the trimmed set (unless --check).
  4. Reports: defined / used / removed / dead-weight bytes saved.

Budget thresholds (--check mode):
  - Symbol count in dictionary must be ≤ SYMBOL_BUDGET
  - Every href="#id" referenced in templates must exist in the dictionary
"""

import re
import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "src" / "templates"
JS_DIR = REPO_ROOT / "src" / "static" / "js"
SVG_DICT = REPO_ROOT / "src" / "templates" / "components" / "svg_dictionary.html"

# Budget: warn/fail if the trimmed dictionary exceeds this many symbols.
# 115 are in use today — 200 gives comfortable headroom for new icons.
SYMBOL_BUDGET = 200


def collect_used_ids() -> set[str]:
    """Scan all templates and JS files for href="#icon-id" references."""
    used = set()
    pattern = re.compile(r'href=["\']#([^"\'>\s]+)["\']')
    for directory in (TEMPLATE_DIR, JS_DIR):
        for path in directory.rglob("*"):
            if path.suffix not in (".html", ".js", ".bak"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for m in pattern.finditer(text):
                    used.add(m.group(1))
            except OSError:
                pass
    return used


def parse_symbols(content: str) -> list[tuple[str, str]]:
    """
    Return list of (id, full_symbol_block) from the SVG dictionary.
    Each block is everything from <symbol id="..."> to the matching </symbol>.
    """
    symbols = []
    pattern = re.compile(
        r'(<symbol\s[^>]*id=["\']([^"\']+)["\'][^>]*>.*?</symbol>)',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(content):
        symbols.append((m.group(2), m.group(1)))
    return symbols


def build_trimmed_content(original: str, keep_ids: set[str], all_symbols: list[tuple[str, str]]) -> str:
    """Rebuild the SVG dictionary keeping only the used symbol blocks."""
    # Extract the outer wrapper (first and last lines)
    lines = original.splitlines(keepends=True)
    # Header: up to and including the opening <svg> line
    header_lines = []
    body_start = 0
    for i, line in enumerate(lines):
        header_lines.append(line)
        if "<svg" in line and "xmlns" in line:
            body_start = i + 1
            break

    # Footer: closing </svg> and wrapper </div>
    footer_lines = []
    for line in reversed(lines):
        footer_lines.insert(0, line)
        if "</svg>" in line:
            break

    kept_blocks = [block for sym_id, block in all_symbols if sym_id in keep_ids]

    return (
        "".join(header_lines)
        + "\n".join(kept_blocks)
        + "\n"
        + "".join(footer_lines)
    )


def main() -> int:
    check_only = "--check" in sys.argv

    if not SVG_DICT.exists():
        print(f"ERROR: svg_dictionary.html not found at {SVG_DICT}", file=sys.stderr)
        return 1

    original = SVG_DICT.read_text(encoding="utf-8")
    original_bytes = len(original.encode("utf-8"))

    all_symbols = parse_symbols(original)
    defined_ids = {sym_id for sym_id, _ in all_symbols}
    used_ids = collect_used_ids()

    # Intersect: only keep symbols that are both defined AND used
    to_keep = defined_ids & used_ids
    unused_defined = defined_ids - used_ids
    used_but_missing = used_ids - defined_ids  # referenced but not in dict (broken icon)

    print(f"\n{'='*60}")
    print(f"  FHAI SVG Icon Audit")
    print(f"{'='*60}")
    print(f"  Symbols defined in dictionary : {len(defined_ids):>5}")
    print(f"  Unique icon refs in templates : {len(used_ids):>5}")
    print(f"  Symbols to KEEP               : {len(to_keep):>5}")
    print(f"  Symbols to REMOVE (dead)      : {len(unused_defined):>5}")
    print(f"  Refs with no matching symbol  : {len(used_but_missing):>5}  {'⚠️ broken!' if used_but_missing else '✓'}")
    print(f"{'='*60}")

    failures = []

    if used_but_missing:
        print(f"\n  ⚠️  BROKEN icon references (href exists, symbol missing):")
        for sym_id in sorted(used_but_missing):
            print(f"     #{sym_id}")
        failures.append(f"{len(used_but_missing)} broken icon references")

    if len(to_keep) > SYMBOL_BUDGET:
        failures.append(
            f"Symbol count {len(to_keep)} exceeds budget of {SYMBOL_BUDGET}. "
            f"Remove unused icons or raise SYMBOL_BUDGET with justification."
        )

    if check_only:
        if failures:
            print(f"\n  ❌ BUDGET CHECK FAILED:")
            for f in failures:
                print(f"     • {f}")
            print()
            return 1
        else:
            print(f"\n  ✅ Budget check passed ({len(to_keep)} symbols ≤ {SYMBOL_BUDGET})\n")
            return 0

    # --- TRIM MODE ---
    if not to_keep:
        print("ERROR: No symbols to keep — aborting to avoid wiping the dictionary.", file=sys.stderr)
        return 1

    trimmed = build_trimmed_content(original, to_keep, all_symbols)
    trimmed_bytes = len(trimmed.encode("utf-8"))
    saved_bytes = original_bytes - trimmed_bytes
    saved_pct = (saved_bytes / original_bytes * 100) if original_bytes else 0

    SVG_DICT.write_text(trimmed, encoding="utf-8")

    print(f"\n  Original size : {original_bytes / 1024:.1f} KB")
    print(f"  Trimmed size  : {trimmed_bytes / 1024:.1f} KB")
    print(f"  Saved         : {saved_bytes / 1024:.1f} KB  ({saved_pct:.0f}%)")
    print(f"\n  ✅ svg_dictionary.html updated: {len(defined_ids)} → {len(to_keep)} symbols\n")

    if failures:
        print("  ⚠️  Warnings (non-fatal in trim mode):")
        for f in failures:
            print(f"     • {f}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
