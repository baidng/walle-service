#!/usr/bin/env bash
set -euo pipefail

# 部署脚本：检查 etc/ 下已生成的配置文件与 systemd 单元，安装到系统目录。
# srs.conf、nginx.conf 保留在仓库 etc/ 中，由 srs.service / start-walle.sh 直接引用。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ETC_DIR="$REPO_ROOT/etc"
GEN_SCRIPT="$REPO_ROOT/scripts/auto_gen_conf_service.sh"
DOC="$REPO_ROOT/docs/walle_deployment_require.md"

SRS_CONF="$ETC_DIR/srs.conf"
SRS_SERVICE="$ETC_DIR/srs.service"
NGINX_CONF="$ETC_DIR/nginx.conf"
WALLE_CONF="$ETC_DIR/walle.conf"
WALLE_SERVICE="$ETC_DIR/walle.service"

CONF_DIR="/etc/walle"
CONF_DST="$CONF_DIR/walle.conf"
SERVICE_DIR="/etc/systemd/system"
SRS_SERVICE_DST="$SERVICE_DIR/srs.service"
WALLE_SERVICE_DST="$SERVICE_DIR/walle.service"

info() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] 失败: %s\n' "$*" >&2; exit 1; }

require_file() {
  local path="$1" doc_section="$2"
  if [ ! -s "$path" ]; then
    printf '[deploy] 失败: 未找到或文件为空：%s\n' "$path" >&2
    printf '         请先执行 bash %s 自动生成，\n' "$GEN_SCRIPT" >&2
    printf '         或参考 %s %s 手动生成。\n' "$DOC" "$doc_section" >&2
    exit 1
  fi
}

validate_nginx_conf() {
  if grep -q '<BASE64(api_key:api_secret)>' "$NGINX_CONF"; then
    fail "etc/nginx.conf 仍含占位符 <BASE64(api_key:api_secret)>，请重新生成或手动替换"
  fi
  grep -qE 'proxy_set_header[[:space:]]+Authorization[[:space:]]+"Basic ' "$NGINX_CONF" \
    || fail "etc/nginx.conf 未找到 proxy_set_header Authorization \"Basic ...\" 行"
}

validate_walle_conf() {
  local key value
  for key in EMQX_API_KEY EMQX_API_SECRET WALLE_INGEST_TOKEN; do
    value="$(grep -E "^${key}=" "$WALLE_CONF" | tail -n1 | cut -d= -f2- || true)"
    [ -n "$value" ] || fail "etc/walle.conf 缺少 $key"
    case "$value" in
      *'<'*|*'替换'*) fail "etc/walle.conf 中 $key 仍是占位值，请重新生成或填写真实凭据" ;;
    esac
  done
}

# ---------- 1. 检查 SRS 配置与 systemd 单元 ----------
info "1/4 检查 SRS 配置"
require_file "$SRS_CONF" "第 7.2 节"
require_file "$SRS_SERVICE" "第 7.3 节"
info "    etc/srs.conf、etc/srs.service 已就绪"

# ---------- 2. 检查 Nginx 配置 ----------
info "2/4 检查 Nginx 配置"
require_file "$NGINX_CONF" "第 8.1 节"
validate_nginx_conf
info "    etc/nginx.conf 已就绪"

# ---------- 3. 检查 walle 配置与 systemd 单元 ----------
info "3/4 检查 walle 配置"
require_file "$WALLE_CONF" "第 8.2 节"
require_file "$WALLE_SERVICE" "第 8.3 节"
validate_walle_conf
info "    etc/walle.conf、etc/walle.service 已就绪"

# ---------- 4. 安装到系统目录 ----------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  fail "安装到系统目录需要 root 权限，请用 sudo 执行本脚本"
fi

info "4/4 安装到系统目录"
$SUDO install -d -m 0755 "$CONF_DIR"
$SUDO install -m 0600 "$WALLE_CONF" "$CONF_DST"
$SUDO install -m 0644 "$SRS_SERVICE" "$SRS_SERVICE_DST"
$SUDO install -m 0644 "$WALLE_SERVICE" "$WALLE_SERVICE_DST"
$SUDO systemctl daemon-reload 2>/dev/null || info "    提示：systemctl daemon-reload 未成功，非 systemd 环境可忽略"

# ---------- 关键信息 ----------
cat <<EOF

================= 部署完成 =================
SRS 配置      : $SRS_CONF（保留在仓库 etc/，不复制）
SRS 服务单元  : $SRS_SERVICE_DST
Nginx 配置    : $NGINX_CONF（保留在仓库 etc/，不复制）
walle 配置    : $CONF_DST（权限 600）
walle 服务单元: $WALLE_SERVICE_DST

当前服务尚未启动，请执行：
  sudo systemctl start srs
  sudo systemctl start walle

开机自启（可选）：
  sudo systemctl enable srs
  sudo systemctl enable walle

查看状态：
  systemctl status srs --no-pager
  systemctl status walle --no-pager
============================================
EOF
