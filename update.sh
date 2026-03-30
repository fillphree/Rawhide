#!/usr/bin/env bash
# Rawhide Image Viewer - Quick updater (code changes only)
# Use this after rawhide.py changes. Run sudo ./install.sh instead
# if Python packages or system dependencies have changed.
set -euo pipefail

APP_DIR="/usr/local/share/rawhide"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

if [[ $EUID -ne 0 ]]; then
    warn "Not running as root — attempting sudo..."
    exec sudo bash "$0" "$@"
fi

if [[ ! -d "$APP_DIR" ]]; then
    echo "Rawhide is not installed. Run sudo ./install.sh first." >&2
    exit 1
fi

info "Updating rawhide.py..."
cp "$SCRIPT_DIR/rawhide.py" "$APP_DIR/rawhide.py"
chmod 644 "$APP_DIR/rawhide.py"

info "Done. Changes are live — restart the app to see them."
