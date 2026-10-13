# PoC 시험 결과 — kubeconfig 토큰 인증 웹훅

> 대상 클러스터: gasandevcontrol (mgmt01 컨트롤 플레인 단일 + dev001, K8s v1.35.1)
> 시험일: 2026-10-13 (KST 13:00 전후, 로그 시각은 UTC) / 계획서 `PoC_auth_webook_plan.md`, 실행 계획 `PoC_auth_webhook_실행계획.md`
> 진행 상태: **S0~S6 완료 (A0~A4 합격), S7(A5 RBAC) 대기 중**

---

## 0. 사전 확인 (2026-10-12, 읽기 전용)

| 항목 | 확인값 | 의미 |
|---|---|---|
| 웹훅 슬롯 | `--authentication-token-webhook-*` 없음 | 비어 있음 |
| 감사 로그 | `--audit-*` 없음, `/var/log/kubernetes` 없음 | A9 위해 신규 추가 |
| 인증서 경로 | `--client-ca-file=/etc/kubernetes/ssl/ca.crt`, admin.conf 는 인증서 기반 | 복구 경로 |
| audience | `--api-audiences` 없음 → issuer `https://kubernetes.default.svc.cluster.local` | 웹훅은 `spec.audiences` echo |
| apiserver Pod | `hostNetwork: true`, `--secure-port=6443`, `--anonymous-auth=True` | `127.0.0.1:8443` 도달 가능 |
| apiserver 기동 시각 | 2026-07-27T09:44:44Z (매니페스트 수정일과 동일, 77일 무변경) | |
| 호스트 도구 | python3 3.12.3 / PyYAML 6.0.1 / openssl / crictl / ctr | |
| 공유 사용 | `litellm`(전날 생성), `inferium`, `team-b`, `dynamo-system` 등 활성 | 재기동 전 공지 |

**확정 결정**: 감사 로그를 웹훅과 같은 매니페스트 수정 1회에 포함 / A10 롤백은 2차 종료 후 / 웹훅은 nohup 호스트 프로세스 / 캐시 TTL `0s`.

---

## 1. S0 — 안전장치

| 항목 | 결과 |
|---|---|
| 백업 | `~/kube-apiserver.yaml.bak` 4603 바이트, sha256 `a91b447d…197b`, 라이브와 diff 없음 |
| 위치 | `/etc/kubernetes/manifests/` 밖 (홈 디렉터리). kubelet 중복 인식 없음 |
| 저장소 | mgmt01 `~/lbcontroller` 를 git pull 로 동기 |

**판정: 합격.**

---

## 2. S1 — 웹훅 서버 / 스크립트 (로컬 작업, 커밋 80fc526)

| 파일 | 역할 |
|---|---|
| `authn/webhook_server.py` | 1차 allow-all TokenReview 웹훅. python 표준 라이브러리, HTTPS 전용, `POST /authenticate`, `GET /healthz` |
| `authn/webhook-kubeconfig.yaml` | apiserver → 웹훅 접속 설정 |
| `authn/audit-policy.yaml` | poc 신원만 Metadata, 나머지 None |
| `authn/rbac/poc-authn-node-reader.yaml` | A5 용 ClusterRole(nodes get/list/watch) + 그룹 바인딩 |
| `authn/scripts/w1-setup-webhook.sh` | 인증서 생성 + `/etc/kubernetes/webhook` 구성 |
| `authn/scripts/w1-run-webhook.sh` | nohup start / stop / status |
| `authn/scripts/w3-make-token-kubeconfig.sh` | 토큰 kubeconfig 생성 |
| `authn/scripts/w2-render-apiserver.py` | 새 매니페스트 렌더 + 검증 (원본 미수정) |
| `authn/scripts/w2-watch-readyz.sh` | `/readyz` 1초 폴링, 다운타임 측정 |

**로컬 자가 시험 (macOS, 포트 18443)**

| 요청 | 응답 |
|---|---|
| `GET /healthz` | 200 `{"status":"ok","mode":"allow-all",…}` |
| TokenReview (apiserver 와 같은 형태, audiences 포함) | `authenticated: true`, user `poc-user` / `poc-1` / `[poc-tenant-a]`, `status.audiences` 요청값 echo |
| 빈 토큰 | `authenticated: false`, `error: empty token` |
| `/other` 경로 | 404, 로그 없음 |
| 평문 HTTP | TLS 핸드셰이크 실패 (curl rc=56) |
| 로그 | 요청당 1행: 시각 / addr / 토큰 앞 8자 / auds / 결과 / user / groups / ms |

**렌더 스크립트 (백업 매니페스트 대상)**: +12줄, PyYAML 재파싱 정상(command 50개, volumes 8, mounts 8), 이미 적용된 파일에 재실행 시 거부.

> 계획 보정: `view` ClusterRole 은 nodes 권한이 없어 A5 는 `poc-authn-node-reader` 로 대체. `get pods -A` 가 여전히 403 인 것으로 권한 범위를 함께 확인.

**판정: 합격.**

---

## 3. S2 — 웹훅 기동 (mgmt01 호스트)

```
$ bash authn/scripts/w1-setup-webhook.sh
[w1] generated /home/ubuntu/poc-authn/tls/tls.crt
/etc/kubernetes/webhook:
-rw-r--r-- root root  426 audit-policy.yaml
-rw-r--r-- root root 1159 ca.crt
-rw-r--r-- root root  574 webhook-kubeconfig.yaml
subject=CN = poc-authn-webhook  notBefore=Oct 13 04:17:04 2026  notAfter=Oct 13 04:17:04 2027
X509v3 Subject Alternative Name: IP Address:127.0.0.1

$ bash authn/scripts/w1-run-webhook.sh start
[w1] running pid 113484, started Tue Oct 13 04:17:04 2026
{"status": "ok", "mode": "allow-all", "username": "poc-user", "groups": ["poc-tenant-a"]}
```

apiserver 가 사용할 `/etc/kubernetes/webhook/ca.crt` 로 TokenReview 자가 시험:
```
{"apiVersion":"authentication.k8s.io/v1","kind":"TokenReview","status":{"authenticated":true,
 "user":{"username":"poc-user","uid":"poc-1","groups":["poc-tenant-a"]},
 "audiences":["https://kubernetes.default.svc.cluster.local"]}}
```
웹훅 로그:
```
2026-10-13T04:17:04 INFO [start] mode=allow-all bind=127.0.0.1:8443 username=poc-user uid=poc-1 groups=['poc-tenant-a']
2026-10-13T04:17:05 INFO addr=127.0.0.1 token=poc-toke... len=14 auds=['https://kubernetes.default.svc.cluster.local'] authenticated=True user=poc-user groups=['poc-tenant-a'] ms=0.03
```
`ss -ltnp`: `127.0.0.1:8443 python3 pid=113484`.

- 개인키는 `~/poc-authn/tls/tls.key` (ubuntu 소유 600). `/etc/kubernetes/webhook` 에는 공개 인증서만 둠
- 이 시점까지 **기존 사용자 영향 없음** (apiserver 가 아직 웹훅을 호출하지 않음)

**판정: 합격.**

---

## 4. S3 = A0 — 설정 전 기준선

```
$ bash authn/scripts/w3-make-token-kubeconfig.sh poc-token-0001
[w3] wrote /home/ubuntu/poc-authn/token.kubeconfig (server=https://172.16.1.120:6443 token=poc-toke...)

$ date -u; kubectl --kubeconfig ~/poc-authn/token.kubeconfig get nodes
2026-10-13T04:17:22Z
error: You must be logged in to the server (Unauthorized)      # rc=1
```
웹훅 로그 `authenticated=` 행 수: 1 (S2 자가 시험 1건뿐, 이 요청으로 인한 호출 없음).

**판정: 합격 — 401 Unauthorized, 웹훅 미호출.** `--anonymous-auth=True` 여도 무효 토큰 제시 시에는 401.

---

## 5. S4 — apiserver 매니페스트 렌더 / 사전 검증 (적용 전)

```
$ python3 authn/scripts/w2-render-apiserver.py ~/kube-apiserver.yaml.bak ~/poc-authn/kube-apiserver.new.yaml
[w2] wrote /home/ubuntu/poc-authn/kube-apiserver.new.yaml (+12 lines)
[w2] yaml ok: 50 command args, 8 volumes, 8 mounts, hostNetwork=True
[w2] host files present: [webhook-kubeconfig.yaml, ca.crt, audit-policy.yaml]
```

| 검증 | 결과 |
|---|---|
| YAML 재파싱 | 정상 |
| 플래그명 8개가 `kube-apiserver v1.35.1 --help` 에 존재 | 전부 존재 (`ctr -n k8s.io run --rm registry.k8s.io/kube-apiserver:v1.35.1 … --help` 로 대조) |
| `--authentication-token-webhook-version` 기본값 | **`v1beta1`** → `v1` 명시 필수 확인 |
| 참조 호스트 파일 3개 | 존재 |
| 감사 정책 / 웹훅 kubeconfig 파싱 | 정상 (`audit.k8s.io/v1 Policy` 3 rules / `server: https://127.0.0.1:8443/authenticate`) |
| 라이브 매니페스트 vs 백업 | 동일 (아직 미적용) |

**적용 예정 diff**
```diff
     - --event-ttl=1h0m0s
+    - --authentication-token-webhook-config-file=/etc/kubernetes/webhook/webhook-kubeconfig.yaml
+    - --authentication-token-webhook-version=v1
+    - --authentication-token-webhook-cache-ttl=0s
+    - --audit-policy-file=/etc/kubernetes/webhook/audit-policy.yaml
+    - --audit-log-path=/var/log/kubernetes/audit/audit.log
+    - --audit-log-maxage=3
+    - --audit-log-maxbackup=3
+    - --audit-log-maxsize=50
     volumeMounts:
+    - mountPath: /etc/kubernetes/webhook
+      name: poc-webhook
+      readOnly: true
+    - mountPath: /var/log/kubernetes/audit
+      name: poc-audit
   volumes:
+  - hostPath:
+      path: /etc/kubernetes/webhook
+      type: Directory
+    name: poc-webhook
+  - hostPath:
+      path: /var/log/kubernetes/audit
+      type: DirectoryOrCreate
+    name: poc-audit
```

**판정: 사전 검증 합격. 적용(S5)은 사용자 승인 대기.**

---

## 6. 영향 범위 정리 (S5 전 확인)

| 단계 | 기존 사용자 영향 |
|---|---|
| S2~S4 | 없음 |
| S5 적용 | **모든 API 호출 10~30초 중단** (kubelet 의 apiserver 재기동). 실행 중 Pod / 트래픽은 유지 |
| S5 이후 | 없음. 인증서 사용자는 x509 인증기에서 끝나고 웹훅은 체인 마지막 |

**S5 절차**: `/readyz` 폴링 시작 → `sudo cp ~/poc-authn/kube-apiserver.new.yaml /etc/kubernetes/manifests/kube-apiserver.yaml` → 90초 내 200 복귀 확인 → 실패 시 `sudo cp ~/kube-apiserver.yaml.bak /etc/kubernetes/manifests/kube-apiserver.yaml`.

---

## 7. S5 = A1, A2 — apiserver 적용 / 다운타임 / 인증서 경로 생존

**적용 시각**: 2026-10-13T06:03:10.186Z (`sudo install -m 0600 ~/poc-authn/kube-apiserver.new.yaml /etc/kubernetes/manifests/kube-apiserver.yaml`)

**A1 — 재기동 관측 (`/readyz` 1초 폴링, 비인증)**

| 시각(UTC) | 상태 |
|---|---|
| 06:03:10.221 | 첫 비정상 응답 (500 → 이후 연결 거부 000) |
| ~06:03:40 | 구 컨테이너 `d34503e1f225` 소멸 (graceful shutdown 약 30초) |
| 06:03:47 | 새 컨테이너 `b69ba7de7276` 기동 로그 시작 |
| 06:03:48.681 | 마지막 비정상 응답 (403 → 500, 기동 중) |
| 06:03:49.694 | **200 복귀** |

- **다운타임 약 39.5초** (비정상 샘플 39개). 예상치 10~30초보다 길었고, 대부분은 구 apiserver 의 graceful shutdown 대기였다
- 15초 안정화 후 `crictl ps -a`: 새 컨테이너 1개 Running, ATTEMPT 0 → **crash-loop 없음**
- 실행 중 컨테이너 인자에 플래그 8개 전부 확인 (`crictl inspect`)
- 재기동 직후 apiserver 로그에 dynamo-operator 의 admission 웹훅 타임아웃 경고(`failing open`) 1회 → 기존 컴포넌트의 재연결 과도기, 본 변경과 무관

**A2 — 인증서 경로**
```
$ kubectl get nodes          # admin.conf (x509)
dev001   Ready   <none>          77d   v1.35.1
mgmt01   Ready   control-plane   77d   v1.35.1
```
웹훅 호출 수 변화 없음 (1 → 1). 인증서 사용자는 웹훅에 도달하지 않는다.
감사 로그 `/var/log/kubernetes/audit/audit.log` 생성됨 (0 바이트, 아직 poc 신원 요청 없음).

**판정: A1 합격 (다운타임 39.5초 기록), A2 합격.**

---

## 8. S6 = A3, A4 — 인증 성립 (401 → 403)

### 8.1 1차 시도 실패와 원인 (06:04:28Z)

토큰 kubeconfig 로 `get nodes` → 여전히 **401**. 웹훅 로그에 호출 없음. apiserver 로그:
```
webhook.go:154] Failed to make webhook authenticator request: the server could not find the requested resource
authentication.go:75] "Unable to authenticate the request" err="[invalid bearer token, the server could not find the requested resource]"
"Unsolicited response received on idle HTTP channel starting with \"<!DOCTYPE HTML>..."
```
`kubectl create -f TokenReview` (admin) 로 같은 체인을 태워도 `status.error: [invalid bearer token, the server could not find the requested resource]`.

**원인**: apiserver(client-go) 는 `POST /authenticate?timeout=30s` 로 호출한다. 웹훅 서버가 경로를 `/authenticate` 와 **정확히 일치**로 비교해 404 를 돌려줬고, 404 응답 시 요청 본문을 읽지 않아 남은 바이트가 keep-alive 연결의 다음 요청 줄로 해석되어 HTML 400 이 추가로 나갔다(= "Unsolicited response"). 404 는 로그에도 남지 않았다.

**조치 (커밋 8fffbfc, 웹훅 서버만 수정, apiserver 무변경)**: 쿼리를 떼고 경로 비교 / 모든 응답 전 본문 소진 / 모든 요청 줄 로그. 웹훅 재기동 (pid 264954, 06:07:03Z).

### 8.2 재시도 (06:07:04Z)
```
$ kubectl --kubeconfig ~/poc-authn/token.kubeconfig get nodes
Error from server (Forbidden): nodes is forbidden: User "poc-user" cannot list resource "nodes" in API group "" at the cluster scope

$ kubectl --kubeconfig ~/poc-authn/token.kubeconfig auth whoami
Username    poc-user
UID         poc-1
Groups      [poc-tenant-a system:authenticated]
```
웹훅 로그 (A4):
```
06:07:04 INFO addr=127.0.0.1 token=poc-toke... len=14 auds=['https://kubernetes.default.svc.cluster.local'] authenticated=True user=poc-user groups=['poc-tenant-a'] ms=0.04
06:07:04 INFO http 127.0.0.1 "POST /authenticate?timeout=30s HTTP/1.1" 200 -
```

**판정: A3 합격 ★ (401 → 403, `User "poc-user"`), A4 합격 (토큰 앞 8자 `poc-toke` 일치).**

**설계 시사점**
- 웹훅 서버 규격에 **"경로는 쿼리 문자열을 무시하고 비교, `?timeout=<apiserver 설정값>` 이 붙는다"** 를 명시. apiserver 측 타임아웃 기본 30초가 쿼리로 전달되므로 서버 측 처리 시간 예산의 근거가 된다
- apiserver 는 `system:authenticated` 그룹을 자동 추가한다. 웹훅이 돌려준 그룹에 더해진다
- 웹훅 서버 자가 시험은 반드시 **apiserver 와 같은 클라이언트 동작**(쿼리 포함, keep-alive)으로 해야 한다. curl 단건 시험은 이 결함을 잡지 못했다

---

## A. 판정 요약

| 단계 | 항목 | 결과 |
|---|---|---|
| S0 | 백업 / 안전장치 | 합격 |
| S1 | 웹훅 서버 / 스크립트 / 렌더 검증 | 합격 |
| S2 | 웹훅 기동 (mgmt01) | 합격 |
| S3 = A0 | 기준선 401, 웹훅 미호출 | 합격 |
| S4 | 매니페스트 렌더 + 사전 검증 4종 | 합격 (적용 대기) |
| S5 = A1 | 적용, crash-loop 없음, 다운타임 **39.5초** | 합격 |
| S5 = A2 | 인증서 경로 생존, 웹훅 미호출 | 합격 |
| S6 = A3 | **401 → 403, `User "poc-user"`** | 합격 ★ (웹훅 경로 비교 결함 1회 수정 후) |
| S6 = A4 | 웹훅 로그에 TokenReview 수신, 토큰 앞자리 일치 | 합격 |
| S7~S11 = A5~A9 | | 미실시 |

## B. 현재 호스트 / 클러스터 상태

| 위치 | 상태 |
|---|---|
| `/etc/kubernetes/manifests/kube-apiserver.yaml` | **웹훅 + 감사 플래그 적용됨** (2026-10-13T06:03Z) |
| `~/kube-apiserver.yaml.bak` | 원본 백업 |
| `~/poc-authn/` | `tls/`, `webhook.log`, `webhook.pid`, `token.kubeconfig`, `kube-apiserver.new.yaml`, `readyz.log` |
| `/var/log/kubernetes/audit/audit.log` | 생성됨 (root 600) |
| `/etc/kubernetes/webhook/` | `ca.crt`, `webhook-kubeconfig.yaml`, `audit-policy.yaml` |
| 웹훅 프로세스 | pid 264954, `127.0.0.1:8443`, nohup (재부팅 시 `w1-run-webhook.sh start` 로 수동 재기동) |
| 클러스터 리소스 | 변경 없음 (RBAC 은 S7 에서 적용) |
