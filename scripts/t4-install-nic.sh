#!/usr/bin/env bash
# T4 — F5 NIC OSS 설치 (helm). 차트는 ~/charts 에 받아 둔 tgz 우선, 없으면 repo 사용.
set -euo pipefail
CHART=${CHART:-$HOME/charts/nginx-ingress-2.7.3.tgz}
[ -f "$CHART" ] || CHART=nginx-stable/nginx-ingress
VALUES="$(dirname "$0")/../manifests/nic-values.yaml"
helm upgrade --install nic "$CHART" -n nginx-ingress --create-namespace \
  --version 2.7.3 --skip-crds -f "$VALUES" --wait --timeout 180s
kubectl -n nginx-ingress get pods,svc -o wide
kubectl get ingressclass
