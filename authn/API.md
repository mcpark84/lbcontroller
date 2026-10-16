# Managed K8s 토큰 API — 규격 (PoC r1)

> 구현: `authn/token_api.py` (python 표준 라이브러리, sqlite) / 검증: `authn/webhook_server.py` (apiserver TokenReview 웹훅)
> 기계 판독용 규격: `authn/openapi.yaml` (OpenAPI 3.0)
> 실측 근거: `test_result_webhook.md` (1차), `test_result_webhook_phase2.md` (2차)
> 작성: 2026-10-16

---

## 1. 개요

이 API 는 **클러스터 접근용 bearer 토큰**을 발급·조회·폐기하고, 토큰이 들어간 kubeconfig 를 내려준다. 토큰은 kube-apiserver 의 인증 웹훅(TokenReview)이 검증한다.

```
사용자 ── kubectl (Bearer pt_…) ──▶ kube-apiserver ── TokenReview(mTLS) ──▶ 웹훅 ── SHA-256 조회 ──▶ tokens.db
포탈/운영자 ── 이 API (발급/폐기) ─────────────────────────────────────────────────────────────▲
```

| 항목 | 값 |
|---|---|
| Base URL (PoC) | `http://127.0.0.1:8080` (mgmt01 loopback) |
| 인증 | **없음** (PoC). 호출자 식별은 `X-Actor` 헤더로 감사 로그에만 기록. 운영에서는 포탈 세션/서비스 계정 인증 필수 |
| Content-Type | 요청 `application/json`, 응답 `application/json` (kubeconfig 는 `application/yaml`) |
| 시각 | 모두 UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| 토큰 형식 | `pt_` + base64url(32바이트 난수), 46자. 서버는 **SHA-256 만 저장** |

## 2. 역할 (`role`)

역할은 발급 시 토큰에 고정된다. 웹훅이 그룹 `mk8s:<role>` 로 변환하고, 그룹에 사전 생성된 RBAC 이 걸린다. **역할 변경 = 새 토큰 발급 + 기존 토큰 폐기.**

| role | 그룹 | RBAC | 범위 |
|---|---|---|---|
| `skt-admin` | `mk8s:skt-admin` | ClusterRoleBinding → `cluster-admin` | 클러스터 전체 (컨트롤 플레인 포함) |
| `tenant-operator` | `mk8s:tenant-operator` | RoleBinding → `view` (고객 네임스페이스마다) | 고객 네임스페이스 **조회만**. Secret 제외 |
| `tenant-user` | `mk8s:tenant-user` | RoleBinding → `edit` (고객 네임스페이스마다) | 고객 네임스페이스 CRUD. RBAC 리소스 변경 불가 |

- 고객 네임스페이스 (PoC): `default`, `dynamo-system`, `inferium`. 매니페스트 `authn/rbac/roles/`.
- 테넌트 역할은 자기 네임스페이스를 `get` 할 수 있으나 네임스페이스 **목록**, 노드, 컨트롤 플레인 네임스페이스는 403.
- 웹훅은 `poc-tenant-<tenant>` 그룹도 함께 돌려준다 (감사 정책 매칭용). 이 그룹에는 바인딩이 없어야 한다.

## 3. 엔드포인트

### 3.1 `POST /tokens` — 운영자 발급

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `owner_id` | string | ✔ | 토큰 소유자. 클러스터에서 `username` 이 된다. 형식 `^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$` (아니면 400) |
| `tenant` | string | ✔ | 테넌트 ID. 그룹 `poc-tenant-<tenant>`. 형식 동일 |
| `role` | string | ✔ | §2 의 3개 중 하나 |
| `cluster` | string | | 대상 클러스터 ID. 기본값 서버의 `cluster_id`. 다른 값이면 이 클러스터에서 `cluster-mismatch` 로 거부 |
| `ttl_seconds` | int | | 만료까지 초. 기본 86400 |

```http
POST /tokens
X-Actor: dan.park
Content-Type: application/json

{"owner_id":"alice","tenant":"a","role":"tenant-user","ttl_seconds":3600}
```
```http
201 Created
{
 "id": "47456f18eecc",
 "owner_id": "alice", "tenant": "a", "cluster": "gasandevcontrol", "role": "tenant-user",
 "issued_via": "operator",
 "created_at": "2026-10-16T05:40:50Z", "expires_at": "2026-10-16T06:40:50Z",
 "revoked_at": null, "last_used_at": null,
 "hash_prefix": "a1b2c3d4e5f6",
 "token": "pt_…46자…"          ← 이 응답에서만 반환. 다시 조회 불가
}
```
오류: `400` 필수 필드 누락 / `role` 값 오류 (`{"error":"role must be one of [...]"}`)

### 3.2 `GET /tokens[?owner=]` — 목록

```http
GET /tokens?owner=alice
```
```http
200 OK
{"items": [ { "id": "...", "owner_id": "alice", "tenant": "a", "cluster": "...", "role": "tenant-user",
              "issued_via": "kubeconfig", "created_at": "...", "expires_at": "...",
              "revoked_at": null, "last_used_at": "2026-10-16T05:40:51Z", "hash_prefix": "a1b2c3d4e5f6" } ]}
```
- 평문과 전체 해시는 **절대 반환하지 않는다.** `hash_prefix`(12자)는 로그 상관용.
- 상태는 필드로 판단: `revoked_at != null` 폐기됨, `expires_at <= now` 만료됨, 둘 다 아니면 유효.
- 필터는 `owner` 만 (PoC). 페이지네이션 없음.

### 3.3 `GET /tokens/{id}` — 단건

`200` 위 항목 1개 / `404 {"error":"not found"}`

### 3.4 `DELETE /tokens/{id}` — 개별 폐기

```http
DELETE /tokens/47456f18eecc
X-Actor: dan.park
```
```http
200 OK
{ ..., "revoked_at": "2026-10-16T05:40:52Z" }
```
- row 는 삭제되지 않는다 (`revoked_at` 기록). 이미 폐기된 토큰에 다시 호출해도 `200` (멱등, `revoked_at` 유지).
- `404` 없는 id.

### 3.5 `DELETE /tokens?user={owner}` — 계정 단위 일괄 폐기

```http
DELETE /tokens?user=alice
```
```http
200 OK
{"owner_id": "alice", "revoked": 3}
```
소유자의 **미폐기** 토큰 전부에 `revoked_at` 기록. 다른 소유자 영향 없음.

### 3.6 `GET /clusters/{cluster_id}/kubeconfig` — 사용자 발급 경로

| 쿼리 | 필수 | 설명 |
|---|---|---|
| `owner` | ✔ | 소유자 |
| `tenant` | ✔ | 테넌트 |
| `role` | ✔ | 역할 |
| `ttl` | | 초. 기본 86400 |

```http
GET /clusters/gasandevcontrol/kubeconfig?owner=alice&tenant=a&role=tenant-user&ttl=3600
X-Actor: alice
```
```http
200 OK
Content-Type: application/yaml
X-Token-Id: 47456f18eecc
X-Token-Expires: 2026-10-16T06:40:50Z

apiVersion: v1
kind: Config
clusters:
  - name: gasandevcontrol
    cluster:
      certificate-authority-data: <클러스터 CA, base64>
      server: https://172.16.1.120:6443
users:
  - name: alice
    user:
      token: pt_…
contexts:
  - name: gasandevcontrol
    context: {cluster: gasandevcontrol, user: alice}
current-context: gasandevcontrol
```
- 토큰을 새로 만들고(`issued_via=kubeconfig`) endpoint / CA 와 조립한다. 평문은 이 파일에만 존재한다.
- YAML 스칼라는 모두 따옴표로 감싼다. `owner=3` 같은 값이 숫자로 파싱되어 kubectl 이 거부하는 것을 막기 위함(2026-10-16 수정).
- `{cluster_id}` 가 서버의 `cluster_id` 와 다르면 `404 {"error":"unknown cluster <id>"}`. (운영: 클러스터 레지스트리에서 endpoint / CA 조회 — NKE 질문 N3)
- `400` 필수 쿼리 누락 / role 오류.

### 3.7 `GET /healthz`

```json
{"status":"ok","db":"/home/ubuntu/poc-authn/tokens.db","tokens":24,
 "roles":["skt-admin","tenant-operator","tenant-user"],
 "cluster_id":"gasandevcontrol","server":"https://172.16.1.120:6443"}
```

## 4. 토큰 수명주기와 검증 규칙

웹훅은 TokenReview 를 받을 때마다 아래 순서로 판정한다 (`tokenstore.verify`).

| 순서 | 조건 | 결과 (`status.error` / 웹훅 로그 reason) |
|---|---|---|
| 1 | 해시가 DB 에 없음 | `unknown` |
| 2 | `revoked_at` 있음 | `revoked` |
| 3 | `expires_at <= now` | `expired` |
| 4 | `cluster` ≠ 웹훅의 `--cluster-id` | `cluster-mismatch` |
| 5 | 통과 | `last_used_at` 갱신, `username=owner_id`, `groups=[mk8s:<role>, poc-tenant-<tenant>]` |

**폐기·만료 반영 시간 (실측, 2차 §4·§8).** apiserver 가 성공 결과를 캐시하므로 폐기 즉시 401 이 되지 않는다.

| apiserver `--authentication-token-webhook-cache-ttl` | 폐기 → 401 |
|---|---|
| `0s` | 최대 10초 (전역 성공 캐시) — 실측 5~7초 |
| `2m` (기본) | **마지막 사용 시각 + 120초 + 10초** — 실측 63~123초 |

이미 열린 `watch` / `logs -f` / `exec` 는 폐기해도 끊기지 않는다. 즉시 차단이 필요하면 RBAC 바인딩 제거.

**실패 캐시 (2차 발견 F6).** 웹훅이 `status.error` 를 채우면 apiserver 는 실패를 캐시하지 않고 매 요청 웹훅을 호출한다 (폐기 토큰 반복 시 1:1). 비우면 TTL 동안 캐시된다. 운영 권장: 확정 거부는 `status.error` 생략(`--omit-status-error`), 사유는 웹훅 로그에만.

## 5. 감사 로그

| 로그 | 위치 (PoC) | 내용 |
|---|---|---|
| 발급 / 폐기 | `~/poc-authn/token_audit.jsonl` | `{"ts","actor","action":"issue"\|"revoke"\|"revoke-user","token_id","owner_id","tenant","role","cluster","issued_via","expires_at","hash_prefix"}` |
| 인증 성공 / 실패 | `~/poc-authn/webhook.log` | `addr peer_cn hash=<12자> ok\|deny reason=<사유> user groups ms`. **401 의 1차 기록** (apiserver 감사에는 안 남음) |
| API 호출 / 인가 | apiserver `/var/log/kubernetes/audit/audit.log` | `user.username`, `user.groups`, verb, 리소스, 응답 코드, `authorization.k8s.io/reason`(허용한 바인딩 이름) |

연결 키: `hash_prefix`(발급·폐기 ↔ 인증) 와 `owner_id`(인증 ↔ API 호출). 어느 로그에도 평문은 없다 (20개 토큰 × 7개 로그 전수 확인).

## 6. 오류 코드

| 코드 | 의미 |
|---|---|
| 400 | 필수 필드 누락, JSON 파싱 실패, `role` 값 오류 |
| 404 | 없는 `id`, 모르는 `cluster_id`, 없는 경로 |
| 201 / 200 | 발급 / 그 외 성공 |

## 7. 보안·운영 메모

- 평문은 발급 응답과 kubeconfig 에만 존재. 서버는 SHA-256 만 저장 (근거: 결과서 2차 부록 B). DB 파일 권한 600.
- 발급면(이 API)과 인증면(웹훅)은 DB 파일만 공유하고 통신하지 않는다. API 가 죽어도 인증은 계속된다 (2차 §10).
- apiserver ↔ 웹훅은 mTLS. 웹훅은 apiserver 클라이언트 인증서(CN=kube-apiserver)만 받는다 (2차 §8·§9).
- NVIDIA AI Clouds v2.4 대조: SEC03(만료·소유자·미사용 탐지) 충족, 로테이션 미구현. SEC20 저장 암호화, SEC08 보존·export 는 운영 과제 (2차 부록 A).

## 8. 미구현 / 다음 후보

| 항목 | 비고 |
|---|---|
| 토큰 로테이션 (`POST /tokens/{id}/rotate`: 새 토큰 발급 + 구 토큰 유예 폐기) | SEC03 |
| API 호출자 인증·인가 (누가 누구 토큰을 폐기할 수 있나) | 포탈 연동 시 |
| 조회 필터 (tenant, role, status), 페이지네이션 | |
| 미사용 토큰 리포트, 만료 임박 알림 | `last_used_at`, `expires_at` 로 가능 |
| 평문으로 자기 토큰 폐기 (`DELETE /tokens/self`) | 사용자가 `id` 를 모를 때 |
| 그룹 매핑 외부화 (IdP) | 3차 |

## 9. 빠른 예시 (curl)

```bash
API=http://127.0.0.1:8080; H='-H X-Actor:dan.park -H Content-Type:application/json'
# 발급 (운영자)
curl -s $H -X POST $API/tokens -d '{"owner_id":"alice","tenant":"a","role":"tenant-user","ttl_seconds":3600}'
# kubeconfig 발급
curl -s $H -o alice.kubeconfig -D - "$API/clusters/gasandevcontrol/kubeconfig?owner=alice&tenant=a&role=tenant-user"
kubectl --kubeconfig alice.kubeconfig get pods -n default
# 조회 / 폐기
curl -s $H "$API/tokens?owner=alice"
curl -s $H -X DELETE $API/tokens/<id>
curl -s $H -X DELETE "$API/tokens?user=alice"
```
스크립트 래퍼: `authn/scripts/w4-tok.sh issue|list|get|revoke|revoke-user|kubeconfig`.
