#!/usr/bin/env bash
# FranklinWH HA Integrator — Add-on sync script
# Copies source from the dev repo into franklinwh_ha_integrator/ build context
# so HA Supervisor can build using COPY (not git clone).
#
# franklinwh-cloud is installed via PyPI (franklinwh-cloud==0.3.0) — no vendoring needed.
#
# Usage:
#   ./scripts/sync_addon.sh                 # sync from this repo root
#   ./scripts/sync_addon.sh /path/to/repo   # sync from explicit path

set -e

REPO_ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
ADDON_DIR="${REPO_ROOT}/franklinwh_ha_integrator"

echo ""
echo "FranklinWH HA Integrator — Add-on Sync"
echo "  From: ${REPO_ROOT}"
echo "  To:   ${ADDON_DIR}"
echo ""

# ── 1. Sync source directories ───────────────────────────────
# db/ is NOT optional: src/main.py reads db/seed/device_catalog_seed.json at
# startup. Omitting it is how the add-on shipped without a device catalog while
# compose worked fine — the failure is a silent skip, not an error.
for DIR in src scripts db; do
    rsync -a --delete \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        --exclude='.DS_Store' --exclude='*.db' --exclude='*.db-shm' --exclude='*.db-wal' \
        "${REPO_ROOT}/${DIR}/" "${ADDON_DIR}/${DIR}/"
    echo "✅ Synced ${DIR}/"
done

# ── 2. Sync individual files ─────────────────────────────────
for FILE in requirements.txt run.sh CHANGELOG.md VERSION; do
    cp "${REPO_ROOT}/${FILE}" "${ADDON_DIR}/${FILE}"
    echo "✅ Synced ${FILE}"
done

# ── 3. Verify ────────────────────────────────────────────────
echo ""
echo "Verification:"
ALL_OK=true
for CHECK in src scripts db requirements.txt run.sh CHANGELOG.md VERSION config.yaml Dockerfile; do
    TARGET="${ADDON_DIR}/${CHECK}"
    if [ -e "${TARGET}" ]; then
        echo "  ✅ ${CHECK}"
    else
        echo "  ❌ ${CHECK} MISSING"
        ALL_OK=false
    fi
done

if [ "${ALL_OK}" = false ]; then
    echo ""
    echo "❌ Sync verification FAILED — do not release"
    exit 1
fi

echo ""
echo "✅ Sync complete — franklinwh_ha_integrator/ is ready for release"
echo "   config.yaml and Dockerfile are add-on-owned and are NOT synced from the"
echo "   repo root: the manifest lives only here, and the add-on image builds"
echo "   from the Home Assistant base (bashio) rather than python:slim."
echo "   Next: bump version in franklinwh_ha_integrator/config.yaml, commit, push, tag"
echo ""
