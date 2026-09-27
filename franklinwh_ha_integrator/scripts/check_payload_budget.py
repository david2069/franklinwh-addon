#!/usr/bin/env python3
"""
check_payload_budget.py — FHAI frontend payload budget enforcer.

Usage:
  python3 scripts/check_payload_budget.py           # CI mode: exit 1 on violations
  python3 scripts/check_payload_budget.py --report  # Human-readable summary only

Enforcement Rules (tuned for the fix on 2026-06-22):
  1. SVG symbol count in dictionary   ≤ SYMBOL_BUDGET     (default 200)
  2. SVG symbols referenced but missing from dictionary = 0 (broken icons)
  3. Synchronous CDN <script> in <head> = 0 (CDN without defer/async = blocking + crash risk)
  4. Inline SVG HTML size            ≤ INLINE_SVG_KB       (default 100 KB)

Why these rules?
  - The iPad crash was caused by 868 KB of inline SVG (1,563 symbols, 115 used)
    + a 2 MB synchronous Mermaid CDN load, crashing WebKit jetsam on every page.
  - Rule 1+4: catches any future icon library import that isn't trimmed.
  - Rule 2: catches broken icon <use href="#..."> references silently added to templates.
  - Rule 3: catches any future CDN dependency added without defer/async.
"""

import re
import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "src" / "templates"
JS_DIR = REPO_ROOT / "src" / "static" / "js"
SVG_DICT = REPO_ROOT / "src" / "templates" / "components" / "svg_dictionary.html"
ADMIN_TEMPLATE = REPO_ROOT / "src" / "templates" / "admin.html"

SYMBOL_BUDGET  = 200   # max symbols in svg_dictionary.html
INLINE_SVG_KB  = 100   # max allowed size of the inline SVG payload (KB)

# ── RULE 5 budgets (added 2026-08-09, GH #4) ─────────────────────────────────
# Rules 1-4 measure SVG bytes and remote URLs. They reported "All budget checks
# passed" on the page that crashes iPad Safari, because the dominant term — JS
# bytes — was never measured, and RULE 3 exempts anything not served over
# http(s). The historical fix for the previous OOM was to vendor the CDN
# scripts locally, which removed the network dependency but not the parse,
# execute, or MutationObserver cost, and made all of it invisible here.
#
# BLOCKING is what actually stalls first paint, so it is the tighter budget.
# TOTAL is a ratchet on overall page weight — deliberately set just above the
# current value so it fails on growth rather than demanding an immediate cull.
# Lower both as items 1-4 of docs/mobile_crash_cull_plan.md land.
# Ratcheted 2026-09-15, when item 1 landed. The previous values were 450 and
# 950, set "just above the current value so it fails on growth" — which meant
# BLOCKING_JS_KB was 450 while Tailwind alone was 403, so the rule could not
# fail on the very thing it was added for. A budget set above the known
# offender is not a budget.
#
# With the Play CDN compiler replaced by a prebuilt stylesheet, nothing blocks
# the parser at all. 60 leaves room for a small bootstrap without letting
# another 400 KB library back in.
BLOCKING_JS_KB = 60    # max synchronous JS in <head> (currently 0)
TOTAL_JS_KB    = 550   # max JS referenced by admin.html (currently ~478)
# Ratchet: was 1500 when 21 per-tab modules loaded statically. Lazy loading
# (GH #4 items 2-4) cut ~600 KB; the budget follows so the saving cannot be
# quietly given back by re-adding a static tag.


def collect_used_icon_ids() -> set[str]:
    """Collect all href="#id" icon references across templates and JS."""
    used: set[str] = set()
    # Only <use href="#id"> is an icon reference. Matching any href="#..."
    # also caught ordinary in-page anchors — a link to #tariff-time-windows was
    # reported as a broken icon, which is a false failure that teaches people
    # to ignore this script.
    pattern = re.compile(r'<use\b[^>]*?href=["\']#([^"\'>\s]+)["\']', re.I | re.S)
    # Skip vendored third-party JS files — they may contain internal SVG strings
    VENDOR_JS_PREFIXES = (
        "leaflet", "mermaid", "alpinejs", "chartjs", "chart."
    )
    for directory in (TEMPLATE_DIR, JS_DIR):
        for path in directory.rglob("*"):
            if path.suffix not in (".html", ".js"):
                continue
            if path.suffix == ".js" and any(path.name.lower().startswith(p) for p in VENDOR_JS_PREFIXES):
                continue  # skip vendor libraries
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for m in pattern.finditer(text):
                    icon_id = m.group(1)
                    # Skip JS template literals like ${p.icon}
                    if "${" not in icon_id and "{{" not in icon_id:
                        used.add(icon_id)
            except OSError:
                pass
    return used


def get_defined_symbol_ids(content: str) -> set[str]:
    """Extract all symbol IDs defined in an SVG sprite sheet."""
    return set(re.findall(r'<symbol\s[^>]*id=["\']([^"\']+)["\']', content, re.IGNORECASE))


def measure_js_payload(content: str) -> tuple[int, int, list[tuple[str, int]]]:
    """
    Measure the JS a page pulls in, regardless of where it is served from.

    Returns (total_bytes, blocking_bytes, blocking_files) where a script counts
    as blocking when it carries neither `defer` nor `async`. Unlike RULE 3 this
    does NOT skip local files — a vendored 400 KB library blocks the parser
    exactly as hard as a remote one.
    """
    total = 0
    blocking = 0
    blocking_files: list[tuple[str, int]] = []

    for m in re.finditer(r'<script\s([^>]*)>', content, re.IGNORECASE):
        attrs = m.group(1)
        src_match = re.search(r'src=["\']([^"\']+)["\']', attrs)
        if not src_match:
            continue  # inline block — not a fetched payload
        src = src_match.group(1)
        if src.startswith(("http://", "https://")):
            continue  # remote: size unknowable offline; RULE 3 already gates these

        # Strip any ?v= cache-buster before resolving on disk
        rel = src.split("?", 1)[0].lstrip("/")
        path = REPO_ROOT / "src" / rel
        if not path.is_file():
            continue

        size = path.stat().st_size
        total += size
        if not ("defer" in attrs.lower() or "async" in attrs.lower()):
            blocking += size
            blocking_files.append((rel, size))

    return total, blocking, blocking_files


def check_cdn_scripts(content: str) -> list[str]:
    """
    Find synchronous CDN <script> tags (no defer, no async) in the <head>.
    These are blocking and crash WebKit on iPadOS under memory pressure.
    """
    violations = []
    # Find everything in <head>
    head_match = re.search(r'<head\b.*?</head>', content, re.DOTALL | re.IGNORECASE)
    if not head_match:
        return violations
    head = head_match.group(0)

    # Pattern: <script src="http..."> without defer or async
    script_pattern = re.compile(
        r'<script\s([^>]*)>',
        re.IGNORECASE
    )
    for m in script_pattern.finditer(head):
        attrs = m.group(1)
        src_match = re.search(r'src=["\']([^"\']+)["\']', attrs)
        if not src_match:
            continue
        src = src_match.group(1)
        if not (src.startswith('http://') or src.startswith('https://')):
            continue  # local — OK
        has_defer = 'defer' in attrs.lower()
        has_async = 'async' in attrs.lower()
        if not has_defer and not has_async:
            violations.append(src)
    return violations


def main() -> int:
    report_only = "--report" in sys.argv
    failures: list[str] = []
    warnings: list[str] = []

    print(f"\n{'='*62}")
    print(f"  FHAI Frontend Payload Budget Check")
    print(f"{'='*62}")

    # ── Rule 1+2: SVG symbol count and broken refs ────────────────────────────
    if not SVG_DICT.exists():
        failures.append(f"svg_dictionary.html not found at {SVG_DICT}")
    else:
        content = SVG_DICT.read_text(encoding="utf-8")
        defined = get_defined_symbol_ids(content)
        used    = collect_used_icon_ids()

        svg_size_kb = len(content.encode("utf-8")) / 1024
        unused  = defined - used

        # Rule 1: budget
        active_count = len(defined)
        print(f"\n  [RULE 1] SVG symbol budget")
        print(f"    Defined  : {active_count}")
        print(f"    Used     : {len(used & defined)}")
        print(f"    Unused   : {len(unused)}")
        print(f"    Budget   : ≤ {SYMBOL_BUDGET}")
        if active_count > SYMBOL_BUDGET:
            failures.append(
                f"SVG symbol count {active_count} exceeds budget {SYMBOL_BUDGET}. "
                f"Run: python3 scripts/trim_svg_dictionary.py"
            )
            print(f"    ❌ FAIL — exceeds budget by {active_count - SYMBOL_BUDGET}")
        else:
            print(f"    ✅ PASS")

        # Rule 2: broken refs
        broken = used - defined
        # Exclude JS template literals that slipped through
        broken = {b for b in broken if "${" not in b and "{{" not in b}
        print(f"\n  [RULE 2] Broken icon references")
        print(f"    Referenced but missing in dictionary: {len(broken)}")
        if broken:
            failures.append(
                f"{len(broken)} broken icon reference(s): {', '.join(sorted(broken)[:10])}"
            )
            print(f"    ❌ FAIL")
            for b in sorted(broken)[:10]:
                print(f"       #{b}")
        else:
            print(f"    ✅ PASS")

        # Rule 4: inline SVG size
        print(f"\n  [RULE 4] Inline SVG payload size")
        print(f"    Current  : {svg_size_kb:.1f} KB")
        print(f"    Budget   : ≤ {INLINE_SVG_KB} KB")
        if svg_size_kb > INLINE_SVG_KB:
            failures.append(
                f"Inline SVG size {svg_size_kb:.1f} KB exceeds budget {INLINE_SVG_KB} KB. "
                f"Run: python3 scripts/trim_svg_dictionary.py"
            )
            print(f"    ❌ FAIL")
        else:
            print(f"    ✅ PASS")

    # ── Rule 3: synchronous CDN scripts ──────────────────────────────────────
    print(f"\n  [RULE 3] Synchronous CDN script dependencies")
    if not ADMIN_TEMPLATE.exists():
        warnings.append(f"admin.html not found — skipping CDN check")
        print(f"    ⚠️  SKIP — template not found")
    else:
        admin_content = ADMIN_TEMPLATE.read_text(encoding="utf-8")
        cdn_violations = check_cdn_scripts(admin_content)
        if cdn_violations:
            failures.append(
                f"{len(cdn_violations)} synchronous CDN script(s) in <head> (blocking, crashes iPadOS):\n"
                + "\n".join(f"  • {u}" for u in cdn_violations)
            )
            print(f"    ❌ FAIL — {len(cdn_violations)} blocking CDN script(s):")
            for u in cdn_violations:
                print(f"       {u}")
        else:
            print(f"    ✅ PASS — no synchronous CDN scripts")

    # ── RULE 5: JS payload (GH #4) ────────────────────────────────────────────
    print(f"\n  [RULE 5] JavaScript payload")
    if not ADMIN_TEMPLATE.exists():
        print(f"    ⚠️  SKIP — template not found")
    else:
        admin_content = ADMIN_TEMPLATE.read_text(encoding="utf-8")
        total_js, blocking_js, blocking_files = measure_js_payload(admin_content)
        total_kb    = total_js / 1024
        blocking_kb = blocking_js / 1024

        print(f"    Total    : {total_kb:.0f} KB   (budget ≤ {TOTAL_JS_KB} KB)")
        print(f"    Blocking : {blocking_kb:.0f} KB   (budget ≤ {BLOCKING_JS_KB} KB)")
        for name, size in sorted(blocking_files, key=lambda x: -x[1]):
            print(f"       • {name}  {size/1024:.0f} KB")

        rule5_failed = False
        if blocking_kb > BLOCKING_JS_KB:
            failures.append(
                f"Blocking JS {blocking_kb:.0f} KB exceeds {BLOCKING_JS_KB} KB budget. "
                f"Add defer/async, or load on demand. Offenders:\n"
                + "\n".join(f"  • {n} ({s/1024:.0f} KB)" for n, s in blocking_files)
            )
            rule5_failed = True
        if total_kb > TOTAL_JS_KB:
            failures.append(
                f"Total JS {total_kb:.0f} KB exceeds {TOTAL_JS_KB} KB budget. "
                f"See docs/mobile_crash_cull_plan.md — load per-tab modules on demand."
            )
            rule5_failed = True
        if not rule5_failed:
            print(f"    ✅ PASS")
        else:
            print(f"    ❌ FAIL")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    if failures:
        print(f"\n  ❌ {len(failures)} BUDGET VIOLATION(S):\n")
        for i, f in enumerate(failures, 1):
            print(f"  {i}. {f}\n")
        if warnings:
            print(f"  ⚠️  {len(warnings)} warning(s):")
            for w in warnings:
                print(f"     • {w}")
        print()
        return 0 if report_only else 1
    else:
        print(f"\n  ✅ All budget checks passed\n")
        if warnings:
            for w in warnings:
                print(f"  ⚠️  {w}")
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
