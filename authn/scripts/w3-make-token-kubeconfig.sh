#!/usr/bin/env bash
# W3: build a bearer-token kubeconfig from the current cluster's CA/endpoint.
#   w3-make-token-kubeconfig.sh [token] [out]
set -euo pipefail
TOKEN=${1:-poc-token-0001}
OUT=${2:-${POC_AUTHN_DIR:-$HOME/poc-authn}/token.kubeconfig}
CA=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
SERVER=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')
[ -n "$CA" ] && [ -n "$SERVER" ] || { echo "cannot read CA/server from current kubeconfig" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: poc
    cluster:
      certificate-authority-data: $CA
      server: $SERVER
users:
  - name: poc-user
    user:
      token: $TOKEN
contexts:
  - name: poc
    context:
      cluster: poc
      user: poc-user
current-context: poc
YAML
chmod 600 "$OUT"
echo "[w3] wrote $OUT (server=$SERVER token=${TOKEN:0:8}...)"
