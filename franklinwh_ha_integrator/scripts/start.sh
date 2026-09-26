#!/bin/sh
# FHAI container entrypoint — enables TLS only when a usable certificate exists.
#
# The Dockerfile CMD runs `uvicorn src.main:app` directly, which means the
# ssl_keyfile/ssl_certfile block under `if __name__ == "__main__"` in main.py
# never executes: TLS was unreachable in Docker no matter what tls_enabled said.
#
# Deliberately conditional rather than hardcoding --ssl-* flags. Missing or
# unreadable certs would make uvicorn exit at startup, and a container that
# crash-loops on a missing file is a lockout with no UI to fix it from. Falling
# back to HTTP keeps the app reachable so you can regenerate the cert.
set -e

CERT="${FHAI_TLS_CERT:-/data/ssl/server.crt}"
KEY="${FHAI_TLS_KEY:-/data/ssl/server.key}"
ADDON_OPTIONS="${FHAI_ADDON_OPTIONS:-/data/options.json}"

set -- uvicorn src.main:app --host 0.0.0.0 --port "${PORT:-8099}" --workers 1

# Honour the "Enable Standalone TLS (HTTPS)" switch in the Security tab.
#
# That switch wrote tls_enabled to app_config and nothing read it: the only code
# that did lived under `if __name__ == "__main__"` in main.py, which never runs
# in a container. TLS was decided solely by whether a certificate happened to be
# readable, so turning HTTPS "off" in the UI changed nothing and the server kept
# serving it. A control that appears to work and does not is worse than no
# control, and it left removing a file as the only way back to HTTP.
#
# Explicit false disables TLS even with a valid certificate present; absent or
# true keeps the previous behaviour, so nothing changes for anyone who has not
# touched the switch.
TLS_OPT_OUT=0
if [ -f /data/config.db ] && command -v python3 >/dev/null 2>&1; then
    TLS_OPT_OUT="$(python3 - <<'PYEOF' 2>/dev/null || echo 0
import json, sqlite3
try:
    c = sqlite3.connect("file:/data/config.db?mode=ro", uri=True)
    row = c.execute("SELECT value FROM app_config WHERE key='tls_enabled'").fetchone()
except Exception:
    print(0); raise SystemExit
if row is None:
    print(0); raise SystemExit
raw = row[0]
try:
    val = json.loads(raw)
except Exception:
    val = str(raw).strip().strip('"')
print(1 if str(val).strip().lower() in ("false", "0", "no") else 0)
PYEOF
)"
fi

if [ -n "${SUPERVISOR_TOKEN:-}" ] || [ -f "$ADDON_OPTIONS" ]; then
    # Home Assistant add-on. Ingress proxies to us over plain HTTP on the
    # internal Docker network, so serving TLS here breaks the panel: Supervisor
    # speaks HTTP to a TLS socket and gets the same empty reply a browser does.
    # HA already terminates TLS at its own front door with its own certificate,
    # so the add-on must not — and there is nothing to gain by trying.
    #
    # This must win over the cert check below, not merely default to off. Add-on
    # installs map data:rw, and backups archive /data wholesale — so restoring a
    # Docker-install backup into an add-on drops a usable server.crt into
    # /data/ssl and would silently flip TLS on, breaking ingress with no obvious
    # cause. Refuse regardless of what is sitting in the volume.
    echo "[start] Home Assistant add-on detected — serving plain HTTP for ingress."
    echo "[start] TLS is terminated by Home Assistant; enabling it here breaks the panel."
elif [ "$TLS_OPT_OUT" = "1" ]; then
    echo "[start] TLS disabled in Security settings (tls_enabled=false) — serving plain HTTP."
    echo "[start] The certificate at $CERT is left in place; re-enable the switch to use it again."
elif [ -r "$CERT" ] && [ -r "$KEY" ]; then
    echo "[start] TLS certificate found — serving HTTPS ($CERT)"
    set -- "$@" --ssl-keyfile "$KEY" --ssl-certfile "$CERT"
else
    echo "[start] No readable TLS certificate at $CERT — serving plain HTTP."
    echo "[start] Generate one with:"
    echo "[start]   docker exec <container> python /app/scripts/gen_selfsigned_cert.py --host <LAN-IP>"
fi

exec "$@"
