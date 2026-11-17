#!/usr/bin/env bash
set -euo pipefail

# 手动停止 walle（门户 nginx + 用户管理 API）
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BASE_DIR/.." && pwd)"
RUN_DIR="$REPO_ROOT/.run/nginx"
PID_FILE="$RUN_DIR/nginx.pid"
API_PID_FILE="$RUN_DIR/walle-api.pid"
PORTAL_URL="http://127.0.0.1:8088/"
API_URL="http://127.0.0.1:9000/api/health"

stop_pid() {
  local pidfile="$1" name="$2" signal="$3"
  if [ ! -f "$pidfile" ]; then
    echo "$name not running (no $pidfile)"
    return 0
  fi
  local pid
  pid="$(cat "$pidfile")"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name not running (stale pid $pid)"
    rm -f "$pidfile"
    return 0
  fi
  echo "stopping $name (pid $pid)"
  kill "-$signal" "$pid"
  for _ in $(seq 1 25); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$name graceful stop timeout, sending TERM to pid $pid" >&2
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$pidfile"
}

stop_pid "$PID_FILE" "portal" QUIT
stop_pid "$API_PID_FILE" "api" TERM

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$PORTAL_URL" || true)
if [ -z "$code" ] || [ "$code" = "000" ]; then
  echo "walle stopped"
else
  echo "walle stopped locally, but $PORTAL_URL still returns $code (another instance?)" >&2
fi

api_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$API_URL" || true)
if [ -n "$api_code" ] && [ "$api_code" != "000" ]; then
  echo "warning: $API_URL still returns $api_code" >&2
fi
