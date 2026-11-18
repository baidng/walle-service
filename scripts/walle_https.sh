#!/usr/bin/env bash
# walle 一键 HTTPS
#
# 由 walle-api 通过 sudo 调用（需要 root），负责：
#   1. 用 HTTP-01 校验向 Let's Encrypt 申请证书（certbot webroot 模式）；
#   2. 在系统 nginx 中生成 walle-https 站点：80 端口做 ACME 校验 + 跳转，
#      443 端口反向代理到门户（默认 127.0.0.1:8088）；
#   3. 写入证书状态 /var/lib/walle/https.json 供后台读取；
#   4. 安装续期后自动 reload nginx 的 deploy hook。
#
# 用法：
#   walle_https.sh apply <domain> [email]
#   walle_https.sh renew <domain>
set -euo pipefail

PORTAL_PORT=8088
ACME_ROOT=/var/www/walle-acme
SITE_AVAILABLE=/etc/nginx/sites-available/walle-https.conf
SITE_ENABLED=/etc/nginx/sites-enabled/walle-https.conf
STATE_DIR=/var/lib/walle
STATE_FILE=$STATE_DIR/https.json
CERT_BASE=/etc/letsencrypt/live
NGINX_TEST_LOG=/tmp/walle-https-nginx-test.log

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
emit_error() { printf '{"ok":false,"error":"%s"}\n' "$(json_escape "$1")"; }
fail() { emit_error "$1"; exit 1; }

valid_domain() {
  [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}
valid_email() {
  [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$ ]]
}

reload_nginx() {
  nginx -t >"$NGINX_TEST_LOG" 2>&1 || return 1
  systemctl reload nginx >/dev/null 2>&1 || nginx -s reload >/dev/null 2>&1 || return 1
}

nginx_test_tail() {
  tail -n 3 "$NGINX_TEST_LOG" 2>/dev/null | tr '\n' ' ' || true
}

write_http_conf() {
  cat >"$SITE_AVAILABLE" <<EOF
# 由 walle 一键 HTTPS 自动生成，请勿手工修改。
server {
    listen 80;
    server_name $1;

    location /.well-known/acme-challenge/ {
        root $ACME_ROOT;
        default_type text/plain;
        try_files \$uri =404;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}
EOF
}

write_full_conf() {
  cat >"$SITE_AVAILABLE" <<EOF
# 由 walle 一键 HTTPS 自动生成，请勿手工修改。
map \$http_upgrade \$walle_connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name $1;

    location /.well-known/acme-challenge/ {
        root $ACME_ROOT;
        default_type text/plain;
        try_files \$uri =404;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name $1;

    ssl_certificate     $CERT_BASE/$1/fullchain.pem;
    ssl_certificate_key $CERT_BASE/$1/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:walle_ssl:10m;
    ssl_session_timeout 1h;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:$PORTAL_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$walle_connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
EOF
}

install_renew_hook() {
  local dir=/etc/letsencrypt/renewal-hooks/deploy
  mkdir -p "$dir"
  cat >"$dir/walle-nginx-reload.sh" <<'EOS'
#!/bin/sh
# 由 walle 一键 HTTPS 生成：证书续期后重载 nginx。
nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
EOS
  chmod 755 "$dir/walle-nginx-reload.sh"
}

cert_expiry() {
  local cert="$1" raw epoch iso
  raw=$(openssl x509 -in "$cert" -noout -enddate | sed 's/^notAfter=//')
  epoch=$(date -u -d "$raw" +%s)
  iso=$(date -u -d "$raw" +%Y-%m-%dT%H:%M:%SZ)
  printf '%s %s' "$iso" "$epoch"
}

write_state() {
  local domain="$1" email="$2" cert="$3" key="$4"
  local expiry iso epoch
  read -r iso epoch <<<"$(cert_expiry "$cert")"
  mkdir -p "$STATE_DIR"
  chmod 755 "$STATE_DIR"
  cat >"$STATE_FILE.tmp" <<EOF
{"domain":"$(json_escape "$domain")","email":"$(json_escape "$email")","cert":"$cert","key":"$key","not_after":"$iso","not_after_epoch":$epoch,"applied_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
  mv -f "$STATE_FILE.tmp" "$STATE_FILE"
  chmod 644 "$STATE_FILE"
}

emit_ok() {
  local domain="$1" cert="$2" key="$3"
  local expiry iso epoch
  read -r iso epoch <<<"$(cert_expiry "$cert")"
  printf '{"ok":true,"domain":"%s","cert":"%s","key":"%s","not_after":"%s","not_after_epoch":%s,"applied_at":"%s"}\n' \
    "$(json_escape "$domain")" "$cert" "$key" "$iso" "$epoch" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

[ "$(id -u)" -eq 0 ] || fail "需要 root 权限：请通过 sudo 调用本脚本"

ACTION=${1:-}
DOMAIN=${2:-}
EMAIL=${3:-}

if [ -n "$DOMAIN" ]; then
  valid_domain "$DOMAIN" || fail "域名格式不正确：$DOMAIN"
fi
if [ -n "$EMAIL" ]; then
  valid_email "$EMAIL" || fail "邮箱格式不正确：$EMAIL"
fi

CERT=$CERT_BASE/$DOMAIN/fullchain.pem
KEY=$CERT_BASE/$DOMAIN/privkey.pem

command -v nginx >/dev/null 2>&1 || fail "未找到 nginx，请先安装系统 nginx"
command -v systemctl >/dev/null 2>&1 || fail "未找到 systemctl"

case "$ACTION" in
  apply)
    command -v certbot >/dev/null 2>&1 || fail "未安装 certbot，请先在服务器执行：sudo apt-get install -y certbot"
    command -v openssl >/dev/null 2>&1 || fail "未找到 openssl"

    mkdir -p "$ACME_ROOT/.well-known/acme-challenge" "$STATE_DIR"
    chmod 755 "$ACME_ROOT"

    # 备份现有站点配置，失败时回滚
    BACKUP=""
    WAS_ENABLED=0
    if [ -f "$SITE_AVAILABLE" ]; then
      BACKUP=$(mktemp)
      cp -f "$SITE_AVAILABLE" "$BACKUP"
    fi
    if [ -L "$SITE_ENABLED" ]; then
      WAS_ENABLED=1
    fi

    restore_site() {
      if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
        cp -f "$BACKUP" "$SITE_AVAILABLE"
      else
        rm -f "$SITE_AVAILABLE"
      fi
      if [ "$WAS_ENABLED" = 1 ]; then
        ln -sf "$SITE_AVAILABLE" "$SITE_ENABLED"
      else
        rm -f "$SITE_ENABLED"
      fi
      reload_nginx || true
    }

    ln -sf "$SITE_AVAILABLE" "$SITE_ENABLED"

    if [ ! -f "$CERT" ]; then
      write_http_conf "$DOMAIN"
      if ! reload_nginx; then
        restore_site
        fail "nginx 加载配置失败（80 端口是否被占用？）：$(nginx_test_tail)"
      fi

      args=(certonly --webroot -w "$ACME_ROOT" -d "$DOMAIN" --non-interactive --agree-tos --keep-until-expiring)
      if [ -n "$EMAIL" ]; then
        args+=(--email "$EMAIL")
      else
        args+=(--register-unsafely-without-email)
      fi
      if ! certbot "${args[@]}" >/tmp/walle-https-certbot.log 2>&1; then
        restore_site
        fail "certbot 申请失败：$(tail -n 5 /tmp/walle-https-certbot.log | tr '\n' ' ')"
      fi
    fi

    [ -f "$CERT" ] && [ -f "$KEY" ] || { restore_site; fail "未找到证书文件 $CERT"; }

    write_full_conf "$DOMAIN"
    if ! reload_nginx; then
      restore_site
      fail "nginx 加载证书失败：$(nginx_test_tail)"
    fi

    install_renew_hook
    write_state "$DOMAIN" "$EMAIL" "$CERT" "$KEY"
    if [ -n "$BACKUP" ]; then
      rm -f "$BACKUP"
    fi
    emit_ok "$DOMAIN" "$CERT" "$KEY"
    ;;

  renew)
    command -v certbot >/dev/null 2>&1 || fail "未安装 certbot"
    [ -f "$CERT" ] || fail "证书不存在：$CERT"
    certbot renew --cert-name "$DOMAIN" --non-interactive >/tmp/walle-https-renew.log 2>&1 \
      || fail "certbot 续期失败：$(tail -n 5 /tmp/walle-https-renew.log | tr '\n' ' ')"
    reload_nginx || fail "证书已续期，但 nginx 重载失败：$(nginx_test_tail)"
    write_state "$DOMAIN" "$EMAIL" "$CERT" "$KEY"
    emit_ok "$DOMAIN" "$CERT" "$KEY"
    ;;

  *)
    fail "用法：$0 {apply <domain> [email]|renew <domain>}"
    ;;
esac
