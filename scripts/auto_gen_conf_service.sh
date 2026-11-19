#!/usr/bin/env bash
set -euo pipefail

# 一键生成 walle-service 的运行配置与 systemd 单元：
#   etc/srs.conf          <- etc/srs.conf.example
#   etc/srs.service       <- etc/srs.service.example
#   etc/nginx.conf        <- etc/nginx.conf.example（自动创建 EMQX API Key 并写入 Basic Auth）
#   etc/walle.conf        <- etc/walle.conf.example（复用同一组 EMQX API Key/Secret）
#   etc/walle.service     <- etc/walle.service.example
#
# 前置条件：
#   1. nginx、EMQX 已通过软件包安装；
#   2. SRS 已按 docs/walle_deployment_require.md 第 5 节编译完成。
#
# 用法：
#   bash scripts/auto_gen_conf_service.sh
#
# 可选环境变量：EMQX_HOST / EMQX_PORT（默认 127.0.0.1:18083）

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ETC_DIR="$REPO_ROOT/etc"

SRS_CONF_TPL="$ETC_DIR/srs.conf.example"
SRS_CONF_OUT="$ETC_DIR/srs.conf"
SRS_SERVICE_TPL="$ETC_DIR/srs.service.example"
SRS_SERVICE_OUT="$ETC_DIR/srs.service"
NGINX_TPL="$ETC_DIR/nginx.conf.example"
NGINX_OUT="$ETC_DIR/nginx.conf"
WALLE_TPL="$ETC_DIR/walle.conf.example"
WALLE_OUT="$ETC_DIR/walle.conf"
WALLE_SERVICE_TPL="$ETC_DIR/walle.service.example"
WALLE_SERVICE_OUT="$ETC_DIR/walle.service"

SRS_BIN="$REPO_ROOT/thirdparty/srs/trunk/objs/srs"
EMQX_HOST="${EMQX_HOST:-127.0.0.1}"
EMQX_PORT="${EMQX_PORT:-18083}"
EMQX_BASE="http://$EMQX_HOST:$EMQX_PORT"
EMQX_API="$EMQX_BASE/api/v5"

SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || id -gn)"

PUBLIC_IP=""
EMQX_ADMIN_PASSWORD=""
EMQX_TOKEN=""
EMQX_LEGACY_AUTH=0
EMQX_API_KEY=""
EMQX_API_SECRET=""

info() { printf '[gen] %s\n' "$*"; }
warn() { printf '[gen] 警告: %s\n' "$*" >&2; }
fail() { printf '[gen] 失败: %s\n' "$*" >&2; exit 1; }

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

escape_sed_repl() {
  printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

backup_if_exists() {
  local file="$1" backup
  [ -f "$file" ] || return 0
  backup="$file.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$file" "$backup"
  info "已备份 $(basename "$file") -> $(basename "$backup")"
}

package_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
}

# ---------- 1. 环境检查 ----------

check_required_packages() {
  command -v dpkg-query >/dev/null 2>&1 || fail "未找到 dpkg-query，无法检查软件包安装状态"
  package_installed nginx || fail "未检测到 nginx 软件包，请先安装：sudo apt install -y nginx"
  if ! package_installed emqx-enterprise && ! package_installed emqx; then
    fail "未检测到 EMQX 软件包，请参考 docs/walle_deployment_require.md 第 6 节安装 EMQX"
  fi
  info "nginx、EMQX 软件包已安装"
}

confirm_srs() {
  local answer=""

  if [ -t 0 ] && command -v whiptail >/dev/null 2>&1; then
    local -a args=(--title "检查 SRS" --yesno "是否已按文档安装并编译好 SRS？

检测路径：$SRS_BIN" 14 76)
    if [ ! -x "$SRS_BIN" ]; then
      args=(--title "检查 SRS" --defaultno --yesno "是否已按文档安装并编译好 SRS？

检测路径：$SRS_BIN" 14 76)
    fi
    whiptail "${args[@]}"
    return $?
  fi

  if [ -t 0 ]; then
    read -r -p "[gen] 是否已安装 SRS？[y/N] " answer || true
  else
    read -r answer || true
  fi
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

print_srs_install_guide() {
  cat >&2 <<EOF
[gen] 请先安装 SRS 后重新执行本脚本（参考 docs/walle_deployment_require.md 第 5 节）：

  cd $REPO_ROOT/thirdparty
  git clone --depth 1 --branch v7.0-a4 https://github.com/ossrs/srs.git
  cd srs/trunk
  ./configure --srt=on
  make -j8
  ./objs/srs -V
EOF
}

# ---------- 2. srs.conf / srs.service ----------

valid_ip() {
  local ip="$1" part
  local -a parts

  if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    IFS='.' read -r -a parts <<< "$ip"
    for part in "${parts[@]}"; do
      [ "$((10#$part))" -le 255 ] || return 1
    done
    return 0
  fi

  [[ "$ip" =~ ^[0-9A-Fa-f:]+$ ]]
}

prompt_public_ip() {
  local ip=""
  if [ -t 0 ]; then
    read -r -p "[gen] 请输入公网 IP（WebRTC candidate，直接回车使用自动探测）: " ip || true
  else
    read -r ip || true
  fi
  if [ -n "$ip" ] && ! valid_ip "$ip"; then
    fail "公网 IP 格式不正确：$ip"
  fi
  PUBLIC_IP="$ip"
}

generate_srs_conf() {
  backup_if_exists "$SRS_CONF_OUT"

  if [ -z "$PUBLIC_IP" ]; then
    cp "$SRS_CONF_TPL" "$SRS_CONF_OUT"
    info "srs.conf: candidate 保持 *，使用自动探测"
  else
    sed "s|candidate \*;|candidate $PUBLIC_IP;|" "$SRS_CONF_TPL" > "$SRS_CONF_OUT"
    grep -Fq "candidate $PUBLIC_IP;" "$SRS_CONF_OUT" \
      || fail "srs.conf 中未找到 'candidate *;' 模板行，无法写入公网 IP"
    info "srs.conf: candidate $PUBLIC_IP"
  fi
  chmod 0644 "$SRS_CONF_OUT"
}

generate_srs_service() {
  local repo_esc user_esc group_esc
  repo_esc="$(escape_sed_repl "$REPO_ROOT")"
  user_esc="$(escape_sed_repl "$SERVICE_USER")"
  group_esc="$(escape_sed_repl "$SERVICE_GROUP")"

  backup_if_exists "$SRS_SERVICE_OUT"
  sed -e "s|^User=.*|User=$user_esc|" \
      -e "s|^Group=.*|Group=$group_esc|" \
      -e "s|/home/gs/01_walle_service/walle-service|$repo_esc|g" \
      "$SRS_SERVICE_TPL" > "$SRS_SERVICE_OUT"

  [ -s "$SRS_SERVICE_OUT" ] || fail "srs.service 生成失败，请检查 $SRS_SERVICE_TPL"
  grep -Fq "WorkingDirectory=$REPO_ROOT/thirdparty/srs/trunk" "$SRS_SERVICE_OUT" \
    || fail "srs.service 中 WorkingDirectory 路径替换失败"
  chmod 0644 "$SRS_SERVICE_OUT"
  info "srs.service 已生成（User=$SERVICE_USER Group=$SERVICE_GROUP）"

  [ -x "$SRS_BIN" ] || warn "未找到可执行文件 $SRS_BIN，启动 srs 前请确认已按文档编译"
}

# ---------- 3. EMQX API Key / nginx.conf ----------

emqx_running() {
  if systemctl is-active --quiet emqx 2>/dev/null; then
    return 0
  fi
  if systemctl is-active --quiet emqx-enterprise 2>/dev/null; then
    return 0
  fi
  if curl -fsS -o /dev/null --max-time 3 "$EMQX_BASE/" 2>/dev/null; then
    return 0
  fi
  return 1
}

ensure_emqx_running() {
  if emqx_running; then
    info "EMQX 正在运行（$EMQX_BASE）"
    return 0
  fi
  printf '[gen] EMQX 未启动，请先启动 EMQX 后重新执行本脚本：\n' >&2
  printf '      sudo systemctl start emqx\n' >&2
  printf '      systemctl status emqx --no-pager\n' >&2
  exit 1
}

prompt_emqx_password() {
  local password=""
  if [ -t 0 ]; then
    read -r -s -p "[gen] 请输入 EMQX Dashboard admin 密码（直接回车使用默认 public）: " password || true
    printf '\n'
  else
    read -r password || true
  fi
  [ -n "$password" ] || password="public"
  EMQX_ADMIN_PASSWORD="$password"
}

json_str() {
  if command -v jq >/dev/null 2>&1; then
    jq -Rn --arg v "$1" '$v'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
  else
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
  fi
}

json_get() {
  local key="$1" file="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$key" 'if type == "object" then (.[$k] // "") else "" end' "$file" 2>/dev/null || true
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2], "")
    print(v if isinstance(v, str) else "")
except Exception:
    print("")' "$file" "$key"
  else
    sed -n "s|.*\"$key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*|\1|p" "$file" | head -n 1
  fi
}

# EMQX 6.x：先登录 Dashboard 获取 Bearer Token；
# EMQX 5.x：/login 不存在，退化为 admin 密码 Basic 认证。
emqx_login() {
  local resp code token msg
  resp="$(new_tmp)"

  code="$(curl -sS -o "$resp" -w '%{http_code}' --connect-timeout 3 --max-time 10 \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"admin\",\"password\":$(json_str "$EMQX_ADMIN_PASSWORD")}" \
    "$EMQX_API/login" 2>/dev/null)" || code="000"

  if [ "$code" = "200" ]; then
    token="$(json_get token "$resp")"
    [ -n "$token" ] || fail "EMQX 登录成功，但响应中缺少 token"
    EMQX_TOKEN="$token"
    return 0
  fi

  if [ "$code" = "404" ] || [ "$code" = "405" ]; then
    EMQX_LEGACY_AUTH=1
    return 0
  fi

  if [ "$code" = "000" ]; then
    fail "无法连接 EMQX Dashboard API（$EMQX_BASE），请确认 EMQX 已启动且端口可访问"
  fi

  msg="$(json_get message "$resp")"
  [ -n "$msg" ] || msg="HTTP $code"
  fail "EMQX Dashboard 登录失败：$msg（请确认 admin 密码正确，且 $EMQX_BASE 可访问）"
}

create_emqx_api_key() {
  local resp code payload name msg key secret url
  name="walle-portal-$(date +%Y%m%d%H%M%S)"
  resp="$(new_tmp)"

  if [ "$EMQX_LEGACY_AUTH" = "1" ]; then
    payload="$(printf '{"name":"%s","description":"walle portal auto generated","enable":true}' "$name")"
    url="$EMQX_API/api_keys"
    code="$(curl -sS -o "$resp" -w '%{http_code}' --connect-timeout 3 --max-time 10 \
      -u "admin:$EMQX_ADMIN_PASSWORD" \
      -H 'Content-Type: application/json' \
      -X POST -d "$payload" "$url" 2>/dev/null)" || code="000"
  else
    payload="$(printf '{"name":"%s","desc":"walle portal auto generated","enable":true}' "$name")"
    url="$EMQX_API/api_key"
    code="$(curl -sS -o "$resp" -w '%{http_code}' --connect-timeout 3 --max-time 10 \
      -H "Authorization: Bearer $EMQX_TOKEN" \
      -H 'Content-Type: application/json' \
      -X POST -d "$payload" "$url" 2>/dev/null)" || code="000"
  fi

  if [ "$code" = "000" ]; then
    fail "无法连接 EMQX API（$url），请确认 EMQX 已启动且端口可访问"
  fi
  if [[ ! "$code" =~ ^2 ]]; then
    msg="$(json_get message "$resp")"
    [ -n "$msg" ] || msg="HTTP $code"
    fail "创建 EMQX API Key 失败：$msg"
  fi

  key="$(json_get api_key "$resp")"
  secret="$(json_get api_secret "$resp")"
  if [ -z "$key" ] || [ -z "$secret" ]; then
    fail "EMQX 响应中缺少 api_key/api_secret：$(head -c 200 "$resp" 2>/dev/null)"
  fi

  EMQX_API_KEY="$key"
  EMQX_API_SECRET="$secret"
  info "已创建 EMQX API Key：$EMQX_API_KEY"
}

generate_nginx_conf() {
  local basic basic_esc
  basic="$(printf '%s:%s' "$EMQX_API_KEY" "$EMQX_API_SECRET" | base64 | tr -d '\n')"
  [ -n "$basic" ] || fail "生成 Basic Auth（base64）失败"
  basic_esc="$(escape_sed_repl "$basic")"

  backup_if_exists "$NGINX_OUT"
  sed '1,/^$/d' "$NGINX_TPL" | sed "s|<BASE64(api_key:api_secret)>|$basic_esc|g" > "$NGINX_OUT"

  [ -s "$NGINX_OUT" ] || fail "nginx.conf 生成失败，请检查 $NGINX_TPL"
  if grep -Fq '<BASE64(api_key:api_secret)>' "$NGINX_OUT"; then
    fail "nginx.conf 中仍存在未替换的 <BASE64(api_key:api_secret)> 占位符"
  fi
  grep -qE 'proxy_set_header[[:space:]]+Authorization[[:space:]]+"Basic ' "$NGINX_OUT" \
    || fail "nginx.conf 中未找到 Authorization Basic 配置行"
  chmod 0644 "$NGINX_OUT"
  info "nginx.conf 已生成（/emqx-api/ 已写入 Basic API Key）"
}

# ---------- 4. walle.conf / walle.service ----------

generate_token() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  elif command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -tx1 -N24 /dev/urandom | tr -d ' \n'
    printf '\n'
  fi
}

generate_walle_conf() {
  local token key_esc secret_esc token_esc
  token="$(generate_token)"
  [ -n "$token" ] || fail "生成 WALLE_INGEST_TOKEN 失败"
  key_esc="$(escape_sed_repl "$EMQX_API_KEY")"
  secret_esc="$(escape_sed_repl "$EMQX_API_SECRET")"
  token_esc="$(escape_sed_repl "$token")"

  backup_if_exists "$WALLE_OUT"
  sed -e "s|<你的 api_key>|$key_esc|g" \
      -e "s|<你的 api_secret>|$secret_esc|g" \
      -e "s|<你的 ingest token>|$token_esc|g" \
      "$WALLE_TPL" > "$WALLE_OUT"

  grep -Fqx "EMQX_API_KEY=$EMQX_API_KEY" "$WALLE_OUT" \
    || fail "walle.conf 中 EMQX_API_KEY 写入失败"
  grep -Fqx "EMQX_API_SECRET=$EMQX_API_SECRET" "$WALLE_OUT" \
    || fail "walle.conf 中 EMQX_API_SECRET 写入失败"
  grep -Fqx "WALLE_INGEST_TOKEN=$token" "$WALLE_OUT" \
    || fail "walle.conf 中 WALLE_INGEST_TOKEN 写入失败"
  chmod 0600 "$WALLE_OUT"
  info "walle.conf 已生成（权限 600，EMQX 凭据与第 3 步一致）"
}

generate_walle_service() {
  local repo_esc user_esc group_esc
  repo_esc="$(escape_sed_repl "$REPO_ROOT")"
  user_esc="$(escape_sed_repl "$SERVICE_USER")"
  group_esc="$(escape_sed_repl "$SERVICE_GROUP")"

  backup_if_exists "$WALLE_SERVICE_OUT"
  sed '1,/^$/d' "$WALLE_SERVICE_TPL" \
    | sed -e "s|__REPO_ROOT__|$repo_esc|g" \
          -e "s|__USER__|$user_esc|g" \
          -e "s|__GROUP__|$group_esc|g" > "$WALLE_SERVICE_OUT"

  [ -s "$WALLE_SERVICE_OUT" ] || fail "walle.service 生成失败，请检查 $WALLE_SERVICE_TPL"
  if grep -qE '__REPO_ROOT__|__USER__|__GROUP__' "$WALLE_SERVICE_OUT"; then
    fail "walle.service 中仍存在未替换的占位符"
  fi
  chmod 0644 "$WALLE_SERVICE_OUT"
  info "walle.service 已生成（User=$SERVICE_USER Group=$SERVICE_GROUP）"
}

# ---------- main ----------

main() {
  info "仓库根目录：$REPO_ROOT"

  local tpl
  for tpl in "$SRS_CONF_TPL" "$SRS_SERVICE_TPL" "$NGINX_TPL" "$WALLE_TPL" "$WALLE_SERVICE_TPL"; do
    [ -f "$tpl" ] || fail "缺少模板文件：$tpl"
  done
  command -v curl >/dev/null 2>&1 || fail "未找到 curl，请先安装：sudo apt install -y curl"

  info "1/4 检查 SRS、nginx、EMQX 安装情况"
  check_required_packages
  if ! confirm_srs; then
    print_srs_install_guide
    exit 1
  fi
  info "SRS 检查通过"

  info "2/4 生成 srs.conf 与 srs.service"
  prompt_public_ip
  generate_srs_conf
  generate_srs_service

  info "3/4 生成 nginx.conf（需要 EMQX API Key）"
  ensure_emqx_running
  prompt_emqx_password
  emqx_login
  create_emqx_api_key
  generate_nginx_conf

  info "4/4 生成 walle.conf 与 walle.service"
  generate_walle_conf
  generate_walle_service

  cat <<EOF

================= 生成完成 =================
SRS 配置      : $SRS_CONF_OUT
SRS 服务单元  : $SRS_SERVICE_OUT
Nginx 配置    : $NGINX_OUT
walle 配置    : $WALLE_OUT（权限 600）
walle 服务单元: $WALLE_SERVICE_OUT
服务用户      : $SERVICE_USER:$SERVICE_GROUP

后续步骤：
  1. 部署 ：bash $SCRIPT_DIR/deploy.sh
============================================
EOF
}

main "$@"
