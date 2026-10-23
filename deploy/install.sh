#!/usr/bin/env bash
# Install or update the shared wecom-excel MCP server on a Linux host with systemd, as root. Nothing is
# compiled: it downloads the prebuilt executable from the GitHub release.
#
#   curl -fsSL https://github.com/Doone-skser/wecom-excel-mcp/releases/latest/download/install.sh | sudo bash
#
# By default the server listens on port 8765 of every interface (http://<server ip>:8765/). Settings
# (environment variables, e.g. `| sudo PORT=9000 bash`):
#   PORT            the port, default 8765
#   DOMAIN          serve https://DOMAIN/ through nginx with a Let's Encrypt certificate instead (Debian / Ubuntu;
#                   the domain must resolve to this host, ports 80 and 443 open); the port is then local only
#   EMAIL           for Let's Encrypt expiry mail (with DOMAIN, optional)
#   SKIP_NGINX=1    with DOMAIN: your own proxy (宝塔, Caddy, ...) forwards https://DOMAIN to 127.0.0.1:PORT
#   ADMIN_USER      the /admin account, default admin
#   ADMIN_PASSWORD  its password; when unset and no login exists yet, a random one is made and shown once
#   VERSION         a release tag such as v0.2.0, default the latest
#   GITHUB_PROXY    a prefix for downloading the executable when github.com is slow, e.g. https://ghfast.top/;
#                   the checksum still comes from github.com itself, so the proxy cannot swap the executable
#   BINARY          install this local executable instead of downloading one
set -euo pipefail

REPO_URL=https://github.com/Doone-skser/wecom-excel-mcp
PORT="${PORT:-8765}"
DOMAIN="${DOMAIN:-}"
ADMIN_USER="${ADMIN_USER:-admin}"
APP=/opt/wecom-excel
STATE=/var/lib/wecom-excel
SVC=wecom-excel-http
SVC_USER=wecom-excel

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
command -v systemctl >/dev/null || die "needs systemd"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "PORT looks wrong: $PORT"
[ -z "$DOMAIN" ] || [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "DOMAIN looks wrong: $DOMAIN"
case "$(uname -m)" in
  x86_64 | amd64) ARCH=x86_64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) die "no prebuilt executable for $(uname -m); see DEPLOY.md to build from source" ;;
esac
command -v curl >/dev/null || die "needs curl"

log "executable -> $APP/wecom-excel-mcp"
mkdir -p "$APP"
if [ -n "${BINARY:-}" ]; then
  install -m 755 "$BINARY" "$APP/wecom-excel-mcp.new"
else
  name="wecom-excel-mcp-linux-$ARCH"
  if [ -n "${VERSION:-}" ]; then base="$REPO_URL/releases/download/$VERSION"; else base="$REPO_URL/releases/latest/download"; fi
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  # The checksums always come straight from github.com (small; reachable where the big download is slow),
  # never through GITHUB_PROXY: a proxy that served both could swap the executable unnoticed.
  curl -fsSL --retry 3 --max-time 60 -o "$tmp/SHA256SUMS" "$base/SHA256SUMS" \
    || die "cannot fetch $base/SHA256SUMS from github.com (needed to check the download)"
  curl -fL --retry 3 --progress-bar -o "$tmp/$name" "${GITHUB_PROXY:-}$base/$name"
  (cd "$tmp" && grep " $name\$" SHA256SUMS | sha256sum -c --quiet -) || die "checksum mismatch for $name"
  install -m 755 "$tmp/$name" "$APP/wecom-excel-mcp.new"
fi
"$APP/wecom-excel-mcp.new" --version >/dev/null || die "the executable does not run on this system"
mv -f "$APP/wecom-excel-mcp.new" "$APP/wecom-excel-mcp"
"$APP/wecom-excel-mcp" --version

log "service user and data directory $STATE"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
  || useradd --system --no-create-home --shell /sbin/nologin "$SVC_USER"
mkdir -p "$STATE"
chown "$SVC_USER:$SVC_USER" "$STATE"
chmod 700 "$STATE"

shown=""
if [ ! -s "$STATE/admin-password" ] || [ -n "${ADMIN_PASSWORD:-}" ]; then
  log "/admin login for $ADMIN_USER"
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    ADMIN_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
    shown="$ADMIN_PASSWORD"
  fi
  printf '%s\n' "$ADMIN_PASSWORD" | "$APP/wecom-excel-mcp" passwd --user "$ADMIN_USER" --file "$STATE/admin-password" >/dev/null
  chown "$SVC_USER:$SVC_USER" "$STATE/admin-password"
fi

if [ -n "$DOMAIN" ]; then
  HOST=127.0.0.1
  URL="https://$DOMAIN"
  PUBLIC="--public-url $URL"
else
  HOST=0.0.0.0
  ip="$(curl -fsS --max-time 5 https://ipinfo.io/ip 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  URL="http://${ip:-<server ip>}:$PORT"
  PUBLIC=""
fi

log "systemd service $SVC ($HOST:$PORT)"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=wecom-excel MCP server (cookies come from each caller, never stored)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
ExecStart=$APP/wecom-excel-mcp serve --host $HOST --port $PORT --data-dir $STATE $PUBLIC
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
StateDirectory=wecom-excel

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SVC"
systemctl restart "$SVC"

if [ -z "$DOMAIN" ]; then
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    log "firewall: ufw allow $PORT/tcp"; ufw allow "$PORT/tcp" >/dev/null
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    log "firewall: firewalld port $PORT/tcp"; firewall-cmd -q --permanent --add-port="$PORT/tcp"; firewall-cmd -q --reload
  fi
elif [ -z "${SKIP_NGINX:-}" ]; then
  command -v apt-get >/dev/null || die "the nginx setup needs Debian / Ubuntu; use SKIP_NGINX=1 and your own proxy"
  log "nginx and certbot"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq nginx certbot >/dev/null
  CONF=/etc/nginx/conf.d/wecom-excel.conf
  ACME=/var/www/wecom-excel-acme
  mkdir -p "$ACME"
  if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    log "certificate for $DOMAIN (Let's Encrypt)"
    cat > "$CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location ^~ /.well-known/acme-challenge/ { root $ACME; default_type text/plain; }
}
EOF
    nginx -t -q
    systemctl reload nginx || systemctl start nginx
    if [ -n "${EMAIL:-}" ]; then contact=(--email "$EMAIL"); else contact=(--register-unsafely-without-email); fi
    certbot certonly --webroot -w "$ACME" -d "$DOMAIN" --non-interactive --agree-tos "${contact[@]}"
  fi
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

  log "nginx -> $CONF"
  cat > "$CONF" <<EOF
# wecom-excel (written by deploy/install.sh). The server itself serves the setup page, /mcp and /admin.
# Callers send their own WeCom cookie in X-Wecom-Cookie; the default log format has no headers.
limit_req_zone \$binary_remote_addr zone=wecom_mcp:10m rate=10r/s;

server {
    listen 80;
    server_name $DOMAIN;
    location ^~ /.well-known/acme-challenge/ { root $ACME; default_type text/plain; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name $DOMAIN;
    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:wecom_ssl:10m;
    add_header Strict-Transport-Security "max-age=31536000" always;

    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header Connection "";

    location = /mcp {
        # a browser opening /mcp gets the setup page instead of an endless event stream
        if (\$http_accept ~* "text/html") { return 302 /; }
        limit_req zone=wecom_mcp burst=40 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:$PORT;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        client_max_body_size 1m;
    }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
    }
}
EOF
  nginx -t -q
  systemctl reload nginx
fi

log "check"
ok=""
for _ in $(seq 30); do
  if curl -fs -o /dev/null "http://127.0.0.1:$PORT/admin/login"; then ok=1; break; fi
  sleep 1
done
[ -n "$ok" ] || { journalctl -u "$SVC" -n 30 --no-pager; die "the service did not start"; }
if [ -n "$DOMAIN" ] && [ -z "${SKIP_NGINX:-}" ]; then
  curl -fsS -o /dev/null "$URL/llms.txt" || die "$URL/llms.txt does not answer"
fi

cat <<EOF

Done. $("$APP/wecom-excel-mcp" --version)
  setup page (send it to users or their AI assistant): $URL/
  MCP endpoint:                                         $URL/mcp
  usage statistics:                                     $URL/admin   account: $ADMIN_USER${shown:+   password: $shown  (shown once)}
EOF
if [ -z "$DOMAIN" ]; then
  cat <<EOF

  Open TCP port $PORT in your cloud provider's security group if it is not open yet.
  Without HTTPS, cookies cross the network unencrypted: use this on a trusted network, or set DOMAIN.
EOF
elif [ -n "${SKIP_NGINX:-}" ]; then
  echo "  Point your proxy for $DOMAIN at http://127.0.0.1:$PORT (pass Host, X-Real-IP, X-Forwarded-Proto)."
fi
echo "  Update: run the same command again.  New /admin password: ADMIN_PASSWORD='...' with the same command."
