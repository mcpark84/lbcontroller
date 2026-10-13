# PoC 시험 결과 — kubeconfig 토큰 인증 웹훅

> 대상 클러스터: gasandevcontrol (mgmt01 컨트롤 플레인 단일 + dev001, K8s v1.35.1)
> 시험일: 2026-10-13 (KST 13:00 전후, 로그 시각은 UTC) / 계획서 `PoC_auth_webook_plan.md`, 실행 계획 `PoC_auth_webhook_실행계획.md`
> 진행 상태: **S0~S9, S11 완료 (A0~A7, A9 합격), S10(A8 웹훅 중단) 대기 중**

---

## 배경 — 이 PoC 가 검증하는 Kubernetes 표준 동작

**웹훅 토큰 인증은 Kubernetes 공식 기능이다.** apiserver 플래그 `--authentication-token-webhook-config-file` 로 켜며, 외부 서버에 TokenReview(`authentication.k8s.io/v1`) 를 보내 "이 토큰이 누구인가" 를 묻는 방식이다. 자체 패치가 아니라 OIDC 와 같은 급의 내장 인증기다.

**자격 증명 종류에 따라 인증기가 갈린다.** kubeconfig 에 `client-certificate-data` 가 있으면 mTLS 로 x509 인증기(`--client-ca-file`)에서 끝나고, `token:` 이 있으면 kubectl 이 모든 요청에 `Authorization: Bearer <토큰>` 을 붙인다. kubectl 은 토큰이 무엇인지 모르고 문자열을 실어 보낼 뿐이다.

**Bearer 토큰은 인증기 체인을 순서대로 거친다.** apiserver 도 토큰만 보고는 종류를 알 수 없어, 설정된 인증기에 차례로 묻는다. 각 인증기는 "내 것 아님 / 인증됨 / 내 것인데 무효" 중 하나로 답한다.

| 순서 | 인증기 | 비고 |
|---|---|---|
| ① | 정적 토큰 파일 | 이 클러스터 미설정 |
| ② | 부트스트랩 토큰 | 형식 불일치 시 통과 |
| ③ | ServiceAccount 토큰(JWT) | **Pod 는 여기서 끝남** → 웹훅에 도달하지 않음 |
| ④ | OIDC | 이 클러스터 미설정 |
| ⑤ | **웹훅 TokenReview** | 앞에서 아무도 인정하지 않은 토큰만 도달 |

**인증(authn)과 인가(authz)는 별개 단계다.** 요청은 인증 → 인가(RBAC) → 어드미션 순으로 처리된다. 웹훅이 `authenticated: true` 를 돌려줘도 그 username / group 에 RBAC 바인딩이 없으면 인가 단계에서 **403** 이 난다.

| 응답 | 의미 |
|---|---|
| **401** Unauthorized | 누구인지 모름. 인증 실패 |
| **403** Forbidden + `User "poc-user"` | **누구인지는 알겠는데** 권한이 없음. 인증 성공, 인가 실패 |
| **200** | 인증 + 인가 통과 |

따라서 **1차 PoC 의 성공 신호는 200 이 아니라 403** 이다. 401 → 403 전환이 "웹훅이 체인에 붙었다" 는 증거이고, 200 은 RBAC 을 추가로 넣은 뒤의 부가 확인이다.

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

**배경**
- apiserver 는 kubelet 이 `/etc/kubernetes/manifests/` 를 감시해 띄우는 정적 Pod 다. 파일을 고치면 kubelet 이 자동으로 재기동하므로 설정 오류 시 API 가 전면 중단되고 `kubectl` 도 같이 죽는다. 복구는 노드에서 파일을 되돌리는 것뿐이다
- 백업을 같은 디렉터리에 두면 kubelet 이 **또 하나의 정적 Pod 매니페스트**로 인식한다. 반드시 디렉터리 밖에 둔다
- 기존 admin kubeconfig 는 인증서 기반이라 웹훅과 무관하게 동작한다. PoC 중 유일한 복구 경로이므로 보존한다

| 항목 | 결과 |
|---|---|
| 백업 | `~/kube-apiserver.yaml.bak` 4603 바이트, sha256 `a91b447d…197b`, 라이브와 diff 없음 |
| 위치 | `/etc/kubernetes/manifests/` 밖 (홈 디렉터리). kubelet 중복 인식 없음 |
| 저장소 | mgmt01 `~/lbcontroller` 를 git pull 로 동기 |

**판정: 합격.**

---

## 2. S1 — 웹훅 서버 / 스크립트 (로컬 작업, 커밋 80fc526)

**배경**
- apiserver 의 웹훅 클라이언트는 **HTTPS 만** 허용한다. 평문 HTTP 서버는 설정 자체가 거부된다. 자체 서명 인증서를 만들고 그 인증서를 CA 로 등록하면 된다
- 웹훅을 Pod 로 띄우면 "apiserver 가 뜨려면 웹훅이 필요하고, 웹훅 Pod 가 뜨려면 apiserver 가 필요한" 순환 의존이 생긴다. PoC 에서는 컨트롤 플레인 노드의 호스트 프로세스가 단순하다. apiserver Pod 는 `hostNetwork: true` 라 `127.0.0.1` 로 도달한다
- TokenReview 응답의 `status` 에는 TTL 필드가 없다. 캐시 수명은 apiserver 플래그 `--authentication-token-webhook-cache-ttl` 이 단독 결정한다
- `status.audiences` 는 요청의 `spec.audiences` 를 그대로 돌려준다. apiserver 는 자신의 audience 와 교집합이 없으면 인증을 거부하기 때문이다

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

**배경**
- 이 단계는 서버를 띄워 둘 뿐 apiserver 는 아직 그 존재를 모른다. 기존 사용자 영향은 없다
- apiserver 가 신뢰할 것은 `/etc/kubernetes/webhook/ca.crt` 하나다. 개인키는 apiserver 에 보여 줄 필요가 없으므로 웹훅 실행 계정의 홈에만 둔다

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

**배경**
- 웹훅이 아직 등록되지 않았으므로 `poc-token-0001` 은 체인 ①~④ 어디에서도 인정되지 않는다. 결과는 "누구인지 모름" = **401** 이어야 한다
- `--anonymous-auth=True` 여도 401 이다. 익명 처리는 **자격 증명을 아예 제시하지 않았을 때**만 적용되고, 무효한 토큰을 제시하면 거부된다
- 이 기준선이 있어야 S6 의 403 이 "웹훅 때문에 바뀐 것" 임을 말할 수 있다

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

**배경**
- `--authentication-token-webhook-version` 기본값은 `v1beta1` 이다. 웹훅 서버가 v1 로 응답하므로 명시해야 한다
- 플래그만 추가하고 **volume 마운트를 빠뜨리면** 컨테이너 안에서 설정 파일이 보이지 않아 apiserver 가 crash-loop 에 빠진다. 가장 흔한 실패이므로 렌더 스크립트가 플래그와 볼륨을 한 쌍으로 넣고, 참조 파일 존재까지 검사한다
- `--authentication-token-webhook-cache-ttl=0s` 는 PoC 한정이다. 매 요청이 웹훅에 가므로 호출 여부를 로그로 바로 확인할 수 있다. 운영 값은 2차 B3 에서 실측해 정한다
- 감사 로그는 apiserver 가 직접 파일로 쓰므로 정책 파일 마운트(읽기)와 로그 디렉터리 마운트(쓰기)가 둘 다 필요하다

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

**배경**
- 매니페스트를 바꾸면 kubelet 이 구 apiserver 를 graceful shutdown 한 뒤 새 컨테이너를 띄운다. 그동안 **인증서 / 토큰 구분 없이 모든 API 호출**이 실패한다. kubectl, 컨트롤러의 watch, 신규 스케줄링이 대상이다
- 실행 중인 Pod 와 컨테이너, Pod 간 트래픽, Service / Ingress 데이터 평면은 apiserver 와 무관하므로 영향이 없다. 컨트롤러들은 재시도 로직으로 자동 복귀한다
- 재기동 뒤 인증서 사용자가 웹훅과 무관함을 보이는 것이 A2 다. x509 인증기에서 끝나므로 웹훅 로그에 호출이 생기면 안 된다

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

**배경**
- 이제 체인 ⑤ 에 웹훅이 있다. `poc-token-0001` 은 ①~④ 를 통과해 웹훅에 도달하고, 1차 웹훅은 아무 문자열이나 `poc-user` 로 답한다. apiserver 입장에서 이 토큰은 **완전히 유효**하다. `--token any-string` 으로 바꿔도 같은 결과가 나온다
- 그러나 `poc-user` 와 그룹 `poc-tenant-a` 에는 아무 RBAC 바인딩이 없다. **인증은 통과하고 인가에서 막혀** 403 이 난다. 메시지에 `User "poc-user"` 가 박히는 것이 웹훅 신원을 apiserver 가 받아들였다는 증거다
- admin kubeconfig 가 200 인 이유는 인증서의 O 필드가 `system:masters` 그룹이고 거기에 cluster-admin 이 기본 바인딩돼 있기 때문이다. 웹훅 신원도 같은 RBAC 규칙을 따른다는 것을 S7 에서 확인한다
- apiserver 는 인증된 모든 사용자에게 `system:authenticated` 그룹을 자동으로 더한다

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

## 9. S7 = A5 — 인가 부여 (403 → 200)

**배경**
- 웹훅이 돌려준 신원(`poc-user`, 그룹 `poc-tenant-a`)은 RBAC 입장에서 다른 사용자와 동등하다. 그룹에 ClusterRoleBinding 을 걸면 그 범위만큼만 열린다
- 기본 ClusterRole `view` 는 nodes 권한이 없어 `get nodes` 가 200 이 되지 않는다. nodes get/list/watch 만 가진 ClusterRole 을 따로 만들어 바인딩했다
- 권한을 주지 않은 `get pods -A` 가 여전히 403 이면 "웹훅 신원에도 RBAC 범위가 그대로 적용된다" 는 증거가 된다

**절차 / 결과 (06:27:32Z)**
```
$ kubectl apply -f authn/rbac/poc-authn-node-reader.yaml
clusterrole.rbac.authorization.k8s.io/poc-authn-node-reader created
clusterrolebinding.rbac.authorization.k8s.io/poc-authn-node-reader created

$ kubectl --kubeconfig ~/poc-authn/token.kubeconfig get nodes
NAME     STATUS   ROLES           AGE   VERSION
dev001   Ready    <none>          77d   v1.35.1          # 200
mgmt01   Ready    control-plane   77d   v1.35.1

$ kubectl --kubeconfig ~/poc-authn/token.kubeconfig get pods -A
Error from server (Forbidden): pods is forbidden: User "poc-user" cannot list resource "pods" ...   # 403

$ kubectl --kubeconfig ~/poc-authn/token.kubeconfig auth can-i list nodes   → yes
$ ... auth can-i list pods -A   → no
$ ... auth can-i delete nodes   → no
```
감사 로그의 인가 사유: `RBAC: allowed by ClusterRoleBinding "poc-authn-node-reader" of ClusterRole "poc-authn-node-reader" to Group "poc-tenant-a"`.

**판정: 합격.** 401 → 403 → 200 전 구간 성립 (목표 A1 달성).

---

## 10. ★ 발견 — apiserver 전역 토큰 성공 캐시 10초 (B3 설계에 직결)

**배경**
- `--authentication-token-webhook-cache-ttl=0s` 는 **웹훅 인증기 앞의 캐시**만 끈다
- apiserver 에는 Bearer 토큰 인증기 체인 전체를 감싸는 **전역 토큰 캐시**가 하나 더 있다. 성공 결과를 **10초** 보관하며, 플래그로 노출되지 않는다 (코드상 `TokenSuccessCacheTTL` 기본값 10s, 실패는 0s = 미캐시). `kube-apiserver --help` 에 관련 플래그가 없음을 확인했다

**실측** (웹훅 로그의 `authenticated=` 행 수 증가분으로 계수)

| 시험 | 호출 패턴 | 웹훅 호출 수 |
|---|---|---|
| S7 직후 | kubectl 5회 연속 (nodes, pods, can-i 3회) = API 요청 7건 | **1** |
| A | `get nodes` 2회, 2초 간격 | **1** |
| B | `get nodes` 2회, 11초 간격 | **2** |
| C | `get nodes` 3회, 1초 간격 | 1 (직전 호출의 캐시 창 안) |

→ 같은 토큰으로 10초 안에 들어오는 요청은 웹훅에 가지 않는다. 10초가 지나면 다시 간다.

**설계 시사점 (★ 2차 B3 전제 수정)**
- **취소 즉시성의 하한은 웹훅 TTL 과 무관하게 10초다.** `--authentication-token-webhook-cache-ttl=0s` 로 해도 취소 후 최대 10초간 기존 토큰이 통과한다. 운영 문서에 "취소 반영 최대 10초 + 웹훅 캐시 TTL" 로 명시해야 한다
- 반대로 성능 면에서는 유리하다. 같은 사용자가 연속으로 날리는 요청은 10초에 1번만 웹훅에 온다. B3 의 "TTL 0s 일 때 초당 웹훅 호출 수" 측정은 이 캐시를 전제로 해석해야 한다
- 그룹 변경(S8)이나 취소 시험은 **반드시 11초 이상 기다린 뒤** 판정해야 한다. 10초 안에 재시도하면 캐시된 옛 결과를 보고 오판한다

---

## 11. S8 = A6 — 그룹 매핑 검증

**배경**
- 인가는 웹훅이 돌려주는 `groups` 에 걸려 있다. 웹훅 응답의 그룹을 바꾸면 같은 토큰, 같은 사용자라도 RBAC 결과가 바뀌어야 한다
- 위 10초 캐시 때문에 변경 직후에는 옛 그룹이 유지된다. 이것도 함께 관찰했다

**절차 / 결과**
```
$ WEBHOOK_GROUPS=poc-tenant-b bash authn/scripts/w1-run-webhook.sh restart     # 06:29:41Z
--- 직후 (캐시 창 안)  06:29:43  get nodes → 200  (옛 그룹 poc-tenant-a 캐시)
--- 11초 후           06:29:54  get nodes → 403  "User "poc-user" cannot list resource "nodes""
                                 auth whoami → Groups [poc-tenant-b system:authenticated]
$ bash authn/scripts/w1-run-webhook.sh restart                                   # poc-tenant-a 복원
--- 11초 후           06:30:07  get nodes → 200
```
감사 로그에도 06:29:54 요청은 `groups=['poc-tenant-b', 'system:authenticated'] code=403`, 06:30:07 은 `poc-tenant-a … 200` 으로 남았다.

**판정: 합격.** 바인딩 없는 그룹이면 403, 복원하면 200. 그룹 전환은 10초 캐시 만료 후 반영.

---

## 12. S9 = A7 — ServiceAccount 토큰 무영향

**배경**
- Pod 가 쓰는 SA 토큰은 JWT 이고, 체인 ③ ServiceAccount 인증기가 서명을 검증해 끝낸다. 웹훅(⑤)에는 도달하지 않아야 한다
- 이것이 "웹훅 장애 시 Pod / 컨트롤러는 무영향" 이라는 설계 주장의 근거다

**절차 / 결과** — 기존 Pod `poc-lb/mock-lb-c5664cdc6-7gcvw` (SA `mock-lb`) 안에서 python urllib 로 in-cluster API 호출
```
/api/v1/namespaces/poc-lb/services?limit=1 -> 200
/api/v1/nodes?limit=1 -> 403 nodes is forbidden: User "system:serviceaccount:poc-lb:mock-lb" ...
token: prefix eyJhbGciOi / JWT segments 3 / len 1166
webhook calls delta during SA test: 0
```
SA 는 자기 RBAC 대로 동작하고(services 200, nodes 403) **웹훅 호출은 0건**. 감사 정책상 SA 요청은 기록 대상이 아니라 감사 로그에도 없음 (0건).

**부수 확인 — Pod 에서 임의 문자열 토큰을 보내면**
```
Authorization: Bearer pod-sent-opaque-token  →  /api/v1/nodes  ->  200
webhook calls delta: 1   (token=pod-sent... authenticated=True user=poc-user)
```
JWT 가 아닌 토큰은 ③ 을 통과해 웹훅에 도달하고, 1차 allow-all 웹훅은 이를 `poc-user` 로 인정했다. 감사 로그에 `user=poc-user, userAgent=Python-urllib/3.12, sourceIPs=[10.233.91.114]` (Pod IP) 로 기록됨.

**판정: 합격 (A7).** 부수 확인은 결함이 아니라 1차 웹훅의 의도된 동작이지만, 아래 시사점을 남긴다.

**설계 시사점**
- 웹훅 인증기는 **클러스터 전체**에 걸린다. 외부 사용자뿐 아니라 Pod 안에서 보낸 임의 토큰도 웹훅에 도달한다. 2차에서 발급 목록 조회로 바꾸는 순간 이 경로는 닫히지만, **웹훅의 토큰 검증이 곧 클러스터의 마지막 방어선**이라는 뜻이다. allow-all 웹훅을 운영 클러스터에 올리는 일은 절대 없어야 한다
- 감사 로그의 `sourceIPs` 와 `userAgent` 로 "같은 username 이지만 Pod 에서 온 요청" 을 구별할 수 있다

---

## 13. S11 = A9 — 감사 로그

**배경**
- 감사 로그는 인증 결과(username / uid / groups)와 인가 결정을 요청 단위로 남긴다. 웹훅 신원이 감사에 그대로 반영되는지가 K51 요건이다
- 정책은 poc 신원만 Metadata, 나머지 None 으로 두었다. 인증 실패(401)는 user 가 비어 정책에 걸리지 않으며 웹훅 자체 로그로 추적한다

**결과** — `/var/log/kubernetes/audit/audit.log`, 적용 후 약 27분간 **27건 전부 poc-user** (poc-tenant-a 25건, poc-tenant-b 2건). 다른 사용자 / SA / 컨트롤러 요청은 0건 (정책대로).

대표 이벤트 (06:30:38Z, Pod 발 임의 토큰 요청):
```json
{ "level": "Metadata", "stage": "ResponseComplete",
  "requestURI": "/api/v1/nodes?limit=1", "verb": "list",
  "user": { "username": "poc-user", "uid": "poc-1", "groups": ["poc-tenant-a", "system:authenticated"] },
  "sourceIPs": ["10.233.91.114"], "userAgent": "Python-urllib/3.12",
  "responseStatus": { "code": 200 },
  "annotations": { "authorization.k8s.io/decision": "allow",
                   "authorization.k8s.io/reason": "RBAC: allowed by ClusterRoleBinding \"poc-authn-node-reader\" of ClusterRole \"poc-authn-node-reader\" to Group \"poc-tenant-a\"" } }
```
시간순으로 401(미기록) → 403(06:07:04, 06:15:57, 06:18:40) → 200(06:27:32 이후) → 그룹 변경 403(06:29:54) → 복원 200 이 그대로 남아 있어, 이 결과서의 A3 / A5 / A6 판정을 감사 로그만으로 재구성할 수 있다.

**판정: 합격.** `user.username=poc-user`, `uid`, `groups`, 인가 사유, 소스 IP, UA 모두 기록.

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
| S7 = A5 | RBAC 바인딩 후 `get nodes` 200, `get pods -A` 403 | 합격 |
| 발견 | 전역 토큰 성공 캐시 10초 (웹훅 TTL 0s 와 무관) | ★ B3 전제 수정 |
| S8 = A6 | 그룹 변경 → 403, 복원 → 200 (10초 캐시 후 반영) | 합격 |
| S9 = A7 | SA 토큰은 웹훅 미호출, SA RBAC 대로 동작 | 합격 |
| S10 = A8 | 웹훅 중단 영향 | 미실시 |
| S11 = A9 | 감사 로그에 poc-user / uid / groups / 인가 사유 기록 | 합격 |

## B. 현재 호스트 / 클러스터 상태

| 위치 | 상태 |
|---|---|
| `/etc/kubernetes/manifests/kube-apiserver.yaml` | **웹훅 + 감사 플래그 적용됨** (2026-10-13T06:03Z) |
| `~/kube-apiserver.yaml.bak` | 원본 백업 |
| `~/poc-authn/` | `tls/`, `webhook.log`, `webhook.pid`, `token.kubeconfig`, `kube-apiserver.new.yaml`, `readyz.log` |
| `/var/log/kubernetes/audit/audit.log` | 생성됨 (root 600) |
| `/etc/kubernetes/webhook/` | `ca.crt`, `webhook-kubeconfig.yaml`, `audit-policy.yaml` |
| 웹훅 프로세스 | pid 304931, `127.0.0.1:8443`, nohup, groups=poc-tenant-a (재부팅 시 `w1-run-webhook.sh start` 로 수동 재기동) |
| 클러스터 리소스 | ClusterRole + ClusterRoleBinding `poc-authn-node-reader` (2026-10-13T06:27Z) |
