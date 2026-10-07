#!/usr/bin/env bash
# T2 — poc-dns/externaldns-tsig Secret 을 poc-edns 로 복사 (Secret 은 네임스페이스 간 참조 불가)
set -euo pipefail
kubectl get ns poc-edns >/dev/null 2>&1 || kubectl create ns poc-edns
kubectl -n poc-dns get secret externaldns-tsig -o json \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); m=s["metadata"]; [m.pop(k,None) for k in ("namespace","uid","resourceVersion","creationTimestamp","managedFields","annotations")]; print(json.dumps(s))' \
  | kubectl -n poc-edns apply -f -
echo "Secret poc-edns/externaldns-tsig synced"
