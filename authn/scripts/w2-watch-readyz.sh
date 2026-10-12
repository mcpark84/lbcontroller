#!/usr/bin/env bash
# W2: poll /readyz once per second and print state transitions with
# timestamps, to measure apiserver downtime during the manifest swap.
#   w2-watch-readyz.sh [server] [logfile]
SERVER=${1:-https://172.16.1.120:6443}
LOG=${2:-${POC_AUTHN_DIR:-$HOME/poc-authn}/readyz.log}
prev=""
echo "[w2] watching $SERVER/readyz -> $LOG (Ctrl-C to stop)"
while true; do
  code=$(curl -sk -m 2 -o /dev/null -w '%{http_code}' "$SERVER/readyz" || echo 000)
  ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  echo "$ts $code" >> "$LOG"
  if [ "$code" != "$prev" ]; then echo "$ts readyz=$code"; prev=$code; fi
  sleep 1
done
