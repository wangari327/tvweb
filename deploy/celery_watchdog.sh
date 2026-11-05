#!/usr/bin/env bash
# Restart the worker only when it has stopped consuming its two-minute
# heartbeat.  This deliberately does not flush Redis: queued work is kept for
# the replacement worker and expires according to each task's schedule.

set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/opt/ibox-tv}"
APP_DIR="${APP_DIR:-$APP_ROOT/current}"
VENV="${VENV:-$APP_ROOT/shared/venv}"
ENV_FILE="${ENV_FILE:-$APP_ROOT/shared/.env}"
CELERY_SERVICE="${CELERY_SERVICE:-ibox-tv-worker.service}"
STALE_SECONDS="${CELERY_WATCHDOG_STALE_SECONDS:-720}"

if [[ ! -r "$ENV_FILE" || ! -x "$VENV/bin/python" ]]; then
    echo "iBOX TV Celery watchdog is missing its environment or Python runtime." >&2
    exit 1
fi

set +u
set -a
source "$ENV_FILE"
set +a
set -u

set +e
"$VENV/bin/python" - "$STALE_SECONDS" <<'PY'
import os
import sys
import time

from redis import Redis

try:
    last_heartbeat = Redis.from_url(
        os.environ["REDIS_URL"], decode_responses=True, socket_connect_timeout=5
    ).get("celery:heartbeat:ingestion")
    stale_after = int(sys.argv[1])
    is_stale = not last_heartbeat or time.time() - int(last_heartbeat) > stale_after
    raise SystemExit(2 if is_stale else 0)
except (KeyError, ValueError):
    raise SystemExit(1)
except Exception as exc:
    print(f"Could not check iBOX TV Celery heartbeat: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
heartbeat_status=$?
set -e

if [[ "$heartbeat_status" -eq 2 ]]; then
    echo "iBOX TV ingestion heartbeat is stale; restarting $CELERY_SERVICE." >&2
    systemctl restart "$CELERY_SERVICE"
elif [[ "$heartbeat_status" -ne 0 ]]; then
    exit "$heartbeat_status"
fi
