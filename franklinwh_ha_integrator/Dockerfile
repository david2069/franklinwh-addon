# syntax=docker/dockerfile:1
# FranklinWH HA Integrator — Home Assistant Add-on image.
#
# Deliberately NOT the repo-root Dockerfile. That one is python:3.12-slim for
# docker-compose; run.sh has `#!/usr/bin/env bashio`, which only exists on the
# Home Assistant base images. Building the add-on from the slim image would
# produce a container that cannot start its own entrypoint.
#
# Build context is this directory, populated by scripts/sync_addon.sh — the
# Supervisor builds from the repository checkout, so everything COPY needs must
# live here rather than one level up.
#
# Supervisor injects:
#   BUILD_FROM  = ghcr.io/home-assistant/{arch}-base:latest
#   BUILD_ARCH  = amd64 | aarch64 | armv7 | armhf | i386

ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

# ── System dependencies ───────────────────────────────────────
# jq is used by run.sh to read /data/options.json; git is needed by pip for any
# VCS-sourced requirement. tzdata carries the zone files: without it a TZ name
# resolves to nothing and the container runs on UTC, which puts every TOU block
# and schedule window out by the site's offset.
RUN apk add --no-cache \
    python3 \
    tzdata \
    py3-pip \
    curl \
    bash \
    jq \
    git

# ── Python dependencies ───────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# ── Environment ───────────────────────────────────────────────
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=ha_addon

# ── Application source ────────────────────────────────────────
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY VERSION /app/VERSION

# src/main.py reads db/seed/device_catalog_seed.json at startup, relative to the
# package root. The previous add-on image omitted db/ entirely, so the device
# catalog silently failed to enrich in add-on installs while working under
# compose. Copy it.
COPY db/ /app/db/

# ── Build-time integrity gate ─────────────────────────────────
RUN python3 /app/scripts/verify_install.py --build

# ── Data directory (Supervisor mounts /data rw via `map: data:rw`) ──
RUN mkdir -p /data

# ── Entrypoint ────────────────────────────────────────────────
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

# Plain HTTP on purpose: ingress proxies here over the Supervisor's internal
# network, and scripts/start.sh refuses TLS in add-on mode. See
# docs/security_guide.md §4a.2.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8099/api/health || exit 1

CMD ["/run.sh"]
