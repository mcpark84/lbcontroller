#!/usr/bin/env bash
# W1: run the PoC webhook as a nohup host process on the control-plane node.
#   w1-run-webhook.sh start|stop|restart|status
# Env: WEBHOOK_GROUPS (default poc-tenant-a), WEBHOOK_USER (default poc-user)
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${POC_AUTHN_DIR:-$HOME/poc-authn}
PID=$WORK/webhook.pid
PORT=${WEBHOOK_PORT:-8443}

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

start() {
  if running; then echo "[w1] already running pid $(cat "$PID")"; return 0; fi
  mkdir -p "$WORK"
  nohup python3 "$REPO/authn/webhook_server.py" \
      --port "$PORT" \
      --cert "$WORK/tls/tls.crt" --key "$WORK/tls/tls.key" \
      --username "${WEBHOOK_USER:-poc-user}" --uid poc-1 \
      --groups "${WEBHOOK_GROUPS:-poc-tenant-a}" \
      --log "$WORK/webhook.log" >> "$WORK/webhook.out" 2>&1 &
  echo $! > "$PID"
  sleep 1
  status
}
stop() {
  if running; then kill "$(cat "$PID")"; sleep 1; echo "[w1] stopped pid $(cat "$PID")"; fi
  rm -f "$PID"
}
status() {
  if running; then
    echo "[w1] running pid $(cat "$PID"), started $(ps -o lstart= -p "$(cat "$PID")")"
    curl -s --cacert "$WORK/tls/tls.crt" "https://127.0.0.1:$PORT/healthz"; echo
  else
    echo "[w1] not running"; return 1
  fi
}
case "${1:-status}" in
  start) start ;; stop) stop ;; restart) stop; start ;; status) status ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
