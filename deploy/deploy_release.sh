#!/usr/bin/env bash
# Fetch main, build an immutable release, perform safe schema creation, then
# atomically switch /opt/ibox-tv/current. Existing services are restarted only
# after the new release is in place.

set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/opt/ibox-tv}"
APP_USER="${APP_USER:-ibox}"
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/wangari327/tvweb.git}"
BRANCH="${BRANCH:-main}"
SOURCE_DIR="${SOURCE_DIR:-$APP_ROOT/repository}"
VENV="$APP_ROOT/shared/venv"
ENV_FILE="$APP_ROOT/shared/.env"
CURRENT_LINK="$APP_ROOT/current"
NGINX_CACHE_DIR="${NGINX_CACHE_DIR:-}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run deployments as root so ownership and system services stay consistent." >&2
    exit 1
fi
if [[ ! -d "$SOURCE_DIR/.git" || ! -x "$VENV/bin/python" || ! -f "$ENV_FILE" ]]; then
    echo "Bootstrap is incomplete. Expected repository, virtualenv, and shared .env under $APP_ROOT." >&2
    exit 1
fi

exec 9>"/var/lock/ibox-tv-deploy.lock"
flock -n 9 || { echo "Another iBOX TV deployment is already running." >&2; exit 1; }

git -C "$SOURCE_DIR" fetch --depth=1 origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
COMMIT="$(git -C "$SOURCE_DIR" rev-parse "origin/$BRANCH")"
RELEASE_DIR="$APP_ROOT/releases/$COMMIT"

if [[ ! -d "$RELEASE_DIR/.git" ]]; then
    # Clone the freshly fetched local checkout so the exact commit selected
    # above is available even if main receives another push during deployment.
    git clone --quiet --no-checkout "$SOURCE_DIR" "$RELEASE_DIR"
    git -C "$RELEASE_DIR" checkout --quiet --detach "$COMMIT"
    chown -R "$APP_USER:$APP_USER" "$RELEASE_DIR"
fi

# The running processes only read the release-local copy; the canonical secret
# remains in shared/.env and is never tracked by Git.
install -m 0640 -o "$APP_USER" -g "$APP_USER" "$ENV_FILE" "$RELEASE_DIR/.env"

runuser -u "$APP_USER" -- "$VENV/bin/pip" install --requirement "$RELEASE_DIR/requirements.txt"
runuser -u "$APP_USER" -- env PYTHONPATH="$RELEASE_DIR" "$VENV/bin/python" "$RELEASE_DIR/scripts/initialize_database.py"

PREVIOUS_RELEASE=""
if [[ -L "$CURRENT_LINK" ]]; then
    PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
fi

rollback() {
    if [[ -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]]; then
        ln -sfn "$PREVIOUS_RELEASE" "$CURRENT_LINK"
        systemctl try-restart ibox-tv-web.service ibox-tv-worker.service ibox-tv-beat.service || true
        echo "Deployment failed; restored $PREVIOUS_RELEASE." >&2
    fi
}
trap rollback ERR

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

# A fresh deployment has no services until install_systemd.sh is run. Later
# releases restart each process, and verify that Gunicorn has answered locally.
if systemctl cat ibox-tv-web.service >/dev/null 2>&1; then
    systemctl restart ibox-tv-web.service ibox-tv-worker.service ibox-tv-beat.service

    healthy=0
    for _ in {1..15}; do
        if curl --fail --silent --max-time 3 http://127.0.0.1:8000/robots.txt >/dev/null; then
            healthy=1
            break
        fi
        sleep 1
    done
    if [[ "$healthy" != "1" ]]; then
        echo "Gunicorn did not pass its local health check." >&2
        exit 1
    fi
fi

# The supplied Nginx template intentionally has no dynamic proxy cache. This
# optional hook only flushes a pre-existing cache directory during migration
# from an older configuration; Redis is never flushed because it is Celery's
# broker as well as the application cache.
if [[ -n "$NGINX_CACHE_DIR" && -d "$NGINX_CACHE_DIR" ]]; then
    systemctl stop nginx
    find "$NGINX_CACHE_DIR" -mindepth 1 -delete
    systemctl start nginx
fi

trap - ERR
echo "Release $COMMIT is active at $CURRENT_LINK."
