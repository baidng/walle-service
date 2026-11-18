#!/usr/bin/env bash
set -euo pipefail

# 部署脚本：校验 etc/walle.conf → 渲染 scripts/walle.service → 安装到系统目录
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_SRC="$REPO_ROOT/etc/walle.conf"
SERVICE_TPL="$REPO_ROOT/scripts/walle.service.example"
SERVICE_SRC="$REPO_ROOT/scripts/walle.service"

CONF_DIR="/etc/walle"
CONF_DST="$CONF_DIR/walle.conf"
SERVICE_DST="/etc/systemd/system/walle.service"

SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

info() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] 失败: %s\n' "$*" >&2; exit 1; }

# ---------- 1. 检查 walle.conf ----------
info "1/3 检查配置文件 $CONF_SRC"
[ -f "$CONF_SRC" ] || fail "找不到 etc/walle.conf，请先执行：cp etc/walle.conf.example etc/walle.conf 并填入真实密钥"
[ -s "$CONF_SRC" ] || fail "etc/walle.conf 是空文件"

for key in EMQX_API_KEY EMQX_API_SECRET; do
  value="$(grep -E "^${key}=" "$CONF_SRC" | tail -n1 | cut -d= -f2- || true)"
  [ -n "$value" ] || fail "etc/walle.conf 缺少 $key"
  case "$value" in
    *'<'*|*'替换'*) fail "etc/walle.conf 中 $key 仍是占位值，请填入真实凭据" ;;
  esac
done
info "    walle.conf 校验通过"

# ---------- 1b. 检查 nginx.conf ----------
NGINX_CONF="$REPO_ROOT/etc/nginx.conf"
info "    检查 nginx.conf $NGINX_CONF"
[ -f "$NGINX_CONF" ] || fail "找不到 etc/nginx.conf，请先执行：cp etc/nginx.conf.example etc/nginx.conf 并替换 <BASE64(api_key:api_secret)>"
[ -s "$NGINX_CONF" ] || fail "etc/nginx.conf 是空文件"
if grep -q '<BASE64(api_key:api_secret)>' "$NGINX_CONF"; then
  fail "etc/nginx.conf 仍含占位符 <BASE64(api_key:api_secret)>，请用 6.3 节生成的真实 Basic 值替换"
fi
auth_line="$(grep -nE 'proxy_set_header\s+Authorization\s+"Basic ' "$NGINX_CONF" || true)"
[ -n "$auth_line" ] || fail "etc/nginx.conf 未找到 proxy_set_header Authorization \"Basic ...\" 行，请检查 /emqx-api/ location 配置"
info "    nginx.conf 校验通过"

# ---------- 2. 生成 scripts/walle.service ----------
info "2/3 渲染服务单元 $SERVICE_SRC"
[ -f "$SERVICE_TPL" ] || fail "找不到模板 scripts/walle.service.example"
sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__GROUP__|$SERVICE_GROUP|g" \
    "$SERVICE_TPL" | sed '1,/^$/d' > "$SERVICE_SRC"
[ -s "$SERVICE_SRC" ] || fail "服务单元渲染失败，请检查 $SERVICE_TPL"
info "    已生成，服务用户 $SERVICE_USER"

# ---------- 3. 安装到系统目录 ----------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  fail "安装到系统目录需要 root 权限，请用 sudo 执行本脚本"
fi

info "3/3 安装到系统目录"
$SUDO install -d -m 0755 "$CONF_DIR"
$SUDO install -m 0600 "$CONF_SRC" "$CONF_DST"
$SUDO install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
$SUDO systemctl daemon-reload 2>/dev/null || info "    提示：systemctl daemon-reload 未成功，非 systemd 环境可忽略"

# ---------- 关键信息 ----------
cat <<EOF

================= 部署完成 =================
配置模板   : etc/walle.conf.example
配置文件   : $CONF_DST (权限 600)
服务模板   : scripts/walle.service.example
服务单元   : $SERVICE_DST
生成文件   : $SERVICE_SRC
服务用户   : $SERVICE_USER
日志目录   : $REPO_ROOT/log
运行目录   : $REPO_ROOT/.run/nginx

启动服务   : sudo systemctl start walle
停止服务   : sudo systemctl stop walle
开机自启   : sudo systemctl enable walle
查看状态   : systemctl status walle --no-pager
============================================
EOF
