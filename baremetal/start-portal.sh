#!/usr/bin/env bash
set -euo pipefail

PREFIX="/home/gs/01_walle_service/baremetal/nginx"
CONF="$PREFIX/nginx.conf"
PID_FILE="$PREFIX/nginx.pid"

case "${1:-start}" in
  start)
    mkdir -p "$PREFIX/temp/client_body" "$PREFIX/temp/proxy" "$PREFIX/temp/fastcgi" "$PREFIX/temp/uwsgi" "$PREFIX/temp/scgi"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "portal already running (pid $(cat "$PID_FILE"))"
      exit 0
    fi
    nginx -p "$PREFIX" -c "$CONF"
    echo "portal started (pid $(cat "$PID_FILE")), http://127.0.0.1:8088/"
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      nginx -p "$PREFIX" -c "$CONF" -s quit || true
      echo "portal stopped"
    else
      echo "portal not running"
    fi
    ;;
  status)
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8088/ || true)
    echo "portal http=$code"
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
