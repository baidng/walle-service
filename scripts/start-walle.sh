#!/usr/bin/env bash
set -euo pipefail

# 路径全部从脚本自身位置推导；仓库整体移动后无需修改
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BASE_DIR/.." && pwd)"
CONF="$REPO_ROOT/etc/nginx.conf"
RUN_DIR="$REPO_ROOT/.run/nginx"
LOG_DIR="$REPO_ROOT/log"
PID_FILE="$RUN_DIR/nginx.pid"
API_PID_FILE="$RUN_DIR/walle-api.pid"
API_LOG="$LOG_DIR/walle-api.log"
API_SCRIPT="$REPO_ROOT/src/backend/walle_api.py"
PORTAL_URL="http://127.0.0.1:8088/"
API_URL="http://127.0.0.1:9000/api/health"

api_running() {
  [ -f "$API_PID_FILE" ] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null
}

start_api() {
  if api_running; then
    echo "api already running (pid $(cat "$API_PID_FILE"))"
    return 0
  fi
  rm -f "$API_PID_FILE"
  nohup python3 "$API_SCRIPT" >>"$API_LOG" 2>&1 &
  echo $! > "$API_PID_FILE"
  sleep 1
  if api_running; then
    echo "api started (pid $(cat "$API_PID_FILE")), $API_URL"
  else
    echo "api failed to start, see $API_LOG" >&2
    return 1
  fi
}

case "${1:-start}" in
  start)
    if [ ! -f "$CONF" ]; then
      echo "missing $CONF, copy etc/nginx.conf.example to etc/nginx.conf first" >&2
      exit 1
    fi
    mkdir -p "$LOG_DIR" "$RUN_DIR/temp/client_body" "$RUN_DIR/temp/proxy" "$RUN_DIR/temp/fastcgi" "$RUN_DIR/temp/uwsgi" "$RUN_DIR/temp/scgi"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "portal already running (pid $(cat "$PID_FILE"))"
    else
      rm -f "$PID_FILE"
      nginx -p "$REPO_ROOT" -c "$CONF"
      echo "portal started (pid $(cat "$PID_FILE")), $PORTAL_URL"
    fi
    start_api
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
    if api_running; then
      API_PID="$(cat "$API_PID_FILE")"
      kill -TERM "$API_PID"
      for _ in $(seq 1 25); do
        kill -0 "$API_PID" 2>/dev/null || break
        sleep 0.2
      done
      rm -f "$API_PID_FILE"
      echo "api stopped"
    else
      rm -f "$API_PID_FILE"
      echo "api not running"
    fi
    ;;
  status)
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$PORTAL_URL" || true)
    echo "portal http=$code"
    api_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API_URL" || true)
    echo "api http=$api_code"
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
