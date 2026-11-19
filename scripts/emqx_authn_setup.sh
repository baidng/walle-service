#!/usr/bin/env bash
set -euo pipefail

# 创建 EMQX 内置数据库认证器（部署文档 6.5，必须）。
#
# 前置条件：
#   1. walle 服务已启动并正常运行；
#   2. etc/walle.conf 已存在，且包含 EMQX_API_KEY / EMQX_API_SECRET。
#
# 用法：
#   bash scripts/emqx_authn_setup.sh
#
# 可选环境变量：
#   EMQX_HOST / EMQX_PORT     默认 127.0.0.1:18083
#   EMQX_API_KEY / EMQX_API_SECRET

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WALLE_CONF="$REPO_ROOT/etc/walle.conf"

EMQX_HOST="${EMQX_HOST:-127.0.0.1}"
EMQX_PORT="${EMQX_PORT:-18083}"
EMQX_BASE="http://$EMQX_HOST:$EMQX_PORT"
EMQX_API="$EMQX_BASE/api/v5"

AUTH_ID="password_based:built_in_database"

EMQX_API_KEY="${EMQX_API_KEY:-}"
EMQX_API_SECRET="${EMQX_API_SECRET:-}"

info() { printf '[emqx-authn] %s\n' "$*"; }
fail() { printf '[emqx-authn] 失败: %s\n' "$*" >&2; exit 1; }

TMP_FILES=()
cleanup() {
  [ "${#TMP_FILES[@]}" -eq 0 ] || rm -f "${TMP_FILES[@]}"
}
trap cleanup EXIT

new_tmp() {
  local f
  f="$(mktemp)"
  TMP_FILES+=("$f")
  printf '%s' "$f"
}

read_conf_value() {
  local key="$1" file="$2" line value
  [ -f "$file" ] || return 1
  line="$(grep -E "^${key}=" "$file" | tail -n1 || true)"
  [ -n "$line" ] || return 1
  value="${line#*=}"
  value="${value%$'\r'}"
  printf '%s' "$value"
}

api_call() {
  local method="$1" path="$2" body="${3:-}" out="$4" code
  if [ -n "$body" ]; then
    code="$(curl -sS -o "$out" -w '%{http_code}' --connect-timeout 3 --max-time 20 \
      -X "$method" -u "$EMQX_API_KEY:$EMQX_API_SECRET" \
      -H 'Content-Type: application/json' -d "$body" "$EMQX_API$path" 2>/dev/null)" || code="000"
  else
    code="$(curl -sS -o "$out" -w '%{http_code}' --connect-timeout 3 --max-time 20 \
      -X "$method" -u "$EMQX_API_KEY:$EMQX_API_SECRET" "$EMQX_API$path" 2>/dev/null)" || code="000"
  fi
  printf '%s' "$code"
}

api_error() {
  local resp="$1" msg
  msg="$(jq -r 'if type == "object" then (.message // "") else "" end' "$resp" 2>/dev/null || true)"
  [ -n "$msg" ] || msg="$(head -c 200 "$resp" 2>/dev/null)"
  [ -n "$msg" ] || msg="无响应内容"
  printf '%s' "$msg"
}

emqx_running() {
  if systemctl is-active --quiet emqx 2>/dev/null; then
    return 0
  fi
  if systemctl is-active --quiet emqx-enterprise 2>/dev/null; then
    return 0
  fi
  curl -fsS -o /dev/null --max-time 3 "$EMQX_BASE/" 2>/dev/null
}

require_walle_ready() {
  if ! systemctl is-active --quiet walle 2>/dev/null; then
    fail "walle 服务未运行，请先启动：sudo systemctl start walle"
  fi
  info "walle 服务正在运行"

  if [ ! -s "$WALLE_CONF" ]; then
    fail "未找到或文件为空：$WALLE_CONF。请先执行 scripts/auto_gen_conf_service.sh 生成配置，或手动创建"
  fi
  info "etc/walle.conf 已就绪"
}

load_credentials() {
  [ -n "$EMQX_API_KEY" ] || EMQX_API_KEY="$(read_conf_value EMQX_API_KEY "$WALLE_CONF" || true)"
  [ -n "$EMQX_API_SECRET" ] || EMQX_API_SECRET="$(read_conf_value EMQX_API_SECRET "$WALLE_CONF" || true)"

  if [ -z "$EMQX_API_KEY" ] || [ -z "$EMQX_API_SECRET" ]; then
    fail "etc/walle.conf 缺少 EMQX_API_KEY / EMQX_API_SECRET，请先完善 walle 配置"
  fi
}

check_emqx_api() {
  local resp code
  resp="$(new_tmp)"
  code="$(api_call GET "/nodes" "" "$resp")"
  if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    fail "EMQX API 认证失败（HTTP $code），请检查 etc/walle.conf 中的 EMQX_API_KEY / EMQX_API_SECRET"
  fi
  [ "$code" = "200" ] || fail "无法访问 EMQX API（HTTP $code）：$(api_error "$resp")"
}

ensure_authenticator() {
  local resp code payload

  resp="$(new_tmp)"
  code="$(api_call GET "/authentication/$AUTH_ID" "" "$resp")"
  if [ "$code" = "200" ]; then
    info "认证器已存在，无需创建：$AUTH_ID"
    return 0
  fi
  [ "$code" = "404" ] || fail "查询认证器失败（HTTP $code）：$(api_error "$resp")"

  payload='{"mechanism":"password_based","backend":"built_in_database","user_id_type":"username","password_hash_algorithm":{"name":"sha256","salt_position":"suffix"}}'
  code="$(api_call POST "/authentication" "$payload" "$resp")"
  if [ "$code" = "409" ]; then
    info "认证器已存在，无需创建：$AUTH_ID"
    return 0
  fi
  [[ "$code" =~ ^2 ]] || fail "创建内置数据库认证器失败（HTTP $code）：$(api_error "$resp")"
  info "已创建认证器：$AUTH_ID"
}

main() {
  command -v curl >/dev/null 2>&1 || fail "未找到 curl，请先安装：sudo apt install -y curl"
  command -v jq >/dev/null 2>&1 || fail "未找到 jq，请先安装：sudo apt install -y jq"

  require_walle_ready

  if ! emqx_running; then
    fail "EMQX 未启动，请先执行：sudo systemctl start emqx"
  fi
  info "EMQX 正在运行（$EMQX_BASE）"

  load_credentials
  check_emqx_api

  info "配置内置数据库认证器"
  ensure_authenticator

  cat <<EOF

============== 认证器配置完成 ==============
walle 服务 : 运行中
EMQX API   : $EMQX_API
认证器     : $AUTH_ID
============================================
EOF
}

main "$@"
