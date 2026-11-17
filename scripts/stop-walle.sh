#!/usr/bin/env bash
set -euo pipefail

# 手动停止 walle 门户（对应 scripts/start-walle.sh 启动的实例）
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BASE_DIR/.." && pwd)"
RUN_DIR="$REPO_ROOT/.run/nginx"
PID_FILE="$RUN_DIR/nginx.pid"
PORTAL_URL="http://127.0.0.1:8088/"

if [ ! -f "$PID_FILE" ]; then
  echo "walle not running (no $PID_FILE)"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "walle not running (stale pid $PID)"
  rm -f "$PID_FILE"
  exit 0
fi

echo "stopping walle (pid $PID)"
kill -QUIT "$PID"

for _ in $(seq 1 25); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.2
done

if kill -0 "$PID" 2>/dev/null; then
  echo "graceful stop timeout, sending TERM to pid $PID" >&2
  kill -TERM "$PID" 2>/dev/null || true
  sleep 1
fi

rm -f "$PID_FILE"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$PORTAL_URL" || true)
if [ -z "$code" ] || [ "$code" = "000" ]; then
  echo "walle stopped"
else
  echo "walle stopped locally, but $PORTAL_URL still returns $code (another instance?)" >&2
fi
