#!/usr/bin/env bash
# T1 — TSIG 키 생성 후 Secret poc-dns/externaldns-tsig 생성 (키는 git 에 커밋하지 않음)
# 호스트에 tsig-keygen 이 없으므로 openssl 로 32바이트 시크릿 생성 (hmac-sha256 과 동일 형식)
set -euo pipefail
NS=poc-dns
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
if kubectl -n "$NS" get secret externaldns-tsig >/dev/null 2>&1; then
  echo "Secret $NS/externaldns-tsig already exists — 재사용"
  exit 0
fi
SECRET=$(openssl rand -base64 32)
KEYFILE=$(mktemp)
cat > "$KEYFILE" <<K
key "externaldns" {
    algorithm hmac-sha256;
    secret "$SECRET";
};
K
kubectl -n "$NS" create secret generic externaldns-tsig \
  --from-file=externaldns.key="$KEYFILE" \
  --from-literal=tsig-secret="$SECRET" \
  --from-literal=tsig-keyname=externaldns \
  --from-literal=tsig-alg=hmac-sha256
rm -f "$KEYFILE"
echo "Secret $NS/externaldns-tsig created (keyname=externaldns alg=hmac-sha256)"
