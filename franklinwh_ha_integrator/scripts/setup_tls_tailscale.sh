#!/usr/bin/env bash
# Install a publicly-trusted Let's Encrypt certificate for FHAI, issued by
# Tailscale for this node's MagicDNS name. Runs on the HOST.
#
# Why prefer this over mkcert: the certificate chains to a public CA, so no
# device needs a CA installed — which is the whole difficulty on iOS and
# iPadOS, where trusting a private CA means installing a profile AND enabling
# it under Settings > General > About > Certificate Trust Settings.
#
# The trade-off is validity: Let's Encrypt issues 90 days, so this must be
# re-run periodically. Use --renew from cron/launchd (see --help).
#
# Covers the MagicDNS name only — a public CA will not sign a private LAN IP.
# That is usually fine: MagicDNS resolves on your home network too, and
# Tailscale connects directly when both ends are on the same LAN, so the same
# URL works at home and away.
#
#   ./scripts/setup_tls_tailscale.sh              # issue + install + restart
#   ./scripts/setup_tls_tailscale.sh --dry-run    # show the plan, change nothing
#   ./scripts/setup_tls_tailscale.sh --renew      # quiet re-issue, for cron
set -euo pipefail

CONTAINER="${FHAI_CONTAINER:-fwhhai-app}"
SSL_DIR="${FHAI_SSL_DIR:-/data/ssl}"
DOMAIN=""
DRY_RUN=0
RENEW=0

while [ $# -gt 0 ]; do
    case "$1" in
        --domain)    DOMAIN="${2:-}"; shift 2 ;;
        --container) CONTAINER="${2:-}"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --renew)     RENEW=1; shift ;;
        -h|--help)
            sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
            echo
            echo "Renew automatically (macOS launchd, weekly — Let's Encrypt is 90 days):"
            echo "  create ~/Library/LaunchAgents/com.fhai.tlsrenew.plist running:"
            echo "    $(cd "$(dirname "$0")" && pwd)/$(basename "$0") --renew"
            echo "Or cron:  17 4 * * 0  $(cd "$(dirname "$0")" && pwd)/$(basename "$0") --renew"
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say()  { [ "$RENEW" = 1 ] || printf '[tls] %s\n' "$*"; }
die()  { printf '[tls] ERROR: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '[tls] would run: %s\n' "$*"; else "$@"; fi; }

[ -f /.dockerenv ] && die "run this on the host, not inside the container."
command -v docker    >/dev/null 2>&1 || die "docker not found on PATH."
command -v tailscale >/dev/null 2>&1 || die "tailscale not found on PATH."
command -v python3   >/dev/null 2>&1 || die "python3 required to read tailscale status."

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" \
    || die "container '$CONTAINER' is not running (override with --container)."

STATUS_JSON="$(tailscale status --json 2>/dev/null || true)"
[ -n "$STATUS_JSON" ] || die "could not read tailscale status — is Tailscale running?"

# ── Preflight: HTTPS certificates must be enabled for the tailnet ────────────
# Without it `tailscale cert` fails with an opaque error. CertDomains is null
# until the toggle is set, so check it explicitly and say what to do.
CERT_DOMAINS="$(printf '%s' "$STATUS_JSON" | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: print(""); raise SystemExit
cd = d.get("CertDomains") or []
print(" ".join(cd))
')"

if [ -z "$CERT_DOMAINS" ]; then
    die "HTTPS Certificates are not enabled for this tailnet.
       Enable them once, in the admin console:
         https://login.tailscale.com/admin/dns  →  HTTPS Certificates  →  Enable
       Then re-run this script. Nothing else is needed."
fi

if [ -z "$DOMAIN" ]; then
    DOMAIN="$(printf '%s' "$STATUS_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print((d.get("Self", {}).get("DNSName") or "").rstrip("."))
')"
    [ -n "$DOMAIN" ] || die "could not determine this node MagicDNS name — pass --domain."
fi
say "issuing for: $DOMAIN"

# ── Issue ────────────────────────────────────────────────────────────────────
# Explicit output paths: bare `tailscale cert <domain>` writes into the current
# directory, private key included.
TMPDIR_CERT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CERT"' EXIT
CRT="$TMPDIR_CERT/server.crt"
KEY="$TMPDIR_CERT/server.key"

run tailscale cert --cert-file "$CRT" --key-file "$KEY" "$DOMAIN"

if [ "$DRY_RUN" = 1 ]; then
    say "dry run — nothing issued, container untouched."
    exit 0
fi

[ -s "$CRT" ] && [ -s "$KEY" ] || die "tailscale cert produced no certificate."

# ── Skip a no-op renewal ─────────────────────────────────────────────────────
# `tailscale cert` returns the cached certificate until it nears expiry, so a
# weekly timer would otherwise reinstall an identical file and restart the
# container every week for nothing. Compare against what is actually deployed
# and stop early when they match. Only --renew short-circuits: an explicit run
# should always reinstall, since that is how you recover a corrupted or
# hand-edited pair.
if [ "$RENEW" = 1 ]; then
    CURRENT="$(docker exec "$CONTAINER" cat "$SSL_DIR/server.crt" 2>/dev/null || true)"
    if [ -n "$CURRENT" ] && [ "$CURRENT" = "$(cat "$CRT")" ]; then
        printf '[tls] %s certificate unchanged — no restart needed.\n' "$(date '+%Y-%m-%d %H:%M')"
        exit 0
    fi
    printf '[tls] %s certificate changed — installing and restarting.\n' "$(date '+%Y-%m-%d %H:%M')"
fi

# ── Install ──────────────────────────────────────────────────────────────────
docker exec "$CONTAINER" sh -c \
    "mkdir -p $SSL_DIR; [ -f $SSL_DIR/server.crt ] && cp $SSL_DIR/server.crt $SSL_DIR/server.crt.bak-\$(date +%Y%m%d%H%M%S) || true; \
     [ -f $SSL_DIR/server.key ] && cp $SSL_DIR/server.key $SSL_DIR/server.key.bak-\$(date +%Y%m%d%H%M%S) || true"
docker cp "$CRT" "$CONTAINER:$SSL_DIR/server.crt"
docker cp "$KEY" "$CONTAINER:$SSL_DIR/server.key"
docker exec "$CONTAINER" sh -c \
    "chown root:root $SSL_DIR/server.crt $SSL_DIR/server.key && \
     chmod 644 $SSL_DIR/server.crt && chmod 600 $SSL_DIR/server.key"

# Rotating the certificate legitimately changes the integrity fingerprint;
# without resealing, the next start logs SECURITY_TAMPER_ALERT for our own change.
docker exec "$CONTAINER" python -c "
import asyncio
from pathlib import Path
from src.services.db import set_db_path
set_db_path(Path('/data/config.db'))
from src.services.security_checker import update_security_snapshot
asyncio.run(update_security_snapshot())
" >/dev/null 2>&1 || say "WARNING: snapshot reseal failed — expect a tamper alert next start."

docker restart "$CONTAINER" >/dev/null
say "done — https://$DOMAIN:8099 is now publicly trusted; no CA install on any device."
say "Let's Encrypt validity is 90 days: schedule '--renew' (see --help)."
