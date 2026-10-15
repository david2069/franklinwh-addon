#!/usr/bin/env bashio
# FranklinWH HA Integrator — Home Assistant Add-on entrypoint
#
# HA Supervisor writes user options directly to /data/options.json.
# Our Python app (AppConfig.load()) reads that file — no need for
# bashio::config API calls here.
#
# This script only needs to:
#   1. Add MQTT host fallback if user left it blank
#   2. Export PYTHONPATH (S6 does not inherit Docker ENV)
#   3. Launch uvicorn

set -e

ADDON_DATA="/data"
OPTIONS_FILE="${ADDON_DATA}/options.json"

# ── Supervisor token ──────────────────────────────────────────
# This must come FIRST, before anything calls bashio.
#
# s6-overlay does not inherit Docker ENV — the same reason PYTHONPATH is
# exported explicitly below. bashio authenticates to the Supervisor with
# SUPERVISOR_TOKEN, so until it is exported EVERY bashio Supervisor call fails
# with "Unable to access the API, forbidden". This block used to sit just above
# the uvicorn exec, which meant the timezone lookup and the MQTT service lookup
# both ran unauthenticated and both silently fell back:
#
#   * no timezone  → the container runs UTC, and TOU blocks, demand windows and
#     export windows are local wall-clock, so in Sydney every one of them is ten
#     hours out
#   * no mqtt service → no broker credentials → an anonymous connection the
#     Mosquitto add-on refuses with "[code:135] Not authorized"
#
# The app needs it too: api_ha.py self-configures the Home Assistant connection
# with it, and the setup wizard reads its presence to tell an add-on install
# from a plain Docker one.
if [ -z "${SUPERVISOR_TOKEN:-}" ] && [ -f /var/run/s6/container_environment/SUPERVISOR_TOKEN ]; then
    SUPERVISOR_TOKEN="$(cat /var/run/s6/container_environment/SUPERVISOR_TOKEN)"
fi
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    export SUPERVISOR_TOKEN
    # Older bashio reads HASSIO_TOKEN, newer reads SUPERVISOR_TOKEN. Which one
    # this base image ships is not worth depending on: exporting both costs
    # nothing, and getting it wrong costs the timezone and the MQTT credentials.
    export HASSIO_TOKEN="${SUPERVISOR_TOKEN}"
    bashio::log.info "Supervisor API available — Home Assistant configures itself."
else
    bashio::log.warning "SUPERVISOR_TOKEN unavailable — timezone, MQTT credentials"
    bashio::log.warning "and the HA connection must all be set manually."
fi

# ── Timezone ──────────────────────────────────────────────────
# Without this the container runs on UTC. The compose deployment mounts the
# host's /etc/localtime; an add-on cannot, so the Supervisor is asked instead.
#
# This is not cosmetic. TOU schedule blocks, demand windows and export windows
# are local wall-clock, and the scheduler fires on local time — on UTC in
# Sydney every one of them is ten hours out, which looks like a broken schedule
# rather than a missing timezone.
# Both streams: bashio::log.error writes to STDOUT, so 2>/dev/null leaves
# "Unable to access the API, forbidden" and "Failed to get info from Supervisor
# API" in the add-on log on every start — two ERROR lines for a probe whose
# failure is expected and handled three lines below.
if bashio::supervisor.ping >/dev/null 2>&1; then
    TZ_NAME="$(bashio::info.timezone 2>/dev/null || true)"
    if [ -n "${TZ_NAME}" ] && [ "${TZ_NAME}" != "null" ] \
       && [ -f "/usr/share/zoneinfo/${TZ_NAME}" ]; then
        export TZ="${TZ_NAME}"
        # tzlocal reads /etc/localtime before TZ, so set both or it keeps UTC.
        cp "/usr/share/zoneinfo/${TZ_NAME}" /etc/localtime
        echo "${TZ_NAME}" > /etc/timezone
        bashio::log.info "Timezone: ${TZ_NAME} (from Supervisor)"
    else
        # Not a failure on its own: the application asks Home Assistant for the
        # site timezone itself a moment later, over a path that works even when
        # every bashio call here returns "forbidden". Asserting an offset
        # schedule at this point is simply untrue, and it is the first thing a
        # user reads when something else goes wrong.
        bashio::log.info "Timezone not available to bashio — the app resolves it from Home Assistant."
    fi
fi

# ── MQTT broker ───────────────────────────────────────────────────────
# config.yaml declares `services: ["mqtt:want"]`, so the Supervisor hands us
# the broker's host, port, username and password for whichever broker add-on
# is configured. That was declared and then ignored: run.sh guessed
# "core-mosquitto" with no credentials, which fails outright on a broker that
# requires auth and is simply wrong for anyone running a different one.
#
# Ask the Supervisor first; fall back to the guess only when it has nothing,
# which is the case where the guess is the best available answer anyway.
# Same again: this probe prints "Unable to access the API, forbidden" to stdout
# when the Supervisor will not answer bashio, and the fallback below is the
# correct behaviour rather than an error.
if bashio::services.available "mqtt" >/dev/null 2>&1; then
    MQTT_HOST="$(bashio::services mqtt "host")"
    MQTT_PORT="$(bashio::services mqtt "port")"
    MQTT_USER="$(bashio::services mqtt "username")"
    MQTT_PASS="$(bashio::services mqtt "password")"
    bashio::log.info "MQTT: ${MQTT_HOST}:${MQTT_PORT} (from Supervisor)"

    # The Supervisor hands over the host even when it has no credentials to
    # give, so the connection is then attempted anonymously and the Mosquitto
    # add-on refuses it with "[code:135] Not authorized" — a message that names
    # neither the cause nor the fix. Say here whether credentials arrived.
    if [ -z "${MQTT_USER}" ]; then
        bashio::log.warning "MQTT: Home Assistant supplied no broker username."
        bashio::log.warning "MQTT: connecting anonymously, which the Mosquitto add-on"
        bashio::log.warning "MQTT: refuses by default. If the broker rejects us, set"
        bashio::log.warning "MQTT: mqtt_username / mqtt_password in Configuration."
    else
        bashio::log.info "MQTT: authenticating as ${MQTT_USER}"
    fi

    PATCHED=$(jq --arg h "${MQTT_HOST}" --arg p "${MQTT_PORT}" \
                 --arg u "${MQTT_USER}" --arg w "${MQTT_PASS}" '
        # A value the user typed wins: they may be pointing at a broker the
        # Supervisor does not know about.
        if (.mqtt_host // "") == "" then .mqtt_host = $h else . end
        | if (.mqtt_port // 0) == 0 then .mqtt_port = ($p | tonumber) else . end
        | if (.mqtt_username // "") == "" then .mqtt_username = $u else . end
        | if (.mqtt_password // "") == "" then .mqtt_password = $w else . end
    ' "${OPTIONS_FILE}")
    echo "${PATCHED}" > "${OPTIONS_FILE}"
elif [ -f "${OPTIONS_FILE}" ] && [ "$(jq -r '.mqtt_host // ""' "${OPTIONS_FILE}")" = "" ]; then
    # No broker known to the Supervisor. core-mosquitto is the usual answer,
    # and if it is wrong the UI reports the connection as down rather than
    # pretending otherwise.
    PATCHED=$(jq '.mqtt_host = "core-mosquitto"' "${OPTIONS_FILE}")
    echo "${PATCHED}" > "${OPTIONS_FILE}"
    # Likewise: the app asks the Supervisor for the broker itself, and on this
    # install that succeeds while bashio's identical request does not. Telling
    # someone to install Mosquitto when they are already running it sends them
    # after a problem they do not have.
    bashio::log.info "MQTT broker not available to bashio — the app asks the Supervisor directly."
    bashio::log.info "Falling back to core-mosquitto if it cannot."
fi

VERSION=$(cat /app/VERSION 2>/dev/null || echo "unknown")
bashio::log.info "FranklinWH HA Integrator v${VERSION} starting..."

# ── Launch app ────────────────────────────────────────────────
# Explicitly export PYTHONPATH — S6 overlay does not inherit Docker ENV vars
export PYTHONPATH=/app

exec uvicorn src.main:app \
    --host 0.0.0.0 \
    --port 8099 \
    --log-level info \
    --no-access-log
