# PoC 계획서 — kubeconfig 토큰 인증 웹훅

> 대상 클러스터: gasandevcontrol (mgmt01 + dev001, K8s v1.35.1)
> 관련 설계 `ManagedK8S_인증방식_설계_r1_out.pptx` (20장) / API `ManagedK8S_API_목록_r1.md`
> 선행 사례 `PoC_ExternalDNS_계획서.md`

---

## 1. 전제 확인 — 인증 체인 동작

**질문**: 기존 key 기반 kubeconfig 은 기존 인증으로 통과하고, 거기서 통과 안 하면 웹훅으로 가는가?

**거의 맞으나 정확히는 "자격 증명 종류"가 먼저 갈린다.**

```
클라이언트 요청
   │
   ├─ 클라이언트 인증서 제시 (mTLS)  →  x509 인증기 (--client-ca-file)
   │                                      └─ 성공 시 종료. 웹훅에 도달하지 않음
   │
   └─ Authorization: Bearer <token>  →  Bearer 토큰 체인 (순차)
          ① static token file (설정 시)
          ② bootstrap token
          ③ ServiceAccount 토큰        ← Pod 는 여기서 끝남
          ④ OIDC (설정 시)
          ⑤ 웹훅 TokenReview           ← 앞에서 아무도 인정 안 한 토큰만 도달
```

**핵심 3가지**

- **기존 admin kubeconfig 은 인증서 기반**이므로 웹훅과 무관하게 계속 동작한다. **이것이 PoC 중 유일한 복구 수단**이므로 반드시 보존할 것
- **ServiceAccount 토큰은 웹훅보다 먼저 처리**된다 → 웹훅이 죽어도 Pod / 노드는 정상. "웹훅 장애 시 사람의 신규 인증만 차단"이라는 설계 주장이 여기서 성립
- 웹훅은 **체인의 마지막**이라 "아무도 모르는 토큰"만 받는다

**인증(authn)과 인가(authz)는 별개다.** 웹훅이 `authenticated: true` 를 돌려줘도 해당 username / group 에 RBAC 바인딩이 없으면 **403 Forbidden** 이 난다. **1차 PoC 의 성공 신호는 200 이 아니라 403** 이다 (401 → 403 전환 = 인증 성립).

---

## 2. 목표

| # | 목표 | 차수 |
|---|---|---|
| **A1** | 웹훅 인증기가 체인에 붙어 동작함을 실증 (401 → 403 → 200) | 1차 |
| **A2** | 인증서 경로와 토큰 경로가 **공존**함을 확인 | 1차 |
| **A3** | 토큰 발급 / 조회 / 취소 API 와 TokenReview 검증 로직 결합 | 2차 |
| **A4** | **취소 즉시성**과 캐시 TTL 의 상호작용 실측 | 2차 |
| **A5** | 웹훅 장애 시 영향 범위 실증 (사람만 차단되는가) | 2차 |

---

## 3. ★ 안전장치 — 착수 전 필수

apiserver 정적 Pod 매니페스트를 수정하므로 **설정 오류 시 클러스터 API 가 전면 중단**된다. `kubectl` 도 함께 죽으므로 복구는 노드에서만 가능하다.

| 항목 | 조치 |
|---|---|
| 공유 자산 여부 | 다른 작업자가 이 클러스터를 쓰는지 **사전 공지** |
| 매니페스트 백업 | `cp /etc/kubernetes/manifests/kube-apiserver.yaml ~/kube-apiserver.yaml.bak` (★ `/etc/kubernetes/manifests/` **밖**에 둘 것 — 안에 두면 kubelet 이 중복 Pod 로 인식) |
| 노드 접근 | **SSH 세션을 유지한 채** 작업. kubectl 이 죽어도 노드에서 복구 |
| 기존 admin kubeconfig | 인증서 기반이므로 웹훅과 무관하게 동작 — 유일한 복구 경로 |
| 관찰 수단 | `crictl ps -a \| grep apiserver`, `journalctl -u kubelet -f`, `/var/log/pods/` |

**롤백**
```bash
cp ~/kube-apiserver.yaml.bak /etc/kubernetes/manifests/kube-apiserver.yaml
# kubelet 이 변경을 감지해 자동 재기동 (보통 10~30초)
crictl ps | grep apiserver
```

---

## 4. 1차 PoC — 무조건 통과 웹훅

### 4.1 W1. 웹훅 서버 (무조건 OK)

- Python 표준 라이브러리 + 자체 서명 인증서. **apiserver 의 웹훅 클라이언트는 HTTPS 를 요구**하므로 평문 HTTP 불가
- 기동 위치: **컨트롤 플레인 노드 호스트** (`127.0.0.1:8443`). Pod 로 띄우면 클러스터 부트스트랩 시 순환 의존이 생기므로 PoC 에서는 호스트 프로세스가 단순

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout /etc/kubernetes/webhook/tls.key -out /etc/kubernetes/webhook/tls.crt \
  -subj "/CN=webhook" -addext "subjectAltName=IP:127.0.0.1"
cp /etc/kubernetes/webhook/tls.crt /etc/kubernetes/webhook/ca.crt
```

**TokenReview 요청 / 응답 규격 (v1)**

```jsonc
// 요청 (apiserver → 웹훅)
{ "apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
  "spec": { "token": "<평문 토큰>", "audiences": ["https://kubernetes.default.svc"] } }

// 응답 (1차 — 무조건 통과)
{ "apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
  "status": { "authenticated": true,
              "user": { "username": "poc-user", "uid": "poc-1",
                        "groups": ["poc-tenant-a"] } } }
```

- **`status` 에 TTL 필드는 없다.** 캐시 수명은 apiserver 플래그가 단독 결정
- 거부는 `{"status":{"authenticated":false}}`. 에러 메시지는 `status.error` 에

**서버 요구사항**
| 항목 | 내용 |
|---|---|
| 경로 | `POST /authenticate` |
| 로그 | 수신 토큰(앞 8자만), 응답 username / groups, 처리 시각 |
| 구현 | `http.server` + `ssl.SSLContext` (약 50행, 의존성 없음) |

### 4.2 W2. apiserver 설정

`/etc/kubernetes/manifests/kube-apiserver.yaml` 에 **플래그 3개 + 마운트 1쌍**을 추가한다.

```yaml
    - --authentication-token-webhook-config-file=/etc/kubernetes/webhook/webhook-kubeconfig.yaml
    - --authentication-token-webhook-version=v1          # ★ 기본값이 v1beta1 이므로 명시 필수
    - --authentication-token-webhook-cache-ttl=0s        # PoC 한정. 매 요청 웹훅 호출
```

```yaml
    volumeMounts:
    - { name: webhook, mountPath: /etc/kubernetes/webhook, readOnly: true }
  volumes:
  - { name: webhook, hostPath: { path: /etc/kubernetes/webhook, type: DirectoryOrCreate } }
```

> **가장 흔한 실패**: 플래그만 추가하고 **volume 마운트를 빠뜨리면** 컨테이너 안에서 설정 파일이 보이지 않아 apiserver 가 crash-loop 에 빠진다.

**웹훅 kubeconfig** (`/etc/kubernetes/webhook/webhook-kubeconfig.yaml`) — **의미가 반전된 kubeconfig**. `clusters` 가 웹훅 서버, `users` 가 apiserver 자신이다.

```yaml
apiVersion: v1
kind: Config
clusters:
  - name: webhook
    cluster:
      server: https://127.0.0.1:8443/authenticate
      certificate-authority: /etc/kubernetes/webhook/ca.crt
users:
  - name: apiserver
    user: {}                       # 1차는 웹훅이 클라이언트 인증을 요구하지 않음
contexts:
  - name: webhook
    context: { cluster: webhook, user: apiserver }
current-context: webhook
```

### 4.3 W3. 토큰 kubeconfig 생성

```yaml
apiVersion: v1
kind: Config
clusters:
  - name: poc
    cluster:
      certificate-authority-data: <기존 admin kubeconfig 의 클러스터 CA 그대로>
      server: https://<apiserver>:6443
users:
  - name: poc-user
    user: { token: poc-token-0001 }      # 1차는 아무 문자열이어도 통과
contexts:
  - name: poc
    context: { cluster: poc, user: poc-user }
current-context: poc
```

### 4.4 시험 항목

| # | 항목 | 절차 | 합격 기준 |
|---|---|---|---|
| **A0** | 설정 전 기준선 | 토큰 kubeconfig 로 `get nodes` | **401 Unauthorized** |
| **A1** | apiserver 재기동 | 플래그 적용 후 기동 확인 | crash-loop 없음, **다운타임 기록** |
| **A2** | 기존 경로 생존 | **admin kubeconfig** 로 `get nodes` | 정상. 웹훅 로그에 호출 **없음** (인증서 경로) |
| **A3** | **인증 성립** | 토큰 kubeconfig 로 `get nodes` | **403 Forbidden** + 메시지에 `User "poc-user"` ★ |
| **A4** | 웹훅 호출 확인 | 웹훅 서버 로그 | TokenReview 요청 수신, 토큰 앞자리 일치 |
| **A5** | **인가 부여** | `poc-tenant-a` 그룹에 ClusterRoleBinding(view) | `get nodes` **정상 응답** |
| **A6** | 그룹 매핑 검증 | 응답 groups 를 바꿔 재시도 | 바인딩 없는 그룹이면 다시 403 |
| **A7** | SA 토큰 무영향 | 임의 Pod 에서 in-cluster API 호출 | 정상. 웹훅 로그에 호출 **없음** |
| **A8** | 웹훅 중단 영향 | 웹훅 프로세스 종료 후 각 경로 시도 | 토큰 경로만 실패, admin / SA 는 정상 |
| **A9** | 감사 로그 | apiserver 감사 로그에서 해당 요청 | `user.username = poc-user` 기록 (K51) |
| **A10** | 롤백 | 백업 복원 후 상태 확인 | 플래그 제거, 토큰 경로 다시 401 |

**A3 이 1차 PoC 의 핵심.** 401 에서 403 으로 바뀌는 순간이 "인증 체인에 웹훅이 붙었다"는 증거다.

---

## 5. 2차 PoC — 토큰 발급 / 취소 연동

### 5.1 구현 범위

API 스켈레톤(`ManagedK8S_API_목록_r1.md`)의 접근 / 인증 4종과 **동일한 계약**으로 구현한다.

| 메서드 | 경로 | 내용 | 포탈 |
|---|---|---|---|
| POST | `/tokens` | 운영자 직접 발급. 평문은 **생성 시 1회만** 반환 | 운 |
| GET | `/tokens` | 소유자 ID / 테넌트 / 클러스터 / 발급 경로 / 만료 / 최종 사용 | 공 |
| DELETE | `/tokens/{id}` | 취소 (`?user=` 로 계정 단위 일괄) | 공 |
| GET | `/clusters/{id}/kubeconfig` | **발급 경로** — 토큰 생성 + endpoint / CA 조립 | 고 |

**토큰 저장 스키마 (최소)**

| 필드 | 용도 |
|---|---|
| `id` | 취소 대상 지정 |
| `hash` | **평문 미저장.** SHA-256 등 단방향 |
| `owner_id` | ★ 고객별 목록 필터 + 계정 비활성화 시 일괄 취소(K21) |
| `tenant`, `cluster` | audience 바인딩 |
| `issued_via` | `kubeconfig` / `operator` |
| `expires_at`, `revoked_at`, `last_used_at` | 만료 / 취소 / 사용 추적 |

**TokenReview 검증 로직**
```
평문 토큰 수신
  → 해시 계산 → 조회
  → 미존재 / 취소됨 / 만료 → authenticated:false
  → audience 가 이 클러스터와 불일치 → authenticated:false
  → last_used_at 갱신
  → username = owner_id, groups = <신원 시스템 그룹> 반환
```

> 2차에서 **그룹은 고정값**으로 둔다. Keycloak / OpenLDAP 연동은 3차 범위.

### 5.2 시험 항목

| # | 항목 | 합격 기준 |
|---|---|---|
| **B1** | 발급 → 사용 | `GET /kubeconfig` 로 받은 파일로 `kubectl` 성공 |
| **B2** | 평문 미저장 | 저장소에 평문 없음. 재조회 시 평문 미반환 |
| **B3** | 취소 즉시성 ★ | `DELETE` 후 **다음 요청부터** 401. 아래 상세 |
| **B4** | 만료 | `expires_at` 경과 후 401 |
| **B5** | audience 바인딩 | A 클러스터 토큰으로 B 클러스터 호출 시 401 |
| **B6** | 계정 단위 일괄 취소 | `?user=` 로 해당 소유자 토큰 전량 무효 (K21) |
| **B7** | 소유자 필터 | `GET /tokens` 가 소유자별로 분리되어 보임 |
| **B8** | 웹훅 장애 | 웹훅 중단 시 **신규 인증만 차단**, 기존 SA / Pod 정상 (A5 재확인) |
| **B9** | 감사 추적 | 발급 / 취소 / 인증 성공·실패가 감사 로그에 기록 (K51) |

### 5.3 B3 상세 — 취소 즉시성과 캐시 TTL ★

**가장 중요한 항목.** `--authentication-token-webhook-cache-ttl` 기본값은 **2분**이다.

```
--cache-ttl=2m  : 취소해도 최대 2분간 기존 결과가 재사용되어 통과
--cache-ttl=0s  : 매 요청 웹훅 호출. 취소 즉시 반영되나 호출량 증가
```

**측정 항목**
1. TTL `2m` 에서 취소 후 차단까지 걸린 실제 시간
2. TTL `0s` 에서 같은 측정
3. TTL `0s` 일 때 평상시 **초당 웹훅 호출 수** (성능 설계 근거)

**부수 확인 — 장기 연결은 재인증되지 않는다.**
`kubectl logs -f` / `exec` / `watch` 를 띄운 상태에서 토큰을 취소하고, **기존 연결이 끊기는지** 확인한다. 끊기지 않는 것이 정상 동작이며, 운영 문서에 "취소는 신규 요청만 차단"으로 명시해야 한다.

---

## 6. 산출물

1. **캐시 TTL 결정서** — 취소 반영 지연 vs 웹훅 호출량 실측값 기반
2. **웹훅 서버 규격 초안** — 요청 / 응답, 타임아웃, 실패 시 동작
3. **운영 런북** — 웹훅 장애 시 영향 범위와 대응 (ExternalDNS 런북과 동일 형식)
4. **apiserver 설정 변경 절차서** — 플래그 / 마운트 / 롤백
5. **NKE 확인 질문 구체화** — 아래 §7

---

## 7. ★ 본 PoC 로 확인할 수 없는 것 — NKE 반영 여부

PoC 클러스터는 kubeadm 기반이라 매니페스트를 직접 고칠 수 있다. **운영 환경(NKE)에서는 이것이 불가능할 수 있다.**

| # | 질문 | 미지원 시 대안 |
|---|---|---|
| **N1** | **웹훅 인증기 슬롯이 비어 있는가** — apiserver 는 `--authentication-token-webhook-config-file` 을 **하나만** 받는다. NKE 가 자체 인증 웹훅을 이미 설정해 두었다면 우리가 들어갈 자리가 없다 | apiserver 앞단 **인증 프록시** (유일한 대안) |
| **N2** | **설정 주입 경로가 있는가** — 클러스터 생성 API 가 authentication 파라미터를 받는가, 받지 않으면 골든 이미지의 kubeadm 설정(`apiServer.extraArgs` / `extraVolumes`)을 반영하는가. **패치 업그레이드 시 보존되는가** | 인증 프록시 / 업그레이드 후 재주입 자동화 |
| **N3** | **endpoint 와 serving CA 를 조회하는 API 가 있는가** — `GET /clusters/{id}/kubeconfig` 가 이 둘을 읽어 조립한다. 조회 수단이 없으면 발급 자체가 불가 | 클러스터 생성 시 반환값을 포탈 DB 에 보관 |

**N1 이 가장 치명적이다.** 슬롯이 이미 점유되어 있으면 2차 PoC 결과와 무관하게 **웹훅 방식 자체가 성립하지 않고** 인증 프록시로 전환해야 한다.

> N1 / N2 는 본 PoC 의 전제를 무너뜨릴 수 있고, N3 는 2차 PoC 의 `GET /kubeconfig` 구현에 직접 걸린다.
> **1차 PoC 착수와 병행해 NVIDIA 에 질의할 것** — 답이 늦으면 2차가 헛일이 될 수 있다.

---

## 8. 범위 밖

- **Keycloak / OpenLDAP 연동** (그룹 자동 상속) — 3차
- **고가용성** (웹훅 다중화, 로드밸런싱) — 설계 단계
- **OIDC 병행** (방식 B) — 설계상 2안으로 유보
- **성능 부하 시험** — 캐시 TTL 결정 후 별도

---

## 9. 진행 순서

```
1일차   §3 안전장치 + W1 웹훅 서버 + A0 기준선
2일차   W2 apiserver 설정 + A1~A5 (핵심: 401 → 403 → 200)
3일차   A6~A10 + 롤백 검증
4~5일차 2차 B1~B7 (토큰 API 결합)
6일차   B3 캐시 TTL 측정 + B8 장애 시나리오 + 산출물 정리
```

**1차는 A3(403 수신)까지만 가도 목표 A1 달성.** 이후는 인가 설정과 부가 확인.
