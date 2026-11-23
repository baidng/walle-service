#!/usr/bin/env bash
set -euo pipefail

# 配置 EMQX 告警落库规则（部署文档 6.7，必须）：
#   创建 HTTP Connector / Action / Rule，把告警回调到 walle_api。
#
# 前置条件：
#   1. walle 服务已启动并正常运行；
#   2. etc/walle.conf 已存在，且包含 EMQX_API_KEY / EMQX_API_SECRET / WALLE_INGEST_TOKEN。
#
# 用法：
#   bash scripts/emqx_alarm_setup.sh
#
# 可选环境变量：
#   EMQX_HOST / EMQX_PORT     默认 127.0.0.1:18083
#   EMQX_API_KEY / EMQX_API_SECRET
#   WALLE_INGEST_TOKEN

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WALLE_CONF="$REPO_ROOT/etc/walle.conf"

EMQX_HOST="${EMQX_HOST:-127.0.0.1}"
EMQX_PORT="${EMQX_PORT:-18083}"
EMQX_BASE="http://$EMQX_HOST:$EMQX_PORT"
EMQX_API="$EMQX_BASE/api/v5"

CONNECTOR_ID="http:walle_alarm_http"
ACTION_ID="http:walle_alarm_hook"
RULE_ID="walle_alarm_persist"

EMQX_API_KEY="${EMQX_API_KEY:-}"
EMQX_API_SECRET="${EMQX_API_SECRET:-}"
WALLE_INGEST_TOKEN="${WALLE_INGEST_TOKEN:-}"

info() { printf '[emqx-alarm] %s\n' "$*"; }
fail() { printf '[emqx-alarm] 失败: %s\n' "$*" >&2; exit 1; }

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

is_placeholder() {
  case "$1" in
    ''|*'<'*|*'替换'*) return 0 ;;
    *) return 1 ;;
  esac
}

json_str() {
  jq -Rn --arg v "$1" '$v'
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

require_success() {
  local code="$1" action="$2" resp="$3"
  [[ "$code" =~ ^2 ]] || fail "$action 失败（HTTP $code）：$(api_error "$resp")"
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
  [ -n "$WALLE_INGEST_TOKEN" ] || WALLE_INGEST_TOKEN="$(read_conf_value WALLE_INGEST_TOKEN "$WALLE_CONF" || true)"

  if [ -z "$EMQX_API_KEY" ] || [ -z "$EMQX_API_SECRET" ]; then
    fail "etc/walle.conf 缺少 EMQX_API_KEY / EMQX_API_SECRET，请先完善 walle 配置"
  fi
  if is_placeholder "$WALLE_INGEST_TOKEN"; then
    fail "etc/walle.conf 缺少可用的 WALLE_INGEST_TOKEN，请先完善 walle 配置并重启 walle 服务"
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

ensure_connector() {
  local resp code create_payload update_payload token_json
  resp="$(new_tmp)"
  token_json="$(json_str "$WALLE_INGEST_TOKEN")"

  create_payload="$(printf '{"type":"http","name":"walle_alarm_http","url":"%s","pool_size":4,"headers":{"content-type":"application/json","x-walle-ingest":%s}}' \
    "http://127.0.0.1:9000" "$token_json")"
  update_payload="$(printf '{"url":"%s","pool_size":4,"headers":{"content-type":"application/json","x-walle-ingest":%s}}' \
    "http://127.0.0.1:9000" "$token_json")"

  code="$(api_call GET "/connectors/$CONNECTOR_ID" "" "$resp")"
  if [ "$code" = "200" ]; then
    code="$(api_call PUT "/connectors/$CONNECTOR_ID" "$update_payload" "$resp")"
    require_success "$code" "更新告警 HTTP Connector" "$resp"
    info "已更新 Connector：$CONNECTOR_ID"
    return 0
  fi
  [ "$code" = "404" ] || fail "查询 Connector 失败（HTTP $code）：$(api_error "$resp")"

  code="$(api_call POST "/connectors" "$create_payload" "$resp")"
  if [ "$code" = "409" ]; then
    code="$(api_call PUT "/connectors/$CONNECTOR_ID" "$update_payload" "$resp")"
    require_success "$code" "更新告警 HTTP Connector" "$resp"
    info "已更新 Connector：$CONNECTOR_ID"
    return 0
  fi
  require_success "$code" "创建告警 HTTP Connector" "$resp"
  info "已创建 Connector：$CONNECTOR_ID"
}

ensure_action() {
  local resp code create_payload update_payload token_json body_json
  resp="$(new_tmp)"
  token_json="$(json_str "$WALLE_INGEST_TOKEN")"
  body_json="$(json_str '{"topic":"${topic}","payload":${payload}}')"

  create_payload="$(printf '{"type":"http","name":"walle_alarm_hook","connector":"walle_alarm_http","parameters":{"path":"/api/internal/alarms","method":"post","headers":{"content-type":"application/json","x-walle-ingest":%s},"body":%s},"resource_opts":{"request_ttl":"10s"}}' \
    "$token_json" "$body_json")"
  update_payload="$(printf '{"connector":"walle_alarm_http","parameters":{"path":"/api/internal/alarms","method":"post","headers":{"content-type":"application/json","x-walle-ingest":%s},"body":%s},"resource_opts":{"request_ttl":"10s"}}' \
    "$token_json" "$body_json")"

  code="$(api_call GET "/actions/$ACTION_ID" "" "$resp")"
  if [ "$code" = "200" ]; then
    code="$(api_call PUT "/actions/$ACTION_ID" "$update_payload" "$resp")"
    require_success "$code" "更新告警 HTTP Action" "$resp"
    info "已更新 Action：$ACTION_ID"
    return 0
  fi
  [ "$code" = "404" ] || fail "查询 Action 失败（HTTP $code）：$(api_error "$resp")"

  code="$(api_call POST "/actions" "$create_payload" "$resp")"
  if [ "$code" = "409" ]; then
    code="$(api_call PUT "/actions/$ACTION_ID" "$update_payload" "$resp")"
    require_success "$code" "更新告警 HTTP Action" "$resp"
    info "已更新 Action：$ACTION_ID"
    return 0
  fi
  require_success "$code" "创建告警 HTTP Action" "$resp"
  info "已创建 Action：$ACTION_ID"
}

ensure_rule() {
  local resp code sql create_payload update_payload
  resp="$(new_tmp)"
  sql='SELECT topic, payload FROM "walle/+/device/+/alarm"'

  create_payload="$(printf '{"id":"walle_alarm_persist","sql":%s,"actions":["http:walle_alarm_hook"],"enable":true,"description":%s}' \
    "$(json_str "$sql")" "$(json_str "walle 告警落库：转发到 walle_api")")"
  update_payload="$(printf '{"sql":%s,"actions":["http:walle_alarm_hook"],"enable":true,"description":%s}' \
    "$(json_str "$sql")" "$(json_str "walle 告警落库：转发到 walle_api")")"

  code="$(api_call GET "/rules/$RULE_ID" "" "$resp")"
  if [ "$code" = "200" ]; then
    code="$(api_call PUT "/rules/$RULE_ID" "$update_payload" "$resp")"
    require_success "$code" "更新告警落库规则" "$resp"
    info "已更新 Rule：$RULE_ID"
    return 0
  fi
  [ "$code" = "404" ] || fail "查询 Rule 失败（HTTP $code）：$(api_error "$resp")"

  code="$(api_call POST "/rules" "$create_payload" "$resp")"
  if [ "$code" = "409" ]; then
    code="$(api_call PUT "/rules/$RULE_ID" "$update_payload" "$resp")"
    require_success "$code" "更新告警落库规则" "$resp"
    info "已更新 Rule：$RULE_ID"
    return 0
  fi
  require_success "$code" "创建告警落库规则" "$resp"
  info "已创建 Rule：$RULE_ID"
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

  info "1/2 配置告警 HTTP Connector / Action"
  ensure_connector
  ensure_action

  info "2/2 配置告警落库规则"
  ensure_rule

  cat <<EOF

============= 告警落库配置完成 =============
walle 服务 : 运行中
EMQX API   : $EMQX_API
Connector  : $CONNECTOR_ID
Action     : $ACTION_ID
Rule       : $RULE_ID（订阅 walle/+/device/+/alarm）
============================================
EOF
}

main "$@"
