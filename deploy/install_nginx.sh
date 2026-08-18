#!/usr/bin/env bash
# Install the production Nginx virtual host. Run certbot afterwards to add TLS.

set -Eeuo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN, for example DOMAIN=ibox-tv.com}"
WWW_DOMAIN="${WWW_DOMAIN:-www.$DOMAIN}"
APP_ROOT="${APP_ROOT:-/opt/ibox-tv}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTINATION=/etc/nginx/sites-available/ibox-tv.conf

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this installer as root (or through sudo)." >&2
    exit 1
fi
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ || "$DOMAIN" == .* || "$DOMAIN" == *..* ]]; then
    echo "DOMAIN is not a valid host name: $DOMAIN" >&2
    exit 1
fi

sed \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__WWW_DOMAIN__|$WWW_DOMAIN|g" \
    -e "s|__APP_ROOT__|$APP_ROOT|g" \
    "$SCRIPT_DIR/nginx/ibox-tv.conf" > "$DESTINATION"
ln -sfn "$DESTINATION" /etc/nginx/sites-enabled/ibox-tv.conf

nginx -t
systemctl reload nginx

cat <<EOF

Nginx is serving HTTP for $DOMAIN. After DNS reaches this VPS, issue TLS:
  certbot --nginx -d $DOMAIN -d $WWW_DOMAIN

If Cloudflare is proxying the hostname, temporarily make the record DNS-only
while using HTTP validation, or use a Cloudflare DNS challenge instead.
EOF
