#!/usr/bin/env bash
# Render and install the three systemd units used on a VPS.

set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/opt/ibox-tv}"
APP_USER="${APP_USER:-ibox}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this installer as root (or through sudo)." >&2
    exit 1
fi
if [[ ! -L "$APP_ROOT/current" || ! -f "$APP_ROOT/shared/.env" ]]; then
    echo "Create a release and shared .env before installing services." >&2
    exit 1
fi

render_unit() {
    local source="$1"
    local destination="$2"
    sed \
        -e "s|__APP_ROOT__|$APP_ROOT|g" \
        -e "s|__APP_USER__|$APP_USER|g" \
        "$source" > "$destination"
}

render_unit "$SCRIPT_DIR/systemd/ibox-tv-web.service" /etc/systemd/system/ibox-tv-web.service
render_unit "$SCRIPT_DIR/systemd/ibox-tv-worker.service" /etc/systemd/system/ibox-tv-worker.service
render_unit "$SCRIPT_DIR/systemd/ibox-tv-beat.service" /etc/systemd/system/ibox-tv-beat.service

systemctl daemon-reload
systemctl enable --now ibox-tv-web.service ibox-tv-worker.service ibox-tv-beat.service
systemctl --no-pager --full status ibox-tv-web.service
