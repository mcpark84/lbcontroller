# 인증 웹훅 PoC 2차 결과 — 토큰 발급 / 해시 저장 / 폐기 / 캐시 / mTLS / 장애

> 대상: gasandevcontrol (mgmt01 172.16.1.120 컨트롤 플레인 단일 + dev001, K8s v1.35.1)
> 실행 계획: `PoC_auth_webhook_2차_실행계획.md` / 1차 결과: `test_result_webhook.md`
> 시작: 2026-10-14. 시각은 모두 UTC (KST = +9h).

---

## 배경 — 2차가 확인하려는 것

1차는 "apiserver 가 모르는 bearer 토큰을 웹훅으로 넘겨 인증 결과를 받는다" 는 **표준 경로가 이 클러스터에서 동작한다** 는 것까지 확인했다 (401 → 403 → 200). 웹훅은 토큰 내용을 보지 않는 allow-all 이었다.

2차는 그 웹훅 뒤에 **실제 토큰 저장소**를 붙인다. 표준 K8s 동작은 바뀌지 않는다. apiserver 는 여전히 TokenReview 를 보내고 `authenticated` 와 `user` 만 읽는다. 바뀌는 것은 웹훅이 `true` 를 돌려주는 조건뿐이다.

| 구성 요소 | 역할 | 2차 구현 |
|---|---|---|
| 토큰 API (`token_api.py`, 127.0.0.1:8080) | 포탈 쪽. 발급 / 조회 / 폐기 / kubeconfig 조립 | sqlite, 표준 라이브러리 |
| 토큰 저장소 (`tokens.db`) | 토큰의 **SHA-256 해시**와 메타데이터. 평문 없음 | 두 프로세스가 WAL 모드로 공유 |
| 웹훅 (`webhook_server.py`, 127.0.0.1:8443) | apiserver 의 TokenReview 를 받아 해시 조회 | `--db` 로 DB 모드, `--client-ca` 로 mTLS |

**왜 해시만 저장해도 되는가.** 토큰은 256비트 난수라 사람이 정한 비밀번호와 달리 사전 공격 대상이 아니다. NIST SP 800-63B 는 엔트로피 112비트 이상의 look-up secret 은 승인된 단방향 해시로 저장하면 충분하다고 정한다. salt 나 bcrypt 류는 불필요하며, 매 인증마다 돌아가는 웹훅에서는 지연만 늘린다. SOC 2 는 알고리즘을 지정하지 않는다. DB 가 유출돼도 토큰을 복원할 수 없으므로, CA 방식의 서명 키처럼 HSM/Vault 급 보관이 요구되지 않는다.

**폐기는 삭제가 아니다.** row 를 지우면 "누가 언제 폐기했나", "폐기 후 누가 계속 시도했나" 를 추적할 수 없다. `revoked_at` 을 기록하고 조회 시 거부한다.

---

## 0. P0 — 사전 확인 (2026-10-14 08:48Z, 읽기 전용)

| 항목 | 확인값 |
|---|---|
| 웹훅 | pid 333469 (10-13 06:41 기동) 생존, `mode: allow-all`, `127.0.0.1:8443` |
| apiserver | 1차 플래그 8개 그대로, 컨테이너 10-13 06:03 기동 후 재시작 없음 |
| 1차 토큰 kubeconfig | `get nodes` 200 (감사 로그 39번째 행) |
| admin / 노드 | 정상, dev001·mgmt01 Ready |
| 8080 포트 | 비어 있음. `sqlite3` CLI, python sqlite 3.45.1 |
| RBAC | `poc-authn-node-reader` → `Group/poc-tenant-a` 유지 |

판정: **합격**. 1차 종료 상태와 동일.

## 1. P1 — 코드 작성과 로컬 자가 시험 (commit c6fcca7)

### 배경
클러스터에 닿기 전에 검증 로직의 네 가지 거부 사유(unknown / revoked / expired / cluster-mismatch)와 평문 미저장을 **로컬에서 먼저** 증명한다. 웹훅 코드는 1차 allow-all 을 그대로 두고 `--db` 가 있을 때만 DB 모드로 들어가므로, 1차 결과를 언제든 재현할 수 있다.

### 로컬 결과 (macOS, 가짜 admin kubeconfig, 포트 18080/18443)

| 시험 | 결과 |
|---|---|
| 발급 → DB | `sha256sum <token>` = DB `hash` (`f732905f…`). DB 파일 안에 `pt_` 0건 |
| 검증 | 유효 → `user=alice groups=[poc-tenant-a]`. unknown / revoked / expired / cluster-mismatch 각각 `authenticated: false` + 사유 |
| 일괄 폐기 | bob 토큰 2개 `DELETE /tokens?user=bob` → `revoked: 2`, 둘 다 거부 |
| 평문 누출 | API 로그, 웹훅 로그, `token_audit.jsonl`, stderr 모두 0건 |
| kubeconfig 응답 | `kubectl config view` 가 server / user / current-context 정상 파싱 |
| mTLS | 올바른 인증서 200 + `peer_cn=kube-apiserver`. 인증서 없음 `peer did not return a certificate`, 다른 CA `certificate verify failed` 로 handshake 거부, 서버 로그 1행 |
| `w2 --set-flag` | diff 가 `cache-ttl=0s → 2m` 한 줄, PyYAML 재파싱 통과. 없는 플래그는 거부 |

수정 2건: 로그 시각을 UTC 로 통일(세 로그 상관용), TLS handshake 실패를 handler 스레드에서 처리해 한 줄 로그로 남김(기본 `socketserver` 는 accept 단계 SSL 오류를 조용히 버린다).

판정: **합격**.

## 2. P2 — 토큰 API 기동, 웹훅 DB 모드 전환 (2026-10-14 09:20Z) ✋ 승인 후

### 배경
웹훅 프로세스만 바꾸므로 apiserver 재기동이 없다. 재기동 사이 1~2초는 토큰 사용자만 401 이고(1차 S10 과 같은 경로), 인증서 사용자와 Pod 는 영향 없다. 1차 토큰 `poc-token-0001` 은 DB 에 없으므로 이 시점부터 **영구히 401** 이 된다. "웹훅이 모르는 토큰 = 401" 이라는 1차 S3 기준선으로 돌아가는 것이다.

### 절차와 로그
```
09:20:31Z  w4-run-token-api.sh start
           {"status":"ok","db":"/home/ubuntu/poc-authn/tokens.db","tokens":0,
            "cluster_id":"gasandevcontrol","server":"https://172.16.1.120:6443"}
09:20:32Z  w1-run-webhook.sh stop            -> stopped pid 333469
09:20:33Z  WEBHOOK_DB=~/poc-authn/tokens.db w1-run-webhook.sh start -> pid 3329490
           {"status":"ok","mode":"db","mtls":"off","cluster_id":"gasandevcontrol"}
09:20:34Z  kubectl --kubeconfig token.kubeconfig get nodes
           error: You must be logged in to the server (Unauthorized)
           webhook.log: hash=a4bb8ac443a8 len=14 deny reason=unknown
           kubectl get nodes (admin)  -> 2 nodes
```
토큰 API 는 endpoint / CA 를 admin kubeconfig 에서 읽어 왔다 (`server=https://172.16.1.120:6443`). 이것이 NKE 질문 N3 에서 API 로 받아야 하는 두 값이다.

### 판정
**합격.** `mode: db`, 1차 토큰 401 + 웹훅 로그 `deny reason=unknown`, 인증서 경로 정상. 웹훅 로그의 토큰 표기가 1차의 평문 앞 8자에서 **해시 앞 12자**로 바뀐 것도 확인.

### 설계 시사점
- 웹훅 교체는 apiserver 입장에서 투명하다. 검증 로직 배포 / 롤백은 웹훅 프로세스 수준에서 끝난다.
- 토큰 DB 파일은 생성 시 644 로 만들어졌다 → `open_db` 에서 600 으로 조이도록 수정 (해시뿐이지만 소유자·테넌트 메타데이터가 있다).

## 3. P3 — 발급 → 해시 저장 → 사용 (E1~E5), B1 / B2 / B7 (2026-10-14 09:22Z)

### 배경
`GET /clusters/{id}/kubeconfig` 는 토큰을 만들고 클러스터 endpoint / CA 와 함께 kubeconfig 로 조립해 준다. 사용자는 이 파일만 받는다. 표준 kubectl 은 `users[].user.token` 을 `Authorization: Bearer` 로 보내고, apiserver 는 1차와 똑같이 웹훅에 TokenReview 를 보낸다. 바뀐 것은 웹훅이 이제 해시를 조회한다는 점뿐이다.

B7(소유자 필터)은 K21 "계정 비활성화 시 일괄 취소" 의 전제다. 토큰이 소유자 단위로 조회되지 않으면 일괄 취소도 할 수 없다.

### 절차와 로그
```
09:22:05Z  w4-tok.sh kubeconfig alice a 3600      -> alice.kubeconfig, X-Token-Id: 57e7e059525f
           token: pt_fC…(46자)  server=https://172.16.1.120:6443  current-context=gasandevcontrol
E2         sha256sum <token> = c074428386a6cc2e…5cc5c
           DB hash            = c074428386a6cc2e…5cc5c   (일치)
           DB rows LIKE 'pt_%' = 0,  db+wal 파일 내 'pt_' 바이트열 = 0
E3         GET /tokens?owner=alice -> hash_prefix "c074428386a6" 만. token / hash 필드 없음
09:22:05Z  E4 kubectl --kubeconfig alice.kubeconfig get nodes -> 200 (dev001, mgmt01)
           webhook.log: hash=c074428386a6 ok user=alice groups=['poc-tenant-a'] ms=6.28
           DB last_used_at = 09:22:05Z
09:22:05Z  E5 같은 명령 3회 연속 -> 200 ×3,  웹훅 호출 수 before=2 after=2  (호출 없음)
09:22:05Z  bob (tenant b) 발급 -> get nodes -> 403  User "bob" cannot list resource "nodes"
           webhook.log: hash=93d4f69d0659 ok user=bob groups=['poc-tenant-b']
B7         GET /tokens?owner=alice -> alice 1건 / ?owner=bob -> bob 1건 / 전체 2건
감사       apiserver audit.log: alice list nodes 200 ×4 "RBAC: allowed by ClusterRoleBinding poc-authn-node-reader"
                                bob   list nodes 403 Forbidden
           token_audit.jsonl: issue alice 57e7…, issue bob 6feb… (actor=dan.park)
평문 검색   webhook.log / token_api.log / token_audit.jsonl / stderr / apiserver audit.log 모두 0건
```

### 판정
| 항목 | 결과 |
|---|---|
| B1 발급 → 사용 | **합격** — API 가 준 kubeconfig 로 바로 200 |
| B2 평문 미저장 | **합격** — DB 에 SHA-256 만, 재조회 시 접두 12자만, 로그 5종 평문 0건 |
| B7 소유자 필터 | **합격** |
| E5 캐시 | 10초 내 재요청은 웹훅 미호출 (1차 F1 재확인) |
| bob 403 | 인증은 됐으나(`User "bob"`) tenant b 그룹에 바인딩 없음 → **인증과 인가가 분리**되어 있음을 2차에서도 재확인 |

### 설계 시사점
- 테넌트 → 그룹 매핑(`poc-tenant-<tenant>`)만으로 RBAC 이 테넌트 단위로 갈린다. 3차 IdP 연동은 이 그룹 목록을 외부에서 받아오는 것으로 축소된다.
- 감사 로그의 `user.username` 이 `owner_id` 로 바로 찍히므로, 포탈 계정 ↔ 클러스터 행위를 별도 매핑표 없이 연결할 수 있다 (K51).

## 4. P4 — 폐기 → 인증 실패 (E6~E10), B3 TTL 0s 측정 (2026-10-14 09:22~09:25Z)

### 배경
apiserver 의 토큰 캐시는 두 겹이다. ① 웹훅 인증기 캐시 `--authentication-token-webhook-cache-ttl` (현재 0s) ② 전역 토큰 **성공** 캐시 약 10초 (플래그 없음, 1차 발견 F1). 그래서 TTL 0s 라도 폐기가 반영되기까지 최대 10초가 걸리고, 반대로 **실패는 전역 캐시에 들어가지 않아** 무효 토큰은 매 요청 웹훅에 간다. 이 비대칭을 숫자로 확인하는 것이 P4 의 목적이다.

장기 연결(watch / logs -f / exec)은 연결 수립 시 한 번 인증되고 이후 재인증하지 않는 것이 K8s 표준 동작이다. 폐기해도 이미 열린 스트림은 끊기지 않는다.

### 절차와 로그
```
09:22:50Z  M5  kubectl get nodes -w (alice 57e7…) 백그라운드 기동 -> 2 노드 출력, 연결 유지
09:22:52Z  M1-1 w5-measure-revoke.sh 57e7e059525f  (1초 폴링, 3초 프라이밍)
09:22:55.757Z   DELETE /tokens/57e7e059525f  -> revoked_at 09:22:55Z
09:22:55.8 ~ 09:23:00.0Z   200 ×5
09:23:01.075Z   401                              -> 폐기 후 5.3초
09:23:12Z  E8  +11초 후 3회 -> 401 ×3,  webhook.log deny reason=revoked ×3 (호출 수 3→6)
           DB last_used_at = 09:22:50Z (watch 수립 시각) 그대로 — 폐기 후 갱신 없음
09:23:12Z  E9  토큰 1글자 변경 -> 401, webhook.log hash=172451a75849 deny reason=unknown
09:23:12Z  M5  watch 프로세스 생존 (폐기 후 17초), 09:23:32Z 생존 (폐기 후 37초) -> 수동 종료. 출력 11행
09:23:32Z  M1-2 alice2 294ab6… 발급 -> 폐기 09:23:35.476Z -> 401 09:23:42.902Z  = 7.4초
09:23:42Z  M1-3 alice3 a25b3a… 발급 -> 폐기 09:23:46.094Z -> 401 09:23:53.522Z  = 7.4초
09:23:53Z  M3  유효 토큰(alice4 656c…) 30초 연속 kubectl
             직렬 1   : kubectl 653건 (21.8 req/s)  웹훅 3건 (0.10 call/s)
             병렬 5   : kubectl 2860건 (95.3 req/s) 웹훅 3건 (0.10 call/s)
09:24:53Z  M4  폐기 토큰(alice 57e7…) 30초 연속 kubectl
             직렬 1   : kubectl 692건 (23.1 req/s, 성공 0)  웹훅 692건 (23.07 call/s)  = 1:1
09:25:23Z  E10 해시 c074428386a6 로 세 로그 연결
             token_audit.jsonl : issue 09:22:05Z (dan.park) / revoke 09:22:55Z (w5)
             webhook.log       : ok 2건 (09:22:05 E4, 09:22:50 watch) / deny 696건 (E7 1 + E8 3 + M4 692)
             apiserver audit   : username=alice 3548건 전부 200 (401 은 user 가 비어 정책상 미기록)
```

### 판정
| 측정 | 결과 | 판정 |
|---|---|---|
| M1 폐기 → 첫 401 (TTL 0s) | **5.3 / 7.4 / 7.4초** | ≤ 10초. **합격**. 지연은 전적으로 전역 10초 캐시 |
| E8 폐기 후 재시도 | 매 요청 웹훅 도달, `revoked` 거부, `last_used_at` 불변 | **합격** |
| E9 미존재 토큰 | `unknown` 거부 | **합격** |
| M3 유효 토큰 호출률 | 95 req/s 에도 웹훅 **0.1 call/s** (토큰당 10초에 1회) | TTL 0s 가 "매 요청 호출" 이 아님을 실증 |
| M4 폐기 토큰 호출률 | 요청 수 = 웹훅 호출 수 (**23 call/s**) | ★ 실패는 캐시되지 않음 |
| M5 장기 연결 | 폐기 후 37초 이상 watch 유지 | 표준 동작. 런북에 "폐기는 신규 요청만 차단" 명시 |

### 설계 시사점 ★
- **TTL 0s 의 실제 비용은 유효 토큰이 아니라 무효 토큰이다.** 유효 토큰은 전역 캐시 덕에 10초에 1회지만, 폐기·만료·위조 토큰을 반복 제시하면 요청 수 그대로 웹훅에 꽂힌다. 웹훅 쪽 rate-limit 또는 TTL > 0 (실패도 캐시) 중 하나는 필요하다. P8 의 TTL 2m 측정(M4 재측정)이 이 결정의 나머지 반쪽이다.
- 폐기 즉시성의 하한 10초는 플래그로 줄일 수 없다. 운영 문서에는 "폐기 후 최대 10초 + 웹훅 TTL" 로 쓴다.
- 장기 연결은 폐기로 끊기지 않으므로, 즉시 차단이 필요하면 **RBAC 바인딩 제거**(인가는 매 요청 평가) 또는 apiserver 재기동이 수단이다.

## 5. P5 — B4 만료 (2026-10-14 09:26Z)

### 배경
만료는 폐기와 달리 운영자 행위 없이 시각만으로 거부된다. 검증 순서상 `revoked_at` 다음에 `expires_at` 을 본다. 전역 10초 캐시 때문에 만료 시각 이후 최대 10초까지 통과할 수 있다.

### 절차와 로그
```
09:26:07Z  alice ttl=30 발급 (726f03584e6b), expires_at = 09:26:37Z
09:26:07Z  즉시 200 (웹훅 ok)
09:26:27Z  t+20s 200 (웹훅 ok — 캐시 갱신)
09:26:27 ~ 09:26:36Z  1초 폴링 200 ×10
09:26:37.769Z  401,  webhook.log deny reason=expired
```
### 판정
**합격.** 만료 시각 +0.8초에 401. 이번에는 직전 캐시 갱신(09:26:27)이 만료 시각에 정확히 10초 앞서 있어 지연이 없었지만, 일반적으로는 최대 10초 늦을 수 있다.

## 6. P6 — B5 클러스터 바인딩 (2026-10-14 09:26Z)

### 배경
웹훅 하나가 여러 클러스터를 받을 때, A 클러스터용 토큰이 B 클러스터에서 통하면 안 된다. apiserver 가 보내는 TokenReview 의 `spec.audiences` 는 클러스터 구분에 쓸 수 없다 (이 클러스터는 `--api-audiences` 미설정이라 암묵 audience 하나뿐이고, 다른 클러스터도 대개 같은 값이다). 그래서 토큰 row 의 `cluster` 와 웹훅이 아는 자기 클러스터 ID(`--cluster-id`) 를 비교한다. 단일 클러스터 PoC 라 "다른 클러스터용으로 발급한 토큰을 이 클러스터에 제시" 로 모사했다. 운영에서는 P8 의 mTLS 클라이언트 인증서 CN 이 호출 클러스터 ID 가 된다.

### 절차와 로그
```
09:26:37Z  POST /tokens {owner_id: eve, tenant: a, cluster: other-cluster}  -> 30ed3ebb7968
           w3-make-token-kubeconfig.sh <token> eve.kubeconfig
09:26:37Z  kubectl --kubeconfig eve.kubeconfig get nodes -> 401
           webhook.log hash=0bb52f909be1 deny reason=cluster-mismatch
```
### 판정
**합격.** 토큰은 유효(미폐기·미만료)하지만 클러스터가 달라 거부.

## 7. P7 — B6 계정 단위 일괄 폐기 (2026-10-14 09:26Z)

### 배경
K21 "계정 비활성화 시 해당 계정의 모든 토큰 무효". `DELETE /tokens?user=` 가 소유자의 미폐기 토큰 전부에 `revoked_at` 을 찍는다. 다른 소유자는 영향이 없어야 하며, 그 증거로 bob 이 **401 이 아니라 403** 을 유지하는지 본다 (403 = 인증은 계속 성립).

### 절차와 로그
```
09:26:37Z  alice5 발급 (93c85936a575). alice 미폐기 토큰 3개 (alice4, ttl30(만료됨), alice5)
09:26:37Z  alice4 200  alice5 200  bob 403
09:26:38Z  DELETE /tokens?user=alice -> {"owner_id":"alice","revoked":3}
09:26:49Z  +11s: alice4 401  alice5 401  bob 403
           webhook.log: e22a463c102b deny revoked / 24c4eb6c7fe2 deny revoked / 93d4f69d0659 ok (bob)
           token_audit.jsonl: {"action":"revoke-user","owner_id":"alice","revoked":3}
```
### 판정
**합격.** alice 전량 401, bob 은 403 유지.

---

### 중간 상태 (P7 종료, 2026-10-14 09:27Z)
| 항목 | 상태 |
|---|---|
| apiserver | 1차 플래그 그대로 (`cache-ttl=0s`), 재기동 없음 |
| 웹훅 | pid 3329490, DB 모드, mTLS off |
| 토큰 API | pid 3329325, 127.0.0.1:8080 |
| tokens.db | 7건 (alice 6건 전부 폐기, bob 1건 유효, eve 1건 other-cluster) |
| 다음 | P8 ✋✋ apiserver 재기동 1회 (`cache-ttl 2m` + mTLS 클라이언트 인증서) |

---

## 부록 A. NVIDIA "Requirements for AI Clouds" v2.4 보안 요건 대조 (2026-10-16 확인)

출처: https://docs.nvidia.com/dsx/ncp/nvidia-requirements-for-ai-clouds/v2.4 — 요구사항 ID 단위(SEC01~27, K8S01~32 등). 표기 규칙: **shall / must = 필수**, should = 권고.

**문서에 등장하지 않는 용어**: hash, SHA, FIPS, HSM, KMS, vault, mTLS, TLS 버전, cipher, webhook. 즉 해시 알고리즘·비밀 보관 제품·TLS 세부는 지정하지 않는다.

### A.1 세 질문에 대한 답

| 질문 | 답 | 근거 조항 |
|---|---|---|
| 토큰 해시를 Vault 등 없이 저장해도 되는가 | **된다.** 단 저장 매체 암호화와 장기 자격증명 관리 요건은 적용 | SEC20 (필수) 모든 저장 데이터 암호화. SEC03 (필수) 장기 자격증명은 만료·로테이션 설정, 소유자 귀속, 미사용 탐지. SEC09 의 키 보관 요건은 "암호 키와 인증서" 대상이라 단방향 해시에는 해당 없음 |
| SHA-256 / SHA-512 지정이 있는가 | **없다.** SHA-256 유지 (부록 B) | 해당 용어 부재 |
| apiserver ↔ 웹훅 인증서 요건 | **상호 인증(mTLS) 과 인증서 수명주기 관리가 요건** | SEC13 모든 east-west / north-south 트래픽에 암호화 + 상호 인증. SEC09 (필수) 인증서 발급·배포·저장·자동 로테이션(간격 설정)·폐기, 만료일 필수, 감사 가능. SEC10 관리형 키·인증서 사용. SEC14 API 엔드포인트 비공개 |

### A.2 설계 반영표

| 조항 | 원문 요지 | 현재 PoC | 운영 설계에 필요한 것 |
|---|---|---|---|
| SEC03 | 장기 자격증명: configurable expiration and rotation, ownership attribution, unused account detection | `expires_at`, `owner_id`, `last_used_at` 구현. B4 / B7 / E10 으로 실증 | **로테이션(재발급) 절차** 추가. 미사용 토큰 리포트 |
| SEC20 | 모든 데이터 at-rest 암호화 | sqlite 파일, 미암호화 디스크 | 토큰 DB 를 암호화 볼륨/DB 에 배치 |
| SEC13 | 상호 인증 | 1차 단방향 TLS → **P8 에서 mTLS 적용** | 클러스터별 클라이언트 인증서, CN = 클러스터 ID |
| SEC09 / SEC10 | 인증서 수명주기, 관리형 PKI | 자체서명 365일 | 플랫폼 PKI(cert-manager / 내부 CA)에서 발급·로테이션. apiserver 는 webhook-kubeconfig 를 **기동 시에만 읽으므로** 클라이언트 인증서 로테이션 = apiserver 재기동(1차 실측 약 40초). 클라이언트 인증서는 길게, 웹훅 서버 인증서는 짧게 나눠 운용 |
| SEC14 | API 엔드포인트 방화벽 / private link | 웹훅 127.0.0.1 바인딩 | 웹훅 엔드포인트 비공개 + 네트워크 제한 + mTLS 병행 |
| SEC08 | 인증 이벤트·인가 결정 감사 로그 30일 보존, export(S3 등), actor / timestamp / source IP / action / result | token_audit.jsonl + webhook.log + apiserver audit (PoC 신원 한정 정책) | 감사 정책을 전체 신원으로 확장, 보존·export 파이프라인. 401 은 user 가 비어 apiserver 감사에 안 남으므로 **웹훅 로그가 인증 실패의 1차 기록** |
| SEC01 / SEC26 | 사용자 인증은 IdP 연합 SSO (OIDC / SAML 2.0). 관리자가 SSO 강제, 비연합 로그인 제한 가능 | 범위 밖 | ★ 웹훅 bearer 토큰은 **사람 로그인의 대체가 아니라 SEC03 의 장기 자격증명(API 키 / 자동화용)** 으로 자리매김. 사람용은 계획서 2안 OIDC 인증기(방식 B)가 SEC01 에 직접 대응 → 운영은 "OIDC(사람) + 웹훅 토큰(서비스 계정)" 병행 |
| SEC19 | SOC 2 Type 1 이상 (Security / Availability / Confidentiality) | — | 알고리즘이 아니라 정책 문서화·구현 일치·변경 이력이 심사 대상 |
| K8S17 | 표준 SA + projected token, 클러스터별 OIDC issuer / JWKS 노출 | 1차 A7 에서 SA 경로가 웹훅과 무관함을 확인 | 웹훅 도입이 SA 토큰 체계를 건드리지 않음 (체인 순서상 SA 가 먼저) |

## 부록 B. 해시 알고리즘 결정서 — SHA-256 유지 (2026-10-16 결정)

**결정**: 토큰 저장 해시는 `SHA-256(plaintext)` 단독. salt·pepper·느린 KDF 없음.

**근거**
1. **요건 부재**: NVIDIA v2.4, SOC 2 TSC, ISO 27001 A.8.24, PCI DSS 모두 알고리즘을 지정하지 않는다. FIPS 180-4 / NIST SP 800-131A 는 SHA-2 계열 전부를 기한 없이 승인한다. NSA CNSA 2.0 의 SHA-384 이상 권고는 국가안보 시스템 한정이다.
2. **입력 엔트로피**: 토큰은 256비트 난수(`secrets.token_bytes(32)`). NIST SP 800-63B §5.1.2.2 는 엔트로피 112비트 이상의 look-up secret 을 승인된 단방향 해시로 저장하면 충분하다고 정하고, salt·느린 KDF 는 엔트로피가 낮은(사람이 정한) 비밀에만 요구한다. 비밀번호에 SHA-256 을 쓰지 말라는 조언은 알고리즘이 아니라 **빠른 해시 + 낮은 엔트로피** 조합에 대한 것이다.
3. **필요한 성질은 원상 저항**: DB 유출 시 해시에서 토큰을 되돌릴 수 없으면 된다. SHA-256 의 원상 공격은 알려진 것이 없고(2017 SHAttered 는 SHA-1 충돌), 양자 Grover 를 가정해도 유효 강도 128비트로 실용적 공격 범위 밖이다. 입력이 256비트 난수라 공격자는 해시가 아니라 토큰 공간을 뒤져야 한다.
4. **운영 비용**: 느린 KDF 는 매 인증(웹훅 호출)마다 수십 ms 를 더해 P4 의 23 call/s 같은 상황에서 웹훅 CPU 를 소모한다. SHA-256 은 로컬 측정 0.1 ms 미만.

**변경 조건**: 고객·보안팀이 CNSA 2.0 을 근거로 SHA-384/512 를 요구하면 `tokenstore.hash_token` 한 줄 변경으로 전환 가능하며, 기존 토큰은 재발급이 필요하다(해시 비교 불가). 그 외 기술적 필요는 없다.
