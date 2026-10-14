#!/usr/bin/env bash
# W4: run the PoC token API as a nohup host process (loopback only).
#   w4-run-token-api.sh start|stop|restart|status
# Env: TOKEN_API_PORT (8080), TOKEN_API_CLUSTER_ID (gasandevcontrol),
#      TOKEN_API_KUBECONFIG (admin kubeconfig for endpoint/CA; default kubectl current)
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${POC_AUTHN_DIR:-$HOME/poc-authn}
PID=$WORK/token_api.pid
PORT=${TOKEN_API_PORT:-8080}

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

start() {
  if running; then echo "[w4] already running pid $(cat "$PID")"; return 0; fi
  mkdir -p "$WORK"
  args=( --port "$PORT" --db "$WORK/tokens.db"
         --cluster-id "${TOKEN_API_CLUSTER_ID:-gasandevcontrol}"
         --audit-log "$WORK/token_audit.jsonl" --log "$WORK/token_api.log" )
  [ -n "${TOKEN_API_KUBECONFIG:-}" ] && args+=( --kubeconfig "$TOKEN_API_KUBECONFIG" )
  nohup python3 "$REPO/authn/token_api.py" "${args[@]}" >> "$WORK/token_api.out" 2>&1 &
  echo $! > "$PID"
  sleep 1
  status
}
stop() {
  if running; then kill "$(cat "$PID")"; sleep 1; echo "[w4] stopped pid $(cat "$PID")"; fi
  rm -f "$PID"
}
status() {
  if running; then
    echo "[w4] running pid $(cat "$PID"), started $(ps -o lstart= -p "$(cat "$PID")")"
    curl -s "http://127.0.0.1:$PORT/healthz"; echo
  else
    echo "[w4] not running"; return 1
  fi
}
case "${1:-status}" in
  start) start ;; stop) stop ;; restart) stop; start ;; status) status ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
