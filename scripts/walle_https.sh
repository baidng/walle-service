#!/usr/bin/env bash
# walle 一键 HTTPS
#
# 由 walle-api 通过 sudo 调用（需要 root），负责：
#   1. 用 HTTP-01 校验向 Let's Encrypt 申请证书（certbot webroot 模式）；
#   2. 在系统 nginx 中生成 walle-https 站点：80 端口做 ACME 校验 + 跳转，
#      443 端口反向代理到门户（默认 127.0.0.1:8088）；
#   3. 把同一张证书复制给 EMQX，并更新 ssl:default（8883）/ wss:default（8084）
#      监听器证书；
#   4. 写入证书状态 /var/lib/walle/https.json 供后台读取；
#   5. 安装续期后同步 EMQX 证书并 reload nginx 的 deploy hook。
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
EMQX_CERT_DIR=/etc/emqx/certs
EMQX_CERT_FILE=$EMQX_CERT_DIR/walle-fullchain.pem
EMQX_KEY_FILE=$EMQX_CERT_DIR/walle-privkey.pem
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

emqx_credentials() {
  local file key="" secret=""
  if [ -n "${EMQX_API_KEY:-}" ] && [ -n "${EMQX_API_SECRET:-}" ]; then
    printf '%s\n%s\n' "$EMQX_API_KEY" "$EMQX_API_SECRET"
    return 0
  fi
  for file in /etc/walle/walle.conf "$REPO_ROOT/etc/walle.conf"; do
    [ -r "$file" ] || continue
    key=$(grep -E '^EMQX_API_KEY=' "$file" | tail -n1 | cut -d= -f2- || true)
    secret=$(grep -E '^EMQX_API_SECRET=' "$file" | tail -n1 | cut -d= -f2- || true)
    if [ -n "$key" ] && [ -n "$secret" ]; then
      printf '%s\n%s\n' "$key" "$secret"
      return 0
    fi
  done
  return 1
}

copy_emqx_certs() {
  local cert="$1" key="$2"
  mkdir -p "$EMQX_CERT_DIR" || return 1
  cp -Lf "$cert" "$EMQX_CERT_FILE" || return 1
  cp -Lf "$key" "$EMQX_KEY_FILE" || return 1
  if id emqx >/dev/null 2>&1; then
    chown emqx:emqx "$EMQX_CERT_FILE" "$EMQX_KEY_FILE" 2>/dev/null || return 1
  fi
  chmod 644 "$EMQX_CERT_FILE" || return 1
  chmod 600 "$EMQX_KEY_FILE" || return 1
}

# 读取监听器当前配置（保留端口等设置），只替换证书路径后 PUT 回 EMQX。
emqx_configure_listener() {
  EMQX_CERT_PATH="$EMQX_CERT_FILE" EMQX_KEY_PATH="$EMQX_KEY_FILE" \
  EMQX_API_KEY="$2" EMQX_API_SECRET="$3" \
  python3 - "$1" <<'PY'
import base64
import copy
import json
import os
import sys
import urllib.error
import urllib.request

listener = sys.argv[1]
api_key = os.environ["EMQX_API_KEY"]
api_secret = os.environ["EMQX_API_SECRET"]
certfile = os.environ["EMQX_CERT_PATH"]
keyfile = os.environ["EMQX_KEY_PATH"]
url = f"http://127.0.0.1:18083/api/v5/listeners/{listener}"
auth = "Basic " + base64.b64encode(
    f"{api_key}:{api_secret}".encode("utf-8")
).decode("ascii")


def request(method, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read() or b"{}")


try:
    payload = copy.deepcopy(request("GET"))
    current_ssl = payload.get("ssl_options") or {}
    if (
        payload.get("enable")
        and current_ssl.get("certfile") == certfile
        and current_ssl.get("keyfile") == keyfile
    ):
        sys.exit(0)
    for field in (
        "running",
        "current_connections",
        "resolved_address",
        "resolved_address_from",
    ):
        payload.pop(field, None)
    payload["enable"] = True
    ssl_options = payload.setdefault("ssl_options", {})
    ssl_options["certfile"] = certfile
    ssl_options["keyfile"] = keyfile
    request("PUT", payload)
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace").replace("\n", " ")[-300:]
    print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    print(str(exc).replace("\n", " ")[-300:], file=sys.stderr)
    sys.exit(1)
PY
}

# 把证书同步给 EMQX；失败只记录，不影响门户 HTTPS。输出一行 JSON。
setup_emqx_tls() {
  local cert="$1" key="$2"
  local creds api_key api_secret listener output
  local configured="" errors=""

  if ! command -v emqx >/dev/null 2>&1; then
    printf '{"ok":false,"skipped":true,"error":"未安装 EMQX，跳过 8883/8084 证书同步"}\n'
    return 0
  fi
  if ! systemctl is-active --quiet emqx 2>/dev/null; then
    printf '{"ok":false,"skipped":true,"error":"EMQX 未运行，跳过 8883/8084 证书同步"}\n'
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    printf '{"ok":false,"error":"缺少 python3，无法配置 EMQX 监听器"}\n'
    return 0
  fi
  if ! creds=$(emqx_credentials); then
    printf '{"ok":false,"error":"未找到 EMQX API 凭据，无法配置 8883/8084"}\n'
    return 0
  fi
  api_key=$(printf '%s\n' "$creds" | sed -n '1p')
  api_secret=$(printf '%s\n' "$creds" | sed -n '2p')

  if ! copy_emqx_certs "$cert" "$key"; then
    printf '{"ok":false,"error":"复制证书到 %s 失败"}\n' "$EMQX_CERT_DIR"
    return 0
  fi

  for listener in ssl:default wss:default; do
    if output=$(emqx_configure_listener "$listener" "$api_key" "$api_secret" 2>&1); then
      configured="${configured:+$configured,}\"$listener\""
    else
      output=$(printf '%s' "$output" | tr '\n\r\t' '   ')
      errors="${errors:+$errors; }$listener: $output"
    fi
  done

  if [ -z "$errors" ]; then
    printf '{"ok":true,"listeners":[%s],"cert":"%s"}\n' "$configured" "$EMQX_CERT_FILE"
  else
    printf '{"ok":false,"listeners":[%s],"error":"%s"}\n' \
      "$configured" "$(json_escape "$errors")"
  fi
}

run_emqx_sync() {
  local json
  json=$(setup_emqx_tls "$1" "$2" 2>/dev/null | tail -n 1)
  case "$json" in
    \{*\}) printf '%s' "$json" ;;
    *) printf '{"ok":false,"error":"EMQX 证书同步失败，请查看脚本输出"}' ;;
  esac
}

install_renew_hook() {
  local domain="$1"
  local dir=/etc/letsencrypt/renewal-hooks/deploy
  mkdir -p "$dir"
  cat >"$dir/walle-nginx-reload.sh" <<EOS
#!/bin/sh
# 由 walle 一键 HTTPS 生成：证书续期后同步 EMQX 8883/8084 并重载 nginx。
case "\${RENEWED_LINEAGE:-}" in
  ""|*/$domain) ;;
  *) exit 0 ;;
esac
CERT="$CERT_BASE/$domain/fullchain.pem"
KEY="$CERT_BASE/$domain/privkey.pem"
if [ -f "\$CERT" ] && [ -f "\$KEY" ]; then
  if command -v emqx >/dev/null 2>&1 && systemctl is-active --quiet emqx 2>/dev/null; then
    mkdir -p "$EMQX_CERT_DIR"
    cp -Lf "\$CERT" "$EMQX_CERT_FILE" && cp -Lf "\$KEY" "$EMQX_KEY_FILE" || true
    if id emqx >/dev/null 2>&1; then
      chown emqx:emqx "$EMQX_CERT_FILE" "$EMQX_KEY_FILE" 2>/dev/null || true
    fi
    chmod 644 "$EMQX_CERT_FILE" 2>/dev/null || true
    chmod 600 "$EMQX_KEY_FILE" 2>/dev/null || true
    emqx ctl pem_cache clean all >/dev/null 2>&1 || true
    emqx ctl listeners restart ssl:default >/dev/null 2>&1 || true
    emqx ctl listeners restart wss:default >/dev/null 2>&1 || true
  fi
fi
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
  local domain="$1" email="$2" cert="$3" key="$4" emqx="${5:-}"
  local iso epoch
  read -r iso epoch <<<"$(cert_expiry "$cert")"
  [ -n "$emqx" ] || emqx='{"ok":false,"error":"未配置"}'
  mkdir -p "$STATE_DIR"
  chmod 755 "$STATE_DIR"
  cat >"$STATE_FILE.tmp" <<EOF
{"domain":"$(json_escape "$domain")","email":"$(json_escape "$email")","cert":"$cert","key":"$key","not_after":"$iso","not_after_epoch":$epoch,"applied_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","emqx":$emqx}
EOF
  mv -f "$STATE_FILE.tmp" "$STATE_FILE"
  chmod 644 "$STATE_FILE"
}

emit_ok() {
  local domain="$1" cert="$2" key="$3" emqx="${4:-}"
  local iso epoch
  read -r iso epoch <<<"$(cert_expiry "$cert")"
  [ -n "$emqx" ] || emqx='{"ok":false,"error":"未配置"}'
  printf '{"ok":true,"domain":"%s","cert":"%s","key":"%s","not_after":"%s","not_after_epoch":%s,"applied_at":"%s","emqx":%s}\n' \
    "$(json_escape "$domain")" "$cert" "$key" "$iso" "$epoch" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$emqx"
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

    install_renew_hook "$DOMAIN"
    EMQX_RESULT=$(run_emqx_sync "$CERT" "$KEY")
    write_state "$DOMAIN" "$EMAIL" "$CERT" "$KEY" "$EMQX_RESULT"
    if [ -n "$BACKUP" ]; then
      rm -f "$BACKUP"
    fi
    emit_ok "$DOMAIN" "$CERT" "$KEY" "$EMQX_RESULT"
    ;;

  renew)
    command -v certbot >/dev/null 2>&1 || fail "未安装 certbot"
    [ -f "$CERT" ] || fail "证书不存在：$CERT"
    certbot renew --cert-name "$DOMAIN" --non-interactive >/tmp/walle-https-renew.log 2>&1 \
      || fail "certbot 续期失败：$(tail -n 5 /tmp/walle-https-renew.log | tr '\n' ' ')"
    reload_nginx || fail "证书已续期，但 nginx 重载失败：$(nginx_test_tail)"
    install_renew_hook "$DOMAIN"
    EMQX_RESULT=$(run_emqx_sync "$CERT" "$KEY")
    write_state "$DOMAIN" "$EMAIL" "$CERT" "$KEY" "$EMQX_RESULT"
    emit_ok "$DOMAIN" "$CERT" "$KEY" "$EMQX_RESULT"
    ;;

  *)
    fail "用法：$0 {apply <domain> [email]|renew <domain>}"
    ;;
esac
