#!/usr/bin/env bash
# W5: measure "revoke -> first 401" latency for one token.
#   w5-measure-revoke.sh TOKEN_ID KUBECONFIG [PRIME_SECONDS=3] [MAX_WAIT=180]
# 1. polls `kubectl get nodes` once per second with KUBECONFIG (so the apiserver
#    caches are warm), 2. revokes TOKEN_ID via the API, 3. keeps polling until
#    the first non-200, prints both timestamps and the delta.
set -uo pipefail
ID=$1; KC=$2; PRIME=${3:-3}; MAX=${4:-180}
API=${TOKEN_API:-http://127.0.0.1:8080}
REPO=$(cd "$(dirname "$0")/../.." && pwd)

probe() {  # prints HTTP-ish status: 200 / 401 / 403 / ERR
  out=$(kubectl --kubeconfig "$KC" get nodes --request-timeout=5s -o name 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then echo 200
  elif grep -q Unauthorized <<<"$out"; then echo 401
  elif grep -q -i forbidden <<<"$out"; then echo 403
  else echo "ERR($(head -c 60 <<<"$out"))"; fi
}
ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }

echo "[w5] priming $PRIME s"
for _ in $(seq "$PRIME"); do echo "$(ts) $(probe)"; sleep 1; done
t_rev=$(date +%s.%N)
resp=$(curl -s -H "X-Actor: w5" -X DELETE "$API/tokens/$ID")
echo "$(ts) REVOKE $ID -> $(echo "$resp" | tr -d '\n' | head -c 120)"
n=0
while [ $n -lt "$MAX" ]; do
  s=$(probe); now=$(date +%s.%N)
  echo "$(ts) $s"
  if [ "$s" != 200 ]; then
    printf "[w5] RESULT first non-200 (%s) after %.1f s\n" "$s" "$(echo "$now - $t_rev" | bc)"
    exit 0
  fi
  sleep 1; n=$((n+1))
done
echo "[w5] RESULT still 200 after $MAX s"; exit 1
