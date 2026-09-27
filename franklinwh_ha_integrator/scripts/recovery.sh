#!/bin/bash
# Headless break-glass recovery shell wrapper
# Usage: ./scripts/recovery.sh disable-security

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
python3 "$SCRIPT_DIR/recovery.py" "$@"
