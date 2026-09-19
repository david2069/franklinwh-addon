#!/usr/bin/env bash
# Issue a locally-trusted TLS certificate for FHAI and install it in the
# container. Runs on the HOST, not inside the container: mkcert's whole point
# is putting a CA into *this machine's* trust store, which a container cannot
# reach.
#
# Why this exists: a self-signed certificate makes Safari, iOS and iPadOS
# refuse the connection outright — often with no "proceed anyway" for an
# IP-address URL. mkcert issues from a CA your OS trusts, so the warning goes
# away rather than being clicked through. See docs/security_guide.md §4a.1.
#
#   ./scripts/setup_tls_mkcert.sh                 # autodetect LAN IP
#   ./scripts/setup_tls_mkcert.sh --host 192.168.0.247
#   ./scripts/setup_tls_mkcert.sh --dry-run       # show the plan, change nothing
#
# Note on working directory: this script never writes into the current
# directory. Bare `mkcert <host>` drops .pem files wherever you happen to be
# standing, so we pass -cert-file/-key-file explicitly into a temp dir. You can
# run this from anywhere. (`mkcert -install` is cwd-independent regardless — it
# only touches CAROOT and the system trust store.)
set -euo pipefail

CONTAINER="${FHAI_CONTAINER:-fwhhai-app}"
SSL_DIR="${FHAI_SSL_DIR:-/data/ssl}"
HOST=""
DRY_RUN=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host)      HOST="${2:-}"; shift 2 ;;
        --container) CONTAINER="${2:-}"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        -h|--help)   sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '[tls] %s\n' "$*"; }
die()  { printf '[tls] ERROR: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '[tls] would run: %s\n' "$*"; else "$@"; fi; }

# ── Preconditions ────────────────────────────────────────────────────────────
[ -f /.dockerenv ] && die "run this on the host, not inside the container."
command -v docker >/dev/null 2>&1 || die "docker not found on PATH."

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    die "container '$CONTAINER' is not running (override with --container)."
fi

# ── Detect the LAN IP if not supplied ────────────────────────────────────────
if [ -z "$HOST" ]; then
    if command -v ipconfig >/dev/null 2>&1; then
        HOST="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
    fi
    if [ -z "$HOST" ] && command -v ip >/dev/null 2>&1; then
        HOST="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
    fi
    [ -n "$HOST" ] || die "could not detect a LAN IP — pass --host explicitly."
    say "detected LAN IP: $HOST"
fi

# ── Tailscale names ──────────────────────────────────────────────────────────
# If this machine is on a tailnet you very likely browse to it by its MagicDNS
# name, not the LAN IP. A certificate that omits that name fails with
# ERR_CERT_COMMON_NAME_INVALID no matter how thoroughly the CA is trusted —
# SAN mismatch is not a trust problem. Fold the tailnet name and IPs in.
#
# Note there is a better option than mkcert if you only ever reach this box over
# Tailscale: enable HTTPS Certificates in the admin console and use
# `tailscale cert`, which issues a publicly-trusted Let's Encrypt certificate
# needing no CA install on any device. See docs/security_guide.md §4a.3.
TS_NAMES=""
if command -v tailscale >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    TS_NAMES="$(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
self_ = d.get("Self") or {}
out = []
name = (self_.get("DNSName") or "").rstrip(".")
if name:
    out.append(name)
out.extend(self_.get("TailscaleIPs") or [])
print(" ".join(out))
' 2>/dev/null || true)"
    if [ -n "$TS_NAMES" ]; then
        say "tailscale detected — including: $TS_NAMES"
    fi
fi

# ── mkcert ───────────────────────────────────────────────────────────────────
if ! command -v mkcert >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || die "mkcert not installed and no brew to install it."
    if [ "$ASSUME_YES" != 1 ] && [ "$DRY_RUN" != 1 ]; then
        printf '[tls] mkcert is not installed. Install it with brew now? [y/N] '
        read -r reply
        case "$reply" in [yY]*) ;; *) die "aborted." ;; esac
    fi
    run brew install mkcert
fi

# Installs the CA into the system trust store. Prompts for your password, and
# is a no-op if already installed. Independent of the working directory.
say "ensuring the local CA is in the system trust store (may prompt for your password)"
run mkcert -install

# ── Issue the leaf ───────────────────────────────────────────────────────────
# Every name you might browse by must be a SAN; Apple platforms ignore CN.
TMPDIR_CERT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CERT"' EXIT
CRT="$TMPDIR_CERT/server.crt"
KEY="$TMPDIR_CERT/server.key"

# shellcheck disable=SC2086 — TS_NAMES is a deliberate word-split list
say "issuing certificate for: $HOST localhost 127.0.0.1 ::1 $CONTAINER $TS_NAMES"
run mkcert -cert-file "$CRT" -key-file "$KEY" \
    "$HOST" localhost 127.0.0.1 ::1 "$CONTAINER" $TS_NAMES

if [ "$DRY_RUN" = 1 ]; then
    say "dry run — no files copied, container untouched."
    say "CA for other devices: $(mkcert -CAROOT 2>/dev/null)/rootCA.pem"
    exit 0
fi

[ -s "$CRT" ] && [ -s "$KEY" ] || die "mkcert produced no certificate."

# ── Install into the container ───────────────────────────────────────────────
say "backing up any existing certificate"
docker exec "$CONTAINER" sh -c \
    "mkdir -p $SSL_DIR; [ -f $SSL_DIR/server.crt ] && cp $SSL_DIR/server.crt $SSL_DIR/server.crt.bak-\$(date +%Y%m%d%H%M%S) || true; \
     [ -f $SSL_DIR/server.key ] && cp $SSL_DIR/server.key $SSL_DIR/server.key.bak-\$(date +%Y%m%d%H%M%S) || true"

say "installing certificate into $CONTAINER:$SSL_DIR"
docker cp "$CRT" "$CONTAINER:$SSL_DIR/server.crt"
docker cp "$KEY" "$CONTAINER:$SSL_DIR/server.key"
docker exec "$CONTAINER" sh -c \
    "chown root:root $SSL_DIR/server.crt $SSL_DIR/server.key && \
     chmod 644 $SSL_DIR/server.crt && chmod 600 $SSL_DIR/server.key"

# ── Reseal the integrity snapshot ────────────────────────────────────────────
# Rotating the certificate legitimately changes the security fingerprint. Without
# this the next start logs SECURITY_TAMPER_ALERT for a change you just made.
say "resealing the security integrity snapshot"
docker exec "$CONTAINER" python -c "
import asyncio
from pathlib import Path
from src.services.db import set_db_path
set_db_path(Path('/data/config.db'))
from src.services.security_checker import update_security_snapshot
asyncio.run(update_security_snapshot())
print('[tls] snapshot resealed')
" || say "WARNING: reseal failed — expect a tamper alert on next start (harmless, but check §4)."

say "restarting $CONTAINER"
docker restart "$CONTAINER" >/dev/null

say "done."
say ""
say "  Trusted on this machine already. Quit and REOPEN Safari — it caches"
say "  trust decisions for the life of the process."
say ""
say "  For iPhone / iPad, send them this CA and install it:"
say "    $(mkcert -CAROOT)/rootCA.pem"
say "  then enable it under Settings > General > About > Certificate Trust Settings."
