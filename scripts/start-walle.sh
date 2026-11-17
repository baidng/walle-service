#!/usr/bin/env bash
set -euo pipefail

# 路径全部从脚本自身位置推导；仓库整体移动后无需修改
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BASE_DIR/.." && pwd)"
CONF="$REPO_ROOT/etc/nginx.conf"
RUN_DIR="$REPO_ROOT/.run/nginx"
LOG_DIR="$REPO_ROOT/log"
PID_FILE="$RUN_DIR/nginx.pid"
PORTAL_URL="http://127.0.0.1:8088/"

case "${1:-start}" in
  start)
    if [ ! -f "$CONF" ]; then
      echo "missing $CONF, copy etc/nginx.conf.example to etc/nginx.conf first" >&2
      exit 1
    fi
    mkdir -p "$LOG_DIR" "$RUN_DIR/temp/client_body" "$RUN_DIR/temp/proxy" "$RUN_DIR/temp/fastcgi" "$RUN_DIR/temp/uwsgi" "$RUN_DIR/temp/scgi"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "portal already running (pid $(cat "$PID_FILE"))"
      exit 0
    fi
    rm -f "$PID_FILE"
    nginx -p "$REPO_ROOT" -c "$CONF"
    echo "portal started (pid $(cat "$PID_FILE")), $PORTAL_URL"
    ;;
  stop)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      PID="$(cat "$PID_FILE")"
      kill -QUIT "$PID"
      for _ in $(seq 1 25); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.2
      done
      rm -f "$PID_FILE"
      echo "portal stopped"
    else
      rm -f "$PID_FILE"
      echo "portal not running"
    fi
    ;;
  status)
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$PORTAL_URL" || true)
    echo "portal http=$code"
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
