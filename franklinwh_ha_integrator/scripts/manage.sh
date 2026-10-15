#!/usr/bin/env bash
# FranklinWH HA Integrator — Management CLI
# Usage: ./scripts/manage.sh <command> [args]

set -e

APP_URL="${APP_URL:-http://localhost:8099}"
DATA_DIR="${DATA_DIR:-./data}"

usage() {
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  status              App version, environment, uptime, gateway health, MQTT"
    echo "  verify              Post-install verification (deps, modules, runtime)"
    echo "  diag                Cloud API connectivity test per gateway"
    echo "  config              Show safe config (no secrets)"
    echo "  logs [N]            Last N log lines (default: 20)"
    echo "  gateway list        List registered gateways"
    echo "  gateway add <id>    Register a gateway by short serial ID"
    echo "  snapshot            Take an immediate DB & Log tarball archive"
    echo "  restore <file>      Safely unpack a snapshot into /data"
    echo "  upgrade             Fetch updates and install safely"
    exit 1
}

check_app() {
    if ! curl -sf "${APP_URL}/api/health" > /dev/null 2>&1; then
        echo "❌ App not reachable at ${APP_URL}"
        echo "   Start with: uvicorn src.main:app --port 8099"
        exit 1
    fi
}

cmd_status() {
    check_app
    echo "=== FranklinWH HA Integrator Status ==="
    curl -sf "${APP_URL}/api/health" | python3 -m json.tool 2>/dev/null || \
        curl -sf "${APP_URL}/api/health"
}

cmd_diag() {
    check_app
    echo "=== Connectivity Diagnostics ==="
    curl -sf "${APP_URL}/api/diag" | python3 -m json.tool 2>/dev/null || \
        curl -sf "${APP_URL}/api/diag"
}

cmd_config() {
    check_app
    echo "=== Safe Config (no secrets) ==="
    curl -sf "${APP_URL}/api/config/safe" | python3 -m json.tool 2>/dev/null || \
        curl -sf "${APP_URL}/api/config/safe"
}

cmd_logs() {
    local n="${1:-20}"
    check_app
    echo "=== Last ${n} log entries ==="
    curl -sf "${APP_URL}/api/logs?limit=${n}" | python3 -m json.tool 2>/dev/null || \
        curl -sf "${APP_URL}/api/logs?limit=${n}"
}

cmd_gateway() {
    local subcmd="${1:-list}"
    case "${subcmd}" in
        list)
            check_app
            echo "=== Registered Gateways ==="
            curl -sf "${APP_URL}/api/gateways" | python3 -m json.tool 2>/dev/null || \
                curl -sf "${APP_URL}/api/gateways"
            ;;
        add)
            local gw_id="${2:-}"
            if [ -z "${gw_id}" ]; then
                echo "Usage: $0 gateway add <short_id>"
                exit 1
            fi
            check_app
            curl -sf -X POST "${APP_URL}/api/gateways" \
                -H "Content-Type: application/json" \
                -d "{\"short_id\": \"${gw_id}\"}" | python3 -m json.tool
            ;;
        *)
            echo "Unknown gateway subcommand: ${subcmd}"
            usage
            ;;
    esac
}

cmd_snapshot() {
    echo "=== Manual Snapshot Generation ==="
    mkdir -p "${DATA_DIR}/backups"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local archive_name="${DATA_DIR}/backups/snapshot_MANUAL_${timestamp}.tar.gz"
    
    # Safely tar without strictly locking
    tar -czf "${archive_name}" -C "${DATA_DIR}" config.db franklinwh.log 2>/dev/null || true
    if [ -f "${archive_name}" ]; then
        echo "✅ Snapshot created: ${archive_name}"
    else
        echo "❌ Snapshot failed"
        exit 1
    fi
}

cmd_restore() {
    local target="$1"
    if [ -z "${target}" ]; then
        echo "Usage: $0 restore <path_to_snapshot.tar.gz>"
        exit 1
    fi
    if [ ! -f "${target}" ]; then
        echo "❌ File not found: ${target}"
        exit 1
    fi
    echo "=== ⚠️ RESTORING SNAPSHOT ==="
    echo "WARNING: This will overwrite your current config.db and logs!"
    read -p "Are you sure? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Extracting ${target} into ${DATA_DIR}..."
        tar -xzf "${target}" -C "${DATA_DIR}"
        echo "✅ Restoration complete! Please restart your FranklinWH HA Integrator instance."
    else
        echo "Aborted."
    fi
}

cmd_upgrade() {
    echo "=== System Upgrade ==="
    echo "[1/3] Launching pre-flight snapshot..."
    cmd_snapshot

    echo "[2/3] Checking environment..."
    if [ -f "/.dockerenv" ] || grep -q 'docker' /proc/1/cgroup 2>/dev/null; then
        echo "⚠️ Docker container environment detected."
        echo "For Docker instances, you must update via the host system:"
        echo "    docker compose pull && docker compose up -d"
        echo "Upgrade wrapper safely terminating to prevent container divergence."
        exit 0
    fi

    if [ ! -d ".git" ]; then
        echo "⚠️ Git repository not found (likely downloaded as a zip rather than cloned)."
        echo "Cannot perform automated 'git pull'."
        exit 1
    fi

    echo "Running git pull..."
    git pull origin main

    echo "[3/3] Updating pip dependencies..."
    if [ -d ".venv" ]; then
        .venv/bin/pip install -r requirements.txt
    else
        pip3 install -r requirements.txt
    fi
    
    echo ""
    echo "✅ Upgrade complete! Please restart your Uvicorn or systemd service."
}

# Main
COMMAND="${1:-}"
shift || true

case "${COMMAND}" in
    status)   cmd_status ;;
    verify)   python3 /app/scripts/verify_install.py ;;
    diag)     cmd_diag ;;
    config)   cmd_config ;;
    logs)     cmd_logs "$@" ;;
    gateway)  cmd_gateway "$@" ;;
    snapshot) cmd_snapshot ;;
    restore)  cmd_restore "$1" ;;
    upgrade)  cmd_upgrade ;;
    *)        usage ;;
esac
