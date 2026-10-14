#!/usr/bin/env bash
# W4: curl wrapper for the PoC token API (commands are what goes into the report).
#   w4-tok.sh issue OWNER TENANT [CLUSTER] [TTL_SECONDS]   -> POST /tokens  (prints plaintext once)
#   w4-tok.sh list [OWNER]                                 -> GET  /tokens[?owner=]
#   w4-tok.sh get ID                                       -> GET  /tokens/ID
#   w4-tok.sh revoke ID                                    -> DELETE /tokens/ID
#   w4-tok.sh revoke-user OWNER                            -> DELETE /tokens?user=OWNER
#   w4-tok.sh kubeconfig OWNER TENANT [TTL_SECONDS] [OUT]  -> GET /clusters/<id>/kubeconfig (writes OUT, mode 600)
# Env: TOKEN_API (http://127.0.0.1:8080), TOKEN_API_CLUSTER_ID (gasandevcontrol), ACTOR ($USER)
set -euo pipefail
API=${TOKEN_API:-http://127.0.0.1:8080}
CID=${TOKEN_API_CLUSTER_ID:-gasandevcontrol}
WORK=${POC_AUTHN_DIR:-$HOME/poc-authn}
H=( -H "X-Actor: ${ACTOR:-$USER}" -H "Content-Type: application/json" )
cmd=${1:-}; shift || true
case "$cmd" in
  issue)
    [ $# -ge 2 ] || { echo "usage: issue OWNER TENANT [CLUSTER] [TTL]"; exit 2; }
    body=$(printf '{"owner_id":"%s","tenant":"%s","cluster":"%s","ttl_seconds":%s}' "$1" "$2" "${3:-$CID}" "${4:-86400}")
    curl -s "${H[@]}" -X POST "$API/tokens" -d "$body" ;;
  list)
    curl -s "${H[@]}" "$API/tokens${1:+?owner=$1}" ;;
  get)
    curl -s "${H[@]}" "$API/tokens/$1" ;;
  revoke)
    curl -s "${H[@]}" -X DELETE "$API/tokens/$1" ;;
  revoke-user)
    curl -s "${H[@]}" -X DELETE "$API/tokens?user=$1" ;;
  kubeconfig)
    [ $# -ge 2 ] || { echo "usage: kubeconfig OWNER TENANT [TTL] [OUT]"; exit 2; }
    out=${4:-$WORK/$1.kubeconfig}
    mkdir -p "$(dirname "$out")"
    hdr=$(mktemp)
    code=$(curl -s "${H[@]}" -D "$hdr" -o "$out" -w '%{http_code}' \
           "$API/clusters/$CID/kubeconfig?owner=$1&tenant=$2&ttl=${3:-86400}")
    if [ "$code" = 200 ]; then
      chmod 600 "$out"
      echo "[w4] wrote $out  $(grep -i '^X-Token-' "$hdr" | tr -d '\r' | tr '\n' ' ')"
    else
      echo "[w4] HTTP $code"; cat "$out"; echo; rm -f "$out"; exit 1
    fi
    rm -f "$hdr" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
echo
