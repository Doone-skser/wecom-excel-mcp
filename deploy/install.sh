#!/usr/bin/env bash
# Install or update the shared wecom-excel MCP server on a Debian / Ubuntu host, as root:
#
#   DOMAIN=wecom.example.com EMAIL=you@example.com ADMIN_USER=admin ./deploy/install.sh
#
# What it sets up (and what running it again updates):
#   - the Python package in /opt/wecom-excel/venv (built from this checkout; needs Rust, installed if missing)
#   - the systemd service wecom-excel-http on 127.0.0.1:8765, as the system user wecom-excel
#   - the landing page, llms.txt and the local client wecom-mcp.py in /var/www/wecom-excel
#   - nginx in front of it (/etc/nginx/conf.d/wecom-excel.conf) with a Let's Encrypt certificate
#   - the /admin login in /var/lib/wecom-excel/admin-password
#
# Settings (environment variables):
#   DOMAIN          required; must already resolve to this host, with ports 80 and 443 open
#   EMAIL           for Let's Encrypt expiry mail (optional)
#   ADMIN_USER      the /admin account, default admin
#   ADMIN_PASSWORD  its password; asked for when unset and no login exists yet (a random one without a terminal)
#   SKIP_NGINX=1    do not touch nginx or certificates (another proxy such as 宝塔 / Caddy serves the site;
#                   deploy/nginx.conf shows what it must do)
set -euo pipefail

: "${DOMAIN:?set DOMAIN, e.g. DOMAIN=wecom.example.com}"
ADMIN_USER="${ADMIN_USER:-admin}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/wecom-excel
WEB=/var/www/wecom-excel
STATE=/var/lib/wecom-excel
SVC_USER=wecom-excel
PLACEHOLDER=wecom.example.com

log() { printf '\n==> %s\n' "$*"; }
domain() { sed "s/${PLACEHOLDER//./\\.}/$DOMAIN/g" "$1"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "DOMAIN looks wrong: $DOMAIN" >&2; exit 1; }

log "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates build-essential
[ -n "${SKIP_NGINX:-}" ] || apt-get install -y -qq nginx certbot

if ! command -v uv >/dev/null; then
  log "uv (Python installer)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
if ! command -v cargo >/dev/null; then
  log "Rust (builds the decoder)"
  curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  . "$HOME/.cargo/env"
fi

log "service user and directories"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$APP" "$WEB" "$STATE"
chown "$SVC_USER:$SVC_USER" "$STATE"
chmod 700 "$STATE"

log "Python package -> $APP/venv"
# Python goes under $APP, not root's home, so the service user can run it.
export UV_PYTHON_INSTALL_DIR="$APP/python"
[ -x "$APP/venv/bin/python" ] || uv venv -q --python 3.12 "$APP/venv"
uv pip install -q --python "$APP/venv/bin/python" --reinstall-package wecom-excel-mcp "$REPO"

log "landing page -> $WEB"
for f in index.html llms.txt wecom-mcp.py; do
  domain "$REPO/deploy/site/$f" > "$WEB/$f"
done
chmod 644 "$WEB"/*

if [ ! -s "$STATE/admin-password" ] || [ -n "${ADMIN_PASSWORD:-}" ]; then
  log "/admin login for $ADMIN_USER"
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    if [ -t 0 ]; then
      read -rsp "password for /admin ($ADMIN_USER): " ADMIN_PASSWORD; echo
    else
      ADMIN_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
      echo "generated /admin password: $ADMIN_PASSWORD   (shown once; it is stored only as a hash)"
    fi
  fi
  hash="$(printf '%s\n' "$ADMIN_PASSWORD" | "$APP/venv/bin/wecom-excel-mcp" --hash-password)"
  install -m 600 -o "$SVC_USER" -g "$SVC_USER" /dev/null "$STATE/admin-password"
  printf '%s:%s\n' "$ADMIN_USER" "$hash" > "$STATE/admin-password"
fi

log "systemd service wecom-excel-http"
domain "$REPO/deploy/wecom-excel-http.service" > /etc/systemd/system/wecom-excel-http.service
systemctl daemon-reload
systemctl enable -q wecom-excel-http
systemctl restart wecom-excel-http

if [ -z "${SKIP_NGINX:-}" ]; then
  CONF=/etc/nginx/conf.d/wecom-excel.conf
  CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  if [ ! -f "$CERT" ]; then
    log "certificate for $DOMAIN (Let's Encrypt, webroot)"
    cat > "$CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    root $WEB;
    location ^~ /.well-known/acme-challenge/ { default_type text/plain; }
}
EOF
    nginx -t -q && systemctl reload nginx
    if [ -n "${EMAIL:-}" ]; then contact=(--email "$EMAIL"); else contact=(--register-unsafely-without-email); fi
    certbot certonly --webroot -w "$WEB" -d "$DOMAIN" --non-interactive --agree-tos "${contact[@]}"
  fi
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

  log "nginx -> $CONF"
  domain "$REPO/deploy/nginx.conf" > "$CONF"
  nginx -t -q
  systemctl reload nginx
fi

log "check"
for _ in $(seq 20); do
  curl -fs -o /dev/null http://127.0.0.1:8765/admin/login && break
  sleep 0.5
done
curl -fsS -o /dev/null http://127.0.0.1:8765/admin/login || { journalctl -u wecom-excel-http -n 30 --no-pager; exit 1; }
if [ -z "${SKIP_NGINX:-}" ]; then
  curl -fsS -o /dev/null "https://$DOMAIN/llms.txt"
  curl -fsS -o /dev/null "https://$DOMAIN/admin/login"
fi

cat <<EOF

Done.
  setup page (send it to users or their AI assistant): https://$DOMAIN/
  MCP endpoint:                                         https://$DOMAIN/mcp
  usage statistics:                                     https://$DOMAIN/admin  (account: $ADMIN_USER)
Update later with: git pull && sudo DOMAIN=$DOMAIN $REPO/deploy/install.sh
EOF
