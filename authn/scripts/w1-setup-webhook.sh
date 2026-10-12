#!/usr/bin/env bash
# W1: create the webhook TLS cert (owned by the running user) and populate
# /etc/kubernetes/webhook with what the apiserver container needs:
#   ca.crt, webhook-kubeconfig.yaml, audit-policy.yaml
# Idempotent. Requires sudo for /etc/kubernetes/webhook only.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${POC_AUTHN_DIR:-$HOME/poc-authn}
TLS=$WORK/tls
ETC=/etc/kubernetes/webhook

mkdir -p "$TLS"
if [ ! -f "$TLS/tls.key" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "$TLS/tls.key" -out "$TLS/tls.crt" \
    -subj "/CN=poc-authn-webhook" -addext "subjectAltName=IP:127.0.0.1"
  chmod 600 "$TLS/tls.key"
  echo "[w1] generated $TLS/tls.crt"
else
  echo "[w1] reusing $TLS/tls.crt"
fi

sudo mkdir -p "$ETC"
sudo install -o root -g root -m 0644 "$TLS/tls.crt"                 "$ETC/ca.crt"
sudo install -o root -g root -m 0644 "$REPO/authn/webhook-kubeconfig.yaml" "$ETC/webhook-kubeconfig.yaml"
sudo install -o root -g root -m 0644 "$REPO/authn/audit-policy.yaml"       "$ETC/audit-policy.yaml"

echo "[w1] $ETC:"; ls -la "$ETC"
openssl x509 -in "$ETC/ca.crt" -noout -subject -dates -ext subjectAltName
