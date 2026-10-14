#!/usr/bin/env bash
# W6: mTLS material for apiserver -> webhook.
#   w6-make-client-cert.sh            # create + install
#   w6-make-client-cert.sh --bad-ca   # also create an unrelated CA (for the negative test)
# Creates in ~/poc-authn/tls:
#   client-ca.key/.crt          CA the webhook trusts for client certs
#   apiserver-client.key/.crt   cert the apiserver presents (CN=kube-apiserver), signed by client-ca
#   bad-ca.crt                  (--bad-ca) a different CA, to prove the webhook really verifies
# Installs apiserver-client.{crt,key} into /etc/kubernetes/webhook (already mounted into the
# apiserver pod; the directory is read at startup, so the apiserver must be restarted to use it)
# and renders ~/poc-authn/webhook-kubeconfig.mtls.yaml for P8 (NOT installed here).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${POC_AUTHN_DIR:-$HOME/poc-authn}
TLS=$WORK/tls
ETC=/etc/kubernetes/webhook
mkdir -p "$TLS"

if [ ! -f "$TLS/client-ca.key" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout "$TLS/client-ca.key" -out "$TLS/client-ca.crt" \
    -subj "/CN=poc-authn-client-ca" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
  openssl req -newkey rsa:2048 -nodes -keyout "$TLS/apiserver-client.key" -out "$TLS/apiserver-client.csr" \
    -subj "/CN=kube-apiserver/O=poc-authn"
  printf 'extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\nbasicConstraints=CA:FALSE\n' > "$TLS/client.ext"
  openssl x509 -req -in "$TLS/apiserver-client.csr" -CA "$TLS/client-ca.crt" -CAkey "$TLS/client-ca.key" \
    -CAcreateserial -days 365 -extfile "$TLS/client.ext" -out "$TLS/apiserver-client.crt"
  chmod 600 "$TLS"/*.key
  echo "[w6] generated client-ca.crt + apiserver-client.crt"
else
  echo "[w6] reusing $TLS/client-ca.crt"
fi
if [ "${1:-}" = "--bad-ca" ] && [ ! -f "$TLS/bad-ca.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout "$TLS/bad-ca.key" -out "$TLS/bad-ca.crt" \
    -subj "/CN=some-other-ca" -addext "basicConstraints=critical,CA:TRUE"
  echo "[w6] generated bad-ca.crt"
fi

sudo install -o root -g root -m 0644 "$TLS/apiserver-client.crt" "$ETC/apiserver-client.crt"
sudo install -o root -g root -m 0600 "$TLS/apiserver-client.key" "$ETC/apiserver-client.key"

sed -e 's|^\(    user: \){}.*|\1\n      client-certificate: /etc/kubernetes/webhook/apiserver-client.crt\n      client-key: /etc/kubernetes/webhook/apiserver-client.key|' \
    "$REPO/authn/webhook-kubeconfig.yaml" > "$WORK/webhook-kubeconfig.mtls.yaml"

echo "[w6] $ETC:"; ls -la "$ETC"
echo "[w6] rendered $WORK/webhook-kubeconfig.mtls.yaml:"; cat "$WORK/webhook-kubeconfig.mtls.yaml"
openssl verify -CAfile "$TLS/client-ca.crt" "$TLS/apiserver-client.crt"
openssl x509 -in "$TLS/apiserver-client.crt" -noout -subject -ext extendedKeyUsage
