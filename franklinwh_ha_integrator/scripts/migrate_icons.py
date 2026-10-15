#!/usr/bin/env python3
"""
Migrate all Material Symbols icons to Lucide SVG icons across all templates.

Usage: python scripts/migrate_icons.py [--dry-run]

Strategy:
  <span class="material-symbols-outlined [extra-classes]">icon_name</span>
  →
  <svg class="icon [extra-classes]" ...><use href="static/icons/lucide.svg#icon-name"/></svg>

We also:
  - Remove the Google Fonts Material Symbols <link> from admin.html
  - Add Lucide sprite <link> / inline SVG system
  - Update design-system.css for .icon sizing
"""
import re
import sys
import urllib.request
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv

ROOT = Path(__file__).parent.parent
TEMPLATE_DIR = ROOT / "src" / "templates"
STATIC_DIR   = ROOT / "src" / "static"
ICONS_DIR    = STATIC_DIR / "icons"
CSS_FILE     = STATIC_DIR / "css" / "design-system.css"
ADMIN_HTML   = TEMPLATE_DIR / "admin.html"

# ── Material Symbols → Lucide name mapping ──────────────────────────────────
# Lucide uses kebab-case names. Pulled from lucide.dev
ICON_MAP = {
    "add":                    "plus",
    "arrow_forward":          "arrow-right",
    "bar_chart":              "bar-chart-2",
    "battery_4_bar":          "battery",
    "bolt":                   "zap",
    "calendar_month":         "calendar",
    "campaign":               "megaphone",
    "cancel":                 "x-circle",
    "check":                  "check",
    "check_circle":           "check-circle",
    "chevron_right":          "chevron-right",
    "close":                  "x",
    "cloud_sync":             "cloud",
    "dark_mode":              "moon",
    "delete":                 "trash-2",
    "description":            "file-text",
    "desktop_windows":        "monitor",
    "dns":                    "server",
    "donut_large":            "loader",
    "edit":                   "pencil",
    "electric_bolt":          "zap",
    "energy_program_saving":  "leaf",
    "error":                  "alert-circle",
    "expand_more":            "chevron-down",
    "favorite":               "heart",
    "filter_alt":             "filter",
    "help":                   "help-circle",
    "history_edu":            "scroll",
    "home":                   "home",
    "hub":                    "network",
    "info":                   "info",
    "keyboard_arrow_down":    "chevron-down",
    "keyboard_arrow_up":      "chevron-up",
    "lan":                    "network",
    "light_mode":             "sun",
    "light_mode":             "sun",
    "link":                   "link",
    "memory":                 "cpu",
    "menu":                   "menu",
    "monitor_heart":          "activity",
    "monitoring":             "line-chart",
    "more_vert":              "more-vertical",
    "open_in_new":            "external-link",
    "palette":                "palette",
    "power":                  "power",
    "public":                 "globe",
    "query_stats":            "trending-up",
    "receipt_long":           "receipt",
    "refresh":                "refresh-cw",
    "schedule":               "clock",
    "search":                 "search",
    "security":               "shield",
    "settings":               "settings",
    "signal_cellular_alt":    "signal",
    "smart_toy":              "bot",
    "solar_power":            "sun",
    "support":                "life-buoy",
    "sync":                   "refresh-cw",
    "terminal":               "terminal",
    "tune":                   "sliders",
    "view_list":              "list",
    "warning":                "alert-triangle",
    "water":                  "droplets",
    "wifi":                   "wifi",
    "wifi_off":               "wifi-off",
    "calculate":              "calculator",
    "language":               "globe",
    "save":                   "save",
    "battery_charging_full":  "battery-charging",
    "battery_alert":          "battery-warning",
    "cloud_off":              "cloud-off",
    "cloud_done":             "cloud-check",
    "download":               "download",
    "upload":                 "upload",
    "content_copy":           "copy",
    "vpn_key":                "key",
    "lock":                   "lock",
    "lock_open":              "lock-open",
    "visibility":             "eye",
    "visibility_off":         "eye-off",
    "share":                  "share-2",
    "print":                  "printer",
    "send":                   "send",
    "logout":                 "log-out",
    "login":                  "log-in",
    "person":                 "user",
    "group":                  "users",
    "notifications":          "bell",
    "notifications_off":      "bell-off",
    "star":                   "star",
    "flag":                   "flag",
    "location_on":            "map-pin",
    "timer":                  "timer",
    "speed":                  "gauge",
    "thermostat":             "thermometer",
    "device_hub":             "git-branch",
    "storage":                "hard-drive",
    "cloud_upload":           "upload-cloud",
    "cloud_download":         "download-cloud",
}

# ── Regex patterns ───────────────────────────────────────────────────────────
# Match: <span class="material-symbols-outlined[optional extra classes]">icon_name</span>
# capture groups: 1=extra_classes, 2=icon_name
MAT_PAT = re.compile(
    r'<span\s+class="material-symbols-outlined([^"]*)"[^>]*>([a-z_0-9]+)</span>',
    re.DOTALL
)

def lucide_svg(icon_name: str, extra_classes: str, inline_style: str = "") -> str:
    """Generate <svg class="icon ..."><use href="#lucide-name"/></svg>"""
    lucide_name = ICON_MAP.get(icon_name, icon_name.replace("_", "-"))
    classes = ("icon " + extra_classes.strip()).strip()
    style_attr = f' style="{inline_style}"' if inline_style else ""
    return (
        f'<svg class="{classes}" aria-hidden="true"{style_attr}>'
        f'<use href="static/icons/lucide.svg#{lucide_name}"/>'
        f'</svg>'
    )

def replace_in_file(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    count = 0

    def replacer(m: re.Match) -> str:
        nonlocal count
        count += 1
        extra   = m.group(1)   # e.g. " fa-spin text-ok text-sm" or ""
        icon    = m.group(2)   # e.g. "sync"
        return lucide_svg(icon, extra)

    new_text = MAT_PAT.sub(replacer, text)
    if new_text != text and not DRY_RUN:
        path.write_text(new_text, encoding="utf-8")
    return count


def patch_head(path: Path) -> None:
    """Remove the Material Symbols Google Fonts link from admin.html head."""
    text = path.read_text(encoding="utf-8")
    # Remove the Fonts link line
    new_text = re.sub(
        r'\s*<link[^>]*fonts\.googleapis\.com[^>]*Material[^>]*/>\n?',
        "\n",
        text
    )
    # Remove comment label if present
    new_text = new_text.replace("  <!-- FontAwesome -->\n", "")
    if new_text != text and not DRY_RUN:
        path.write_text(new_text, encoding="utf-8")
        print("  Patched admin.html <head>: removed Material Symbols CDN link")


def patch_css(path: Path) -> None:
    """Add .icon CSS rule and remove .material-symbols-outlined block."""
    text = path.read_text(encoding="utf-8")
    icon_css = """
/* ── Lucide SVG icons ───────────────────────────────────────── */
.icon {
  display: inline-block;
  width:  1.1em;
  height: 1.1em;
  vertical-align: middle;
  stroke: currentColor;
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}
/* spin animation for loading icons */
.icon.fa-spin, .spin { animation: icon-spin 0.8s linear infinite; }
@keyframes icon-spin { to { transform: rotate(360deg); } }
"""
    # Remove old .material-symbols-outlined block
    new_text = re.sub(
        r'/\* ── Material Symbols Default.*?(?=\n\n)',
        "",
        text,
        flags=re.DOTALL
    )
    # Inject icon CSS after the Tokens section if not already present
    if ".icon {" not in new_text:
        new_text = new_text.replace(
            "/* ── FEM Utilities",
            icon_css + "\n/* ── FEM Utilities"
        )
    if new_text != text and not DRY_RUN:
        path.write_text(new_text, encoding="utf-8")
        print("  Patched design-system.css: added .icon rule, removed .material-symbols-outlined")


def download_sprite() -> None:
    """Download Lucide SVG sprite from jsDelivr."""
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    sprite_url = "https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/font/lucide.svg"
    dest = ICONS_DIR / "lucide.svg"
    if dest.exists():
        print(f"  Sprite already at {dest} — skipping download")
        return
    print(f"  Downloading Lucide sprite → {dest}")
    if not DRY_RUN:
        urllib.request.urlretrieve(sprite_url, dest)
        print(f"  Downloaded ({dest.stat().st_size:,} bytes)")


def main():
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Migrating Material Symbols → Lucide SVG icons\n")

    if not DRY_RUN:
        download_sprite()
    else:
        print("  [dry] Would download Lucide sprite to src/static/icons/lucide.svg")

    # Patch CSS
    patch_css(CSS_FILE)

    # Remove Google Fonts CDN link from admin.html head
    patch_head(ADMIN_HTML)

    # Replace icons in all templates
    total = 0
    for html in sorted(TEMPLATE_DIR.rglob("*.html")):
        n = replace_in_file(html)
        if n:
            print(f"  {'[dry] ' if DRY_RUN else ''}Replaced {n:3d} icons  {html.relative_to(ROOT)}")
            total += n

    print(f"\n{'[dry] ' if DRY_RUN else ''}Total replacements: {total}")
    print("Done. Restart the server to apply changes.")


if __name__ == "__main__":
    main()
