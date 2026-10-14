#!/usr/bin/env bash
# W5: webhook calls per second while kubectl hammers the apiserver.
#   w5-webhook-rate.sh KUBECONFIG SECONDS [PARALLEL=1]
# Runs PARALLEL loops of back-to-back `kubectl get nodes` for SECONDS, then
# counts the webhook log lines (POST /authenticate decisions) written in that
# window and the kubectl requests made, and prints both rates.
set -uo pipefail
KC=$1; SEC=$2; PAR=${3:-1}
WORK=${POC_AUTHN_DIR:-$HOME/poc-authn}
LOG=$WORK/webhook.log
tmp=$(mktemp -d)
before=$(wc -l < "$LOG")
t0=$(date +%s)
for i in $(seq "$PAR"); do
  ( n=0; ok=0; while [ $(( $(date +%s) - t0 )) -lt "$SEC" ]; do
      kubectl --kubeconfig "$KC" get nodes --request-timeout=5s >/dev/null 2>&1 && ok=$((ok+1)); n=$((n+1)); done
    echo "$n $ok" > "$tmp/$i" ) &
done
wait
after=$(wc -l < "$LOG")
req=0; okc=0
for f in "$tmp"/*; do read -r n ok < "$f"; req=$((req+n)); okc=$((okc+ok)); done
calls=$(sed -n "$((before+1)),${after}p" "$LOG" | grep -c ' hash=')
printf "[w5] %ds parallel=%d kubectl=%d (ok=%d, %.1f req/s)  webhook=%d (%.2f call/s)  calls/req=%.3f\n" \
  "$SEC" "$PAR" "$req" "$okc" "$(echo "$req/$SEC" | bc -l)" "$calls" "$(echo "$calls/$SEC" | bc -l)" \
  "$(echo "$calls/($req>0?$req:1)" | bc -l)"
rm -rf "$tmp"
