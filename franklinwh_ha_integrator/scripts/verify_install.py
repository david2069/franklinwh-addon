#!/usr/bin/env python3
"""
Post-installation verification for FranklinWH HA Integrator.
Modelled on FEM's verify_install.py pattern.

Runs at:
  • Docker build time   (RUN python3 scripts/verify_install.py --build)
  • Container runtime   (docker exec fwhhai-app ./scripts/manage.sh verify)
  • Manual diagnostics  (python3 scripts/verify_install.py)

Exit code 0 = all checks passed, non-zero = failures detected.
"""

import importlib
import importlib.metadata
import os
import sys

# ═══════════════════════════════════════════════════════════════════
#  CHECK DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

# Critical deps — build MUST fail if any are missing
CRITICAL_IMPORTS = [
    ("fastapi",              "Web framework"),
    ("uvicorn",              "ASGI server"),
    ("jinja2",               "Template engine"),
    ("aiosqlite",            "Async SQLite"),
    ("aiomqtt",              "Async MQTT client"),
    ("paho.mqtt",            "MQTT client (paho)"),
    ("httpx",                "Async HTTP client"),
    ("aiofiles",             "Async file I/O"),
    ("python_multipart",     "Multipart form parsing"),
    ("franklinwh_cloud",     "FranklinWH Cloud API"),
]

# Application modules — verifies PYTHONPATH and code structure
APP_MODULES = [
    ("src.main",                   "FastAPI application"),
    ("src.services.db",            "Database service"),
    ("src.config.manager",         "AppConfig"),
    ("src.models.entities",        "Entity registry"),
    ("src.middleware.auth",        "Auth middleware"),
    ("src.services.mqtt_publisher","MQTT publisher"),
]

# Runtime-only checks (skipped during --build)
RUNTIME_CHECKS = [
    ("data_dir",  "Data directory writable"),
    ("db_health", "SQLite DB accessible"),
]


# ═══════════════════════════════════════════════════════════════════
#  CHECK FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def check_import(module_name: str) -> tuple[bool, str]:
    """Try to import a module, return (success, detail)."""
    try:
        importlib.import_module(module_name)
        try:
            ver = importlib.metadata.version(module_name.split(".")[0])
            return True, f"v{ver}"
        except Exception:
            return True, "OK"
    except ImportError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Import error: {e}"


def check_data_dir() -> tuple[bool, str]:
    """Verify /data or ./data exists and is writable."""
    for path in ["/data", os.path.join(os.getcwd(), "data")]:
        if os.path.isdir(path):
            writable = os.access(path, os.W_OK)
            return writable, f"{path} {'writable' if writable else 'READ-ONLY'}"
    return False, "No data directory found (/data or ./data)"


def check_db_health() -> tuple[bool, str]:
    """Sample the SQLite DB — checks it can be opened and has gateway table."""
    import sqlite3
    for path in ["/data/config.db", "data/config.db"]:
        if os.path.isfile(path):
            try:
                conn = sqlite3.connect(path)
                conn.execute("SELECT COUNT(*) FROM gateways")
                conn.close()
                return True, f"{path} — OK"
            except Exception as e:
                return False, f"{path} — {e}"
    return True, "DB not yet initialised (normal on first run)"


# ═══════════════════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════════════════

def run_checks(build_only: bool = False, quiet: bool = False) -> int:
    passed = failed = warnings = 0
    failures = []

    def _report(ok: bool, label: str, detail: str, critical: bool = True):
        nonlocal passed, failed, warnings
        if ok:
            passed += 1
            if not quiet:
                print(f"  ✅ {label:45s}  {detail}")
        elif critical:
            failed += 1
            failures.append(f"{label}: {detail}")
            print(f"  ❌ {label:45s}  {detail}")
        else:
            warnings += 1
            if not quiet:
                print(f"  ⚠️  {label:45s}  {detail}")

    mode = "BUILD" if build_only else "FULL"
    if not quiet:
        print()
        print("═══════════════════════════════════════════════════════")
        print(f"  🔍 Post-Install Verification ({mode})")
        print("═══════════════════════════════════════════════════════")
        print(f"  Python:  {sys.executable}  ({sys.version.split()[0]})")
        print(f"  CWD:     {os.getcwd()}")
        print()
        print("  ── Critical Dependencies ────────────────────────────")

    for mod_name, desc in CRITICAL_IMPORTS:
        ok, detail = check_import(mod_name)
        _report(ok, f"{desc} ({mod_name})", detail, critical=True)

    if not quiet:
        print()
        print("  ── Application Modules ──────────────────────────────")

    for mod_name, desc in APP_MODULES:
        ok, detail = check_import(mod_name)
        _report(ok, desc, detail, critical=True)

    if not build_only:
        if not quiet:
            print()
            print("  ── Runtime Checks ───────────────────────────────────")
        runtime_fns = {"data_dir": check_data_dir, "db_health": check_db_health}
        for check_id, desc in RUNTIME_CHECKS:
            fn = runtime_fns.get(check_id)
            if fn:
                ok, detail = fn()
                _report(ok, desc, detail, critical=False)

    print()
    total = passed + failed + warnings
    print(f"  Results: {passed}/{total} passed", end="")
    if warnings:
        print(f", {warnings} warnings", end="")
    if failed:
        print(f", {failed} FAILED", end="")
    print()

    if failed:
        print()
        print("  ❌ VERIFICATION FAILED:")
        for f in failures:
            print(f"     • {f}")
        print()
        return 1

    if not quiet:
        print("  ✅ All checks passed")
        print()
    return 0


# ═══════════════════════════════════════════════════════════════════
#  HEADLESS / CI FLAGS  (bypass PIN gate — uses ADMIN_PASSWORD)
# ═══════════════════════════════════════════════════════════════════

def _api(base_url: str, method: str, path: str, admin_password: str = "") -> dict:
    """Minimal HTTP helper — avoids importing requests at build time."""
    import urllib.request
    import urllib.error
    import json as _json
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, method=method.upper())
    if admin_password:
        import base64
        creds = base64.b64encode(f"admin:{admin_password}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    req.add_header("X-Headless-Enable", "true")
    req.add_header("Content-Type", "application/json")
    if method.upper() in ("POST", "PUT"):
        req.data = b"{}"
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return _json.loads(body)
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_test_ha(base_url: str, password: str) -> int:
    """POST /api/ha/test — verify HA connection."""
    print(f"\n  🔌 Testing HA connection via {base_url}/api/ha/test …")
    result = _api(base_url, "POST", "/api/ha/test", password)
    if result.get("ok"):
        ver = result.get("version", "unknown")
        ms = result.get("latency_ms", "?")
        print(f"  ✅ HA Connected — version {ver}, latency {ms}ms")
        return 0
    else:
        print(f"  ❌ HA test failed: {result.get('error', result)}")
        return 1


def cmd_enable_ha(base_url: str, password: str) -> int:
    """POST /api/ha/enable — headless enable (no PIN required with X-Headless-Enable)."""
    print(f"\n  ▶  Enabling HA integration via {base_url}/api/ha/enable …")
    result = _api(base_url, "POST", "/api/ha/enable", password)
    if result.get("ok"):
        print(f"  ✅ HA integration enabled")
        return 0
    else:
        print(f"  ❌ Enable HA failed: {result.get('error', result)}")
        return 1


def cmd_test_mqtt(base_url: str, password: str) -> int:
    """POST /api/mqtt/test — verify MQTT broker connection."""
    print(f"\n  🔌 Testing MQTT connection via {base_url}/api/mqtt/test …")
    result = _api(base_url, "POST", "/api/mqtt/test", password)
    if result.get("ok"):
        host = result.get("host", "?")
        port = result.get("port", "?")
        ms = result.get("latency_ms", "?")
        print(f"  ✅ MQTT Connected — {host}:{port}, latency {ms}ms")
        return 0
    else:
        print(f"  ❌ MQTT test failed: {result.get('error', result)}")
        return 1


def cmd_enable_mqtt(base_url: str, password: str) -> int:
    """POST /api/mqtt/enable — headless enable (no PIN required with X-Headless-Enable)."""
    print(f"\n  ▶  Enabling MQTT publisher via {base_url}/api/mqtt/enable …")
    result = _api(base_url, "POST", "/api/mqtt/enable", password)
    if result.get("ok"):
        print(f"  ✅ MQTT publisher enabled")
        return 0
    else:
        print(f"  ❌ Enable MQTT failed: {result.get('error', result)}")
        return 1


def cmd_check_integrity(base_url: str, password: str) -> int:
    """GET /api/ha/status + /api/mqtt/status — verify startup integrity checks ran."""
    print(f"\n  🔐 Checking config integrity via {base_url} …")
    failures = 0
    for label, path in [("HA", "/api/ha/status"), ("MQTT", "/api/mqtt/status")]:
        result = _api(base_url, "GET", path, password)
        if "error" in result and result.get("ok") is False:
            print(f"  ❌ {label} status unavailable: {result.get('error')}")
            failures += 1
        else:
            state = result.get("service_state") or result.get("state") or "unknown"
            print(f"  ✅ {label} status reachable — service_state={state}")
    return 0 if failures == 0 else 1


# ═══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    # Headless flag resolution
    base_url = os.environ.get("FHAI_BASE_URL", "http://localhost:8099")
    for a in args:
        if a.startswith("--url="):
            base_url = a.split("=", 1)[1]
    admin_password = os.environ.get("ADMIN_PASSWORD", "")

    HEADLESS_CMDS = {
        "--test-ha":         cmd_test_ha,
        "--enable-ha":       cmd_enable_ha,
        "--test-mqtt":       cmd_test_mqtt,
        "--enable-mqtt":     cmd_enable_mqtt,
        "--check-integrity": cmd_check_integrity,
    }

    # If any headless flag present, run ONLY those commands (skip import checks)
    headless_flags = [a for a in args if a in HEADLESS_CMDS]
    if headless_flags:
        exit_code = 0
        for flag in headless_flags:
            rc = HEADLESS_CMDS[flag](base_url, admin_password)
            if rc != 0:
                exit_code = rc
        sys.exit(exit_code)

    # Standard verification mode
    build_only = "--build" in args
    quiet = "--quiet" in args or "-q" in args
    sys.exit(run_checks(build_only=build_only, quiet=quiet))

