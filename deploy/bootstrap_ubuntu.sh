#!/usr/bin/env bash
# Prepare a clean Ubuntu 22.04/24.04 VPS for iBOX TV.
#
# Example:
#   curl -fsSLO https://raw.githubusercontent.com/wangari327/tvweb/main/deploy/bootstrap_ubuntu.sh
#   REPOSITORY_URL=https://github.com/wangari327/tvweb.git bash bootstrap_ubuntu.sh

set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/opt/ibox-tv}"
APP_USER="${APP_USER:-ibox}"
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/wangari327/tvweb.git}"
BRANCH="${BRANCH:-main}"
INSTALL_POSTGRES="${INSTALL_POSTGRES:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this bootstrap as root (or through sudo)." >&2
    exit 1
fi

if [[ "$APP_ROOT" == "/" || "$APP_ROOT" == *".."* ]]; then
    echo "APP_ROOT must be a specific safe directory, not $APP_ROOT." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    ca-certificates curl git build-essential \
    python3 python3-dev python3-venv \
    libpq-dev nginx redis-server \
    certbot python3-certbot-nginx

if [[ "$INSTALL_POSTGRES" == "1" ]]; then
    apt-get install -y postgresql postgresql-contrib
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -m 0755 -o "$APP_USER" -g "$APP_USER" \
    "$APP_ROOT" "$APP_ROOT/releases" "$APP_ROOT/shared"

SOURCE_DIR="$APP_ROOT/repository"
if [[ -d "$SOURCE_DIR/.git" ]]; then
    git -C "$SOURCE_DIR" fetch --depth=1 origin "$BRANCH"
else
    git clone --depth=1 --branch "$BRANCH" "$REPOSITORY_URL" "$SOURCE_DIR"
    chown -R "$APP_USER:$APP_USER" "$SOURCE_DIR"
fi

VENV="$APP_ROOT/shared/venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    runuser -u "$APP_USER" -- python3 -m venv "$VENV"
fi
runuser -u "$APP_USER" -- "$VENV/bin/pip" install --upgrade pip wheel
runuser -u "$APP_USER" -- "$VENV/bin/pip" install --requirement "$SOURCE_DIR/requirements.txt"

ENV_FILE="$APP_ROOT/shared/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    install -m 0640 -o "$APP_USER" -g "$APP_USER" "$SOURCE_DIR/.env.example" "$ENV_FILE"
fi

systemctl enable redis-server
systemctl start redis-server

cat <<EOF

Bootstrap complete. No application process has been started yet.

1. Edit secrets and service URLs:
   nano $ENV_FILE
2. Create the first immutable release and initialise the database:
   APP_ROOT=$APP_ROOT APP_USER=$APP_USER REPOSITORY_URL=$REPOSITORY_URL \\
     bash $SOURCE_DIR/deploy/deploy_release.sh
3. Install and start the systemd services:
   APP_ROOT=$APP_ROOT APP_USER=$APP_USER bash $SOURCE_DIR/deploy/install_systemd.sh
4. Configure Nginx, then issue a certificate:
   DOMAIN=example.com APP_ROOT=$APP_ROOT bash $SOURCE_DIR/deploy/install_nginx.sh

Set INSTALL_POSTGRES=1 before this script if this VPS, rather than a managed
PostgreSQL provider, should host the application database.
EOF
