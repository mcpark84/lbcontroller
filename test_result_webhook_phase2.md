# 인증 웹훅 PoC 2차 결과 — 토큰 발급 / 해시 저장 / 폐기 / 캐시 / mTLS / 장애

> 대상: gasandevcontrol (mgmt01 172.16.1.120 컨트롤 플레인 단일 + dev001, K8s v1.35.1)
> 실행 계획: `PoC_auth_webhook_2차_실행계획.md` / 1차 결과: `test_result_webhook.md`
> 시작: 2026-10-14. 시각은 모두 UTC (KST = +9h).

---

## 요약 — P0~P11 결과 (2026-10-14 ~ 10-16)

| 단계 | 항목 | 핵심 결과 | 판정 |
|---|---|---|---|
| P0 | 사전 확인 | 1차 종료 상태 그대로 (웹훅 allow-all, apiserver 플래그 8개, 토큰 200) | 합격 |
| P1 | 코드 + 로컬 자가 시험 | 토큰 API / 해시 저장소 / 웹훅 DB 모드 / mTLS. 거부 사유 4종, 평문 0건 | 합격 |
| P2 | 웹훅 DB 모드 전환 | 웹훅 재기동만으로 전환, 1차 토큰은 `unknown` 으로 401 | 합격 |
| P3 | B1 B2 B7 발급 → 사용 | `sha256sum` = DB hash, 평문 0건, alice 200 / bob 403 (인증 OK·인가 없음), 소유자 필터 | 합격 |
| P4 | B3 폐기 (TTL 0s) | 폐기 → 401 **5.3 / 7.4 / 7.4초**. 유효 토큰 웹훅 0.1 call/s, 폐기 토큰 **23 call/s (1:1)**. watch 는 유지 | 합격 |
| P5 | B4 만료 | 만료 +0.8초에 401 (`expired`) | 합격 |
| P6 | B5 클러스터 바인딩 | 다른 cluster 로 발급한 토큰 401 (`cluster-mismatch`) | 합격 |
| P7 | B6 일괄 폐기 | alice 3건 전부 401, bob 403 유지 | 합격 |
| P8 | 재기동: TTL 2m + mTLS | 다운타임 **40.7초**. `peer_cn=kube-apiserver`, required 전환 OK. 폐기 → 401 **123 / 63 / 123초 = 마지막 호출 + 126초**. ★ F6 `status.error` 있으면 실패 미캐시(697 call) / 없으면 캐시(1 call) | 합격 |
| P9 | mTLS 부정 시험 | 다른 CA 신뢰 시 진짜 apiserver 도 거부 (`tls: unknown certificate authority`). 웹훅 재기동만으로 복원 | 합격 |
| P10 | B8 웹훅 hang / API 장애 | hang: **30.0초 후 401**, admin·SA·캐시 토큰 정상. 토큰 API 다운 시 인증 계속 | 합격 |
| P11 | B9 감사 추적 | 토큰 20개 × 로그 7종 평문 0건. 세 로그가 해시 접두 + username 으로 연결 | 합격 |
| §12 | 역할 기반 RBAC C1~C6 | skt-admin 전체 / tenant-operator 조회만 / tenant-user CRUD, 컨트롤 플레인 403, 권한 상승 403, 감사에 바인딩 이름 기록. 1차 `poc-authn-node-reader` 잔존으로 테넌트 `get nodes` 가 열렸던 것은 바인딩 삭제로 해소 | 합격 |
| §13 | API 문서 + CLI | `authn/API.md`, `authn/openapi.yaml`, `mk8s-token` 7개 명령. 실제 클러스터에서 발급 → 사용 → 폐기 → 감사 한 바퀴 | 합격 |
| P12 | A10 롤백 | **보류** | — |

**2차 발견 요약**: F6 `status.error` 유무가 실패 캐시를 결정 (§8) / 폐기 즉시성 = "마지막 사용 + TTL + 10초" (§8) / hang 은 down 과 달리 30초 지연 (§10) / 401 의 1차 기록은 웹훅 로그 (§11). NVIDIA v2.4 요건 대조는 부록 A, SHA-256 결정은 부록 B.

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

## 8. P8 — apiserver 재기동 1회: 캐시 TTL 2m + mTLS 클라이언트 인증서 (2026-10-16 03:06~03:19Z) ✋✋ 승인 후

### 배경
두 변경을 한 번의 재기동에 묶었다. ① `--authentication-token-webhook-cache-ttl` 0s → 2m (기본값). ② webhook-kubeconfig 의 `users[].user` 에 client-certificate / client-key 를 넣어 apiserver 가 웹훅에 **클라이언트 인증서를 제시**하게 함. 둘 다 apiserver 가 기동 시에만 읽는 설정이라 재기동이 필요하다.

웹훅은 재기동 **전에** `--client-ca` optional 로 올려 둔다. apiserver 가 아직 인증서를 안 보내도 거부하지 않고 peer CN 만 로그에 남기므로, 재기동 뒤 CN 이 보이는 것을 눈으로 확인한 다음 required 로 올린다. 실패해도 웹훅을 optional 로 되돌리면 끝나고 apiserver 재기동은 불필요하다.

TTL 2m 에서 기대하는 것: 폐기 → 401 지연이 **"폐기 시각 + 2분" 이 아니라 "마지막 웹훅 호출 시각 + 2분"** 이다. 캐시는 웹훅 응답을 받은 순간부터 산다.

### 절차와 로그
```
03:06:09Z  w6-make-client-cert.sh --bad-ca
           client-ca.crt / apiserver-client.crt (CN=kube-apiserver, EKU clientAuth) / bad-ca.crt
           /etc/kubernetes/webhook/apiserver-client.{crt 644, key 600} 배치 (apiserver 컨테이너 uid 0)
03:06:10Z  웹훅 재기동 db + client-ca OPTIONAL
           curl(인증서 없음) -> 200 peer_cn=null / curl(인증서) -> 200 peer_cn=kube-apiserver
03:07:28Z  새 토큰 alice-p8 / bob-p8 (10-14 발급분은 ttl 1h 만료)  -> alice 200, bob 403
03:07:28Z  렌더: sudo cat 매니페스트 사본 -> w2 --set-flag cache-ttl=2m
           diff (매니페스트)   58c58  - --authentication-token-webhook-cache-ttl=0s  ->  =2m
           diff (webhook-kubeconfig)  user: {}  ->  client-certificate / client-key 2줄
           kubectl config view 로 mtls kubeconfig 파싱 OK, openssl verify OK
03:07:57.820Z  APPLY  webhook-kubeconfig 교체 -> 매니페스트 cp
03:07:57.850Z  readyz 500  -> 03:07:58 연결 거부 ... 03:08:36 403 -> 03:08:37 500 -> 03:08:38.568Z 200
               다운타임 40.7초 (1차 39.5초와 동일 패턴)
03:08:57Z  alice 200, bob 403.  webhook.log: peer_cn=kube-apiserver ★  apiserver 로그 webhook/tls 오류 0
03:08:58Z  웹훅 재기동 REQUIRED.  curl(인증서 없음) -> tlsv13 alert certificate required
03:09:43Z  새 토큰으로 웹훅 호출 강제 -> 200, webhook.log peer_cn=kube-apiserver (required 에서 실제 호출 확인)
```

### B3 측정 (TTL 2m)
```
M1-1  prime 3s   03:09:43 캐시 채움 -> 03:09:46.5 폐기 -> 03:11:49.9 401   = 폐기 후 123.4초 (캐시 후 126초)
M1-2  prime 60s  03:11:50 캐시 채움 -> 03:12:53.1 폐기 -> 03:13:56.4 401   = 폐기 후  63.3초 (캐시 후 126초)
M1-3  prime 3s   03:13:56 캐시 채움 -> 03:13:59.6 폐기 -> 03:16:02.9 401   = 폐기 후 123.3초 (캐시 후 126초)
M3    유효 토큰 30초 직렬  kubectl 599건 (20.0 req/s)  웹훅 1건 (0.03 call/s)
M4    폐기 토큰 30초 직렬  kubectl 697건 (23.2 req/s)  웹훅 697건 (23.23 call/s) = 1:1  ← 예상 밖
      apiserver 로그 ×700: Unable to authenticate the request err="[invalid bearer token, revoked]"
```
세 번 모두 **캐시 채움 + 126초**(웹훅 TTL 120 + 전역 성공 캐시 10 − 폴링 오차)에 401. 폐기 시각과 무관하다.

### ★ 발견 F6 — 실패가 캐시되느냐는 웹훅 응답의 `status.error` 유무가 결정한다
M4 가 TTL 2m 에서도 1:1 로 나와 K8s 1.35 소스를 확인했다.
- `plugin/pkg/authenticator/token/webhook/webhook.go`: `Status.Authenticated=false` 일 때 `Status.Error` 가 **비어 있지 않으면 `(nil, false, errors.New(Status.Error))`**, 비어 있으면 `(nil, false, nil)` 을 반환.
- `pkg/kubeapiserver/authenticator/config.go`: 웹훅 인증기는 `tokencache.New(webhook, cacheErrs=**false**, ttl, ttl)` 로 감싼다.
- `token/cache/cached_token_authenticator.go`: `!cacheErrs && err != nil` 이면 **캐시하지 않고** 반환. `!ok && failureTTL>0` 이면 failureTTL 동안 캐시.

즉 우리 웹훅이 `status.error: "revoked"` 를 돌려주는 한 apiserver 는 그것을 "오류" 로 분류해 매 요청 웹훅을 다시 부른다. 재현 확인:
```
03:18:36Z  웹훅 --omit-status-error 로 재기동 (required mTLS 유지)
M4b  폐기 토큰 30초  kubectl 718건  웹훅 **1건**  (apiserver 로그: err="invalid bearer token" ×718)
M4c  미존재 토큰 30초 kubectl 711건  웹훅 **1건**
03:19:38Z  웹훅 기본값(status.error 포함)으로 복원
```

### 판정
| 항목 | 결과 |
|---|---|
| 재기동 | crash-loop 없음, 다운타임 **40.7초**, 인증서·SA 경로 정상 |
| mTLS | apiserver 가 클라이언트 인증서 제시 (`peer_cn=kube-apiserver`), required 전환 후에도 정상. 인증서 없는 호출은 handshake 거부 |
| B3 M1 (2m) | 폐기 → 401 **123.4 / 63.3 / 123.3초** = 항상 캐시 채움 + 126초 |
| B3 M3 (2m) | 유효 토큰 웹훅 호출 0.03 call/s (2분에 1회) |
| B3 M4 (2m) | `status.error` 포함 시 **1:1** (캐시 없음), 생략 시 **30초에 1건** |

### 설계 시사점 ★ (캐시 TTL 결정서의 핵심 입력)
1. **TTL 값보다 `status.error` 처리 방침이 웹훅 부하를 좌우한다.** TTL 2m 이라도 사유를 `status.error` 에 쓰면 무효 토큰 반복은 100% 웹훅에 도달한다. 생략하면 TTL 동안 차단된다. 사유는 웹훅 로그에만 남기고 `status.error` 는 비우는 것이 맞다. apiserver 로그에는 어차피 `invalid bearer token` 만 남고, 사용자는 두 경우 모두 401 만 본다.
2. 반대로 `status.error` 는 **"이 결과를 캐시하지 마라" 는 신호로 쓸 수 있다.** 예: DB 장애로 판정을 못 내린 경우(일시 오류)에는 error 를 채워 다음 요청이 즉시 재시도되게 하고, 확정 거부(revoked / expired / unknown)는 비워 캐시되게 한다.
3. 폐기 즉시성은 "폐기 후 N초" 가 아니라 "마지막 사용 후 TTL + 10초" 로 문서화해야 한다. 자주 쓰는 토큰일수록 캐시가 계속 갱신되어 폐기 반영이 TTL 끝까지 밀린다.
4. TTL 후보: 즉시성 요구가 "분 단위" 면 2m(기본) + error 생략. "10초 이내" 가 요구면 0s + 웹훅 자체 rate-limit (실패 캐시가 없으므로).

### 현재 상태 (P8 종료)
apiserver `cache-ttl=2m` + mTLS kubeconfig / 웹훅 pid 4161627 db + mTLS **required** + status.error 포함 / 토큰 API pid 3329325. `~/kube-apiserver.yaml.bak`(원본), `~/poc-authn/webhook-kubeconfig.phase1.yaml`(1차 kubeconfig) 보관.

## 9. P9 — mTLS 부정 시험: 웹훅이 다른 CA 만 신뢰할 때 (2026-10-16 05:06Z) ✋ 승인 후

### 배경
P8 은 "올바른 인증서면 통과" 만 보였다. 웹훅이 정말 검증하는지는 **진짜 apiserver 의 호출이 거부되는 것**으로 증명한다. 웹훅의 `--client-ca` 를 무관한 `bad-ca.crt` 로 바꾸면 apiserver 가 제시하는 인증서(client-ca 서명)는 신뢰 체인이 없어 handshake 에서 끊긴다. 이때 apiserver 는 TokenReview 응답 자체를 못 받으므로 1차 S10(웹훅 down)과 같은 "오류" 경로로 401 을 낸다.

### 절차와 로그
```
05:06:45Z  웹훅 재기동 --client-ca bad-ca.crt --client-cert-required
           curl(apiserver 인증서) -> tlsv1 alert unknown ca
05:06:47Z  새 토큰 alice-p9 -> kubectl get nodes -> 401 (0.05초 만에 실패, 재시도 없음)
           alice-p8 (마지막 인증 03:19Z, 캐시 만료) -> 401
           admin -> 2 nodes (영향 없음)
           webhook.log ×2: tls/conn error [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
           apiserver  ×2: Failed to make webhook authenticator request: Post ".../authenticate?timeout=30s":
                          remote error: tls: unknown certificate authority
                      ×2: Unable to authenticate the request err="[invalid bearer token, Post ...: remote error: tls: unknown certificate authority]"
05:06:47Z  --client-ca client-ca.crt 로 복원
05:06:49Z  alice-p9 -> 200, webhook.log peer_cn=kube-apiserver
```

### 판정
**합격.** 웹훅이 신뢰하지 않는 CA 의 인증서는 진짜 apiserver 라도 거부된다. 복원은 웹훅 재기동만으로 끝났다 (apiserver 재기동 불필요).

### 설계 시사점 / 런북
- 증상은 1차 F4(웹훅 down)와 같은 "토큰 사용자 전원 즉시 401" 이고, 구분은 apiserver 로그 문구로 한다: `connection refused` = 웹훅 다운, `tls: unknown certificate authority` / `x509` = **인증서 신뢰 문제** (CA 교체, 인증서 만료, 로테이션 누락). 런북 2단계의 grep 대상에 `tls:` 를 추가.
- 인증서 로테이션 사고의 전형이 이 모양이다. SEC09 의 자동 로테이션을 넣을 때 **웹훅 쪽 client-ca 번들에 신·구 CA 를 겹쳐 두는 기간**이 있어야 apiserver 재기동 없이 넘어갈 수 있다.

## 10. P10 — B8 웹훅 장애 (hang) 와 발급 API 장애 (2026-10-16 05:12Z) ✋ 승인 후

### 배경
1차 S10 은 웹훅 프로세스를 **종료**한 경우였다. 포트가 닫혀 있어 apiserver 는 `connection refused` 로 즉시 401 을 냈다. 운영에서 더 흔한 장애는 프로세스는 살아 있는데 응답을 못 하는 **hang**(GC 정지, DB 락, 디스크 풀)이다. 이때 apiserver 는 1차 F2 에서 본 `?timeout=30s` 만큼 기다린다. `kill -STOP` 으로 그 상태를 정확히 만들 수 있다.

두 번째는 발급면 장애다. 웹훅은 DB 파일만 읽고 토큰 API 프로세스와 통신하지 않으므로, 토큰 API 가 죽어도 **이미 발급된 토큰의 인증은 계속**되어야 한다.

### 절차와 로그
```
05:12:34Z  alice-p10 로 1회 인증 (캐시 채움). alice-p10b 는 미사용 (웹훅 호출 강제용)
05:12:34.749Z  kill -STOP <webhook>
05:12:34.8Z    alice-p10b kubectl 시작 (백그라운드)
05:12:37Z  hang 중  admin -> 2 nodes 정상
           hang 중  SA 토큰(kubectl create token default) -> 403 (SA 로 인증됨, RBAC 없음) 정상
           hang 중  alice-p10 (3초 전 인증, 캐시) -> 200
           hang 중  apiserver -> 8443 established 연결 1개 (대기 중)
05:13:04.797Z  alice-p10b 결과: 401  — **정확히 30.0초** 후
           apiserver 로그 ×1: Failed to make webhook authenticator request: Post ".../authenticate?timeout=30s":
                              net/http: request canceled (Client.Timeout exceeded while awaiting headers)
05:13:04.847Z  kill -CONT  (hang 30.09초)
05:13:05Z  alice-p10b -> 200, webhook.log peer_cn=kube-apiserver (즉시 복귀)
05:13:05Z  토큰 API stop.  발급 시도 -> 연결 실패 (예상)
05:13:17Z  alice-p10b -> 200 (캐시)
           API 다운 중 DB 에 직접 넣은 토큰(장애 전 발급분 모사) 첫 사용 -> 200, 웹훅 로그 ok
05:13:17Z  토큰 API start (pid 183224)
```

### 판정
| 항목 | 결과 |
|---|---|
| hang 중 신규 토큰 인증 | **30.0초 대기 후 401**. 재시도 없이 1회 (`Client.Timeout exceeded`) |
| hang 중 admin / SA / 캐시된 토큰 | 모두 정상 |
| CONT 후 | 1초 내 복귀 |
| 토큰 API 다운 | 기존 토큰 인증 정상, 신규 발급만 불가 |

### 설계 시사점 / 런북
- 웹훅 **down** 은 즉시 401(F4), **hang** 은 사용자당 30초 멈춤 뒤 401. 사용자 체감이 다르다. hang 이 "kubectl 이 멈춰요" 로 접수되면 apiserver 로그의 `Client.Timeout exceeded while awaiting headers` 가 결정적 단서다.
- hang 중에도 **캐시된 토큰은 계속 통과**한다(TTL 2m 동안). 장애 체감은 "새로 로그인하는 사람" 과 "2분 넘게 쉬었다가 돌아온 사람" 부터 시작된다. 이것도 TTL 을 0 이 아닌 값으로 두는 이유가 된다.
- apiserver 는 hang 한 웹훅에 요청당 30초짜리 연결을 하나씩 잡는다. 사용자가 많으면 apiserver 의 웹훅 클라이언트 연결이 그만큼 쌓인다. 웹훅 앞단에 짧은 자체 타임아웃(예: 5초)과 헬스체크 기반 재시작이 필요하다.
- 발급면(토큰 API, DB 쓰기)과 인증면(웹훅, DB 읽기)은 장애 도메인이 분리된다. 운영 설계에서 두 컴포넌트의 SLO 를 다르게 잡을 수 있다.

## 11. P11 — B9 감사 추적 종합 + 평문 누출 전수 검사 (2026-10-16 05:14Z, 읽기 전용)

### 배경
SEC08 / K51 이 요구하는 것은 "발급 / 폐기 / 인증 성공·실패 / API 호출" 이 **누가·언제·무엇을** 으로 이어지는 것이다. 세 로그는 역할이 다르다. `token_audit.jsonl` 은 발급·폐기(actor 포함), `webhook.log` 는 인증 성공·실패와 사유(**401 은 여기에만** 남는다. apiserver 감사는 user 가 비어 정책에 안 걸림), apiserver `audit.log` 는 인증 성공 후의 API 호출과 인가 결과. 연결 키는 해시 앞 12자와 username(owner_id) 이다.

### 결과
```
평문 전수 검사: ~/poc-authn/*.kubeconfig 의 토큰 20개 × (webhook.log, webhook.out, token_api.log, token_api.out,
                token_audit.jsonl, apiserver audit.log, apiserver 컨테이너 로그) -> 평문 일치 0건, "pt_" 문자열 0건
로그 규모:      webhook.log 2969행 (ok 32 / deny 1418: revoked 1401, unknown 12, expired 3, cluster-mismatch 2)
                token_audit.jsonl 24행 (issue 17 / revoke 6 / revoke-user 1)
                apiserver audit.log 4579행 (alice 4534, bob 6, poc-user 39)
토큰 1개 추적 (alice-p10b, hash 5e900977c76b):
  05:12:34Z token_audit  issue  actor=dan.park  token_id=18bf45c2c590
  05:13:04Z webhook      ok     ← hang 중 대기하던 요청이 CONT 직후 처리됨 (apiserver 는 이미 30초 타임아웃으로 포기)
  05:13:05Z webhook      ok     ← 재시도
  05:13:05Z apiserver    alice list nodes 200  sourceIP 172.16.1.120  kubectl/v1.35.1
```

### 판정
**합격.** 세 로그가 해시 접두와 username 으로 한 줄로 이어지고, 평문은 어디에도 없다.

### 설계 시사점
- SEC08 의 메타데이터 항목(actor / timestamp / source IP / action / result) 중 source IP 와 userAgent 는 apiserver 감사에만 있다. 세 로그를 **한 곳으로 모으는 파이프라인**(예: 해시 접두를 공통 필드로 S3 export) 이 운영 요건이다.
- 인증 실패(401) 추적은 apiserver 감사가 아니라 **웹훅 로그가 1차 기록**이다. 웹훅 로그의 보존 기간(30일+)과 무결성이 SEC08 범위에 들어간다.
- P10 의 "타임아웃 후 뒤늦게 처리된 요청" 은 웹훅 로그에는 ok 로 남지만 사용자에게는 401 이었다. 로그만으로 사용자 체감을 판단할 때 주의할 점으로 런북에 기록.

## 12. 역할 기반 권한 (role → 그룹 → RBAC) C1~C6 (2026-10-16 05:38~05:41Z) ✋ RBAC 적용 승인 후

### 배경
K8s 의 인가는 인증기가 돌려준 **그룹**에 바인딩을 거는 방식이다. 웹훅이 토큰의 `role` 을 그룹 `mk8s:<role>` 로 바꿔 주면, 나머지는 표준 RBAC 이다. 역할은 발급 시 토큰에 고정되고(role 변경 = 새 토큰), RBAC 은 역할별로 **사전에** 만들어 둔다. 사용자 요구:

| role | 권한 | 구현 |
|---|---|---|
| skt-admin | 컨트롤 플레인 포함 전체 CRUD | ClusterRoleBinding → 빌트인 `cluster-admin` |
| tenant-operator | 고객 네임스페이스 조회만, 컨트롤 플레인 접근 불가 | 고객 네임스페이스마다 RoleBinding → `view` |
| tenant-user | 고객 네임스페이스 CRUD, 컨트롤 플레인 접근 불가 | 고객 네임스페이스마다 RoleBinding → `edit` |

고객 네임스페이스는 이 클러스터에 있는 `default`, `dynamo-system`, `inferium` (slurm 은 없어 제외). 바인딩이 없는 네임스페이스는 자동으로 403 이므로 "컨트롤 플레인 접근 불가" 는 별도 거부 규칙 없이 성립한다. 테넌트 역할이 `kubectl get ns <이름>` 은 되도록 `resourceNames` 로 좁힌 ClusterRole 하나를 더 뒀다(`list` 는 resourceNames 로 못 좁히므로 목록은 불가).

웹훅은 `mk8s:<role>` 과 함께 기존 `poc-tenant-<tenant>` 그룹도 돌려준다. 1차 감사 정책이 그 그룹으로 기록하기 때문에 apiserver 재기동 없이 감사가 이어진다. 기존 토큰 18건은 DB 마이그레이션으로 `role=legacy` 가 되어 이전 그룹만 받는다.

### 절차와 로그
```
05:38:40Z  토큰 API 재기동 -> role 컬럼 추가 (legacy 18건). 웹훅 재기동 (db + mTLS required)
           bob-p8 (legacy) -> 403 (인증 유지)
05:40:50Z  kubectl apply -f authn/rbac/roles/   -> 9개 생성 (CRB 2, CR 1, RB 6)
05:40:50Z  역할별 토큰 발급: u-skt-admin / u-tenant-operator / u-tenant-user (tenant a)

C1 skt-admin        get pods -A OK / get nodes OK / kube-system configmap create 201, delete 200
C2 tenant-operator  default·inferium·dynamo-system pods list 200
                    kube-system 403 / team-b 403 / default configmap create 403 / secrets list 403
                    get ns default 200 / get ns (목록) 403
                    get nodes 200  ← ★ 예상 밖 (아래)
C3 tenant-user      default·inferium configmap create 201 / get 200 / delete 200
                    kube-system create 403, list 403 / rolebinding(admin) 생성 403 (권한 상승 시도 차단)
                    get nodes 200  ← ★ 동일
C4 role=root -> 400 "role must be one of [...]" / kubeconfig 경로 role=superuser -> 400
C5 apiserver audit: 세 사용자 모두 groups 에 mk8s:<role>, 허용은 "RBAC: allowed by RoleBinding mk8s-tenant-user/default ..." 식으로 바인딩 이름까지 기록, 거부는 reason 없음(403)
C6 owner=dual 에 tenant-operator / skt-admin 토큰 2개 -> operator 는 kube-system 403, admin 은 200. admin 토큰만 폐기
```

### 판정
| 항목 | 결과 |
|---|---|
| C1 skt-admin | **합격** |
| C2 tenant-operator | 네임스페이스 경계·읽기 전용 **합격**. 단 `get nodes` 가 허용됨 (★) |
| C3 tenant-user | 네임스페이스 경계·CRUD·권한 상승 차단 **합격**. 단 `get nodes` 허용 (★) |
| C4 role 검증 | **합격** |
| C5 감사 | **합격** — 그룹과 바인딩 이름이 남아 "왜 허용됐나" 추적 가능 |
| C6 다중 역할 | **합격** — 같은 소유자라도 토큰별 역할 독립 |

**★ `get nodes` 허용 원인**: 1차 A5 에서 만든 `poc-authn-node-reader`(ClusterRoleBinding → group `poc-tenant-a`) 가 남아 있고, 웹훅이 감사 연속성을 위해 `poc-tenant-a` 그룹을 함께 돌려주기 때문이다. 감사 로그에도 `allowed by ClusterRoleBinding "poc-authn-node-reader"` 로 정확히 찍혔다. 역할 모델의 결함이 아니라 1차 잔존물이며, 해당 바인딩을 삭제하면 사라진다(감사 정책은 그룹 이름만 보므로 삭제해도 감사는 유지).

### 후속 조치 (2026-10-16 05:45Z, 승인 후)
```
kubectl delete clusterrolebinding,clusterrole poc-authn-node-reader
+11초 후  tenant-operator get nodes -> 403 / tenant-user get nodes -> 403 / skt-admin 2 nodes
          tenant-operator get pods default -> 200 (역할 권한 유지), 감사 로그에 u-tenant-* 계속 기록
```
C2 / C3 의 ★ 항목 해소. 1차 산출물 중 클러스터에 남은 것은 이제 역할 RBAC 9개뿐.

### 설계 시사점
- **그룹은 더해지기만 한다.** 한 사용자가 여러 그룹을 받으면 권한은 합집합이다. 역할 그룹 외에 붙는 그룹(테넌트 그룹, `system:authenticated`)에 어떤 바인딩이 걸려 있는지가 실제 권한을 정한다. 운영에서는 "역할 그룹 외 바인딩 금지" 를 점검 항목으로 둬야 한다.
- 네임스페이스가 늘면 RoleBinding 도 늘어난다. 고객 네임스페이스 생성 시 바인딩을 같이 만드는 자동화(또는 네임스페이스 라벨 기반 컨트롤러)가 필요하다.
- `edit` 는 해당 네임스페이스의 Secret 읽기·쓰기를 포함하고 `view` 는 Secret 을 제외한다 (C2 secrets 403). 요구사항과 맞는지 확인 필요.
- 감사 로그의 `authorization.k8s.io/reason` 이 바인딩 이름을 적어 주므로, 권한 사고 조사 시 "어느 바인딩이 열어 줬나" 를 바로 알 수 있다.

## 13. API 문서와 CLI (2026-10-16 05:45~05:49Z)

### 배경
PoC 산출물을 다른 사람이 쓰려면 규격과 도구가 필요하다. 규격은 사람용(`authn/API.md`)과 기계용(`authn/openapi.yaml`, OpenAPI 3.0.3) 둘 다 두고, CLI(`authn/cli/mk8s_token.py`)는 그 규격만 보고 만든다. CLI 는 API 호출 외에 아무 로직도 갖지 않는다. 단 `list` 의 `--role` / `--status` 필터는 API 가 `owner` 필터만 제공하므로 CLI 가 응답을 받아 로컬에서 거른다(`status` 는 `revoked_at` / `expires_at` 로 계산).

### 산출물
| 파일 | 내용 |
|---|---|
| `authn/API.md` | 개요·흐름도, 역할/그룹/RBAC 표, 엔드포인트 7개 요청·응답 예시, 검증 순서와 폐기 반영 시간, F6 권고, 감사 로그 3종 포맷과 연결 키, 오류 코드, 보안 메모, 미구현 목록, curl 예시 |
| `authn/openapi.yaml` | 경로 4개 (`/tokens`, `/tokens/{id}`, `/clusters/{id}/kubeconfig`, `/healthz`), 스키마 Role(enum) / Token / TokenWithSecret / IssueRequest / Error |
| `authn/cli/mk8s_token.py`, `README.md` | 하위 명령 `issue` `kubeconfig` `list` `get` `revoke` `revoke-user` `health`, `--json`, 환경변수 `MK8S_TOKEN_API` `MK8S_ACTOR`. 종료 코드 0 / 1(API 4xx·5xx) / 2(연결 불가·사용법·중단) |

### 시험
로컬(가짜 API, 포트 18080): 7개 명령 모두 동작. 잘못된 role 은 argparse 가 rc 2 로 거부, 없는 id 폐기는 `HTTP 404` rc 1, 같은 id 재폐기는 멱등 rc 0, `revoke-user` 는 확인 질문에 `n` 이면 rc 2, API 연결 불가 rc 2. kubeconfig 파일은 600.

mgmt01 (실제 클러스터):
```
05:48:31Z  mk8s-token kubeconfig carol a tenant-user --ttl 3600 -o carol-cli.kubeconfig  -> token_id a7f8dbf10354
           kubectl create configmap (default) -> created / delete -> deleted / get pods kube-system -> 403
05:48:32Z  mk8s-token list --owner carol      -> 1행, status valid, last_used_at 05:48:31Z
           mk8s-token list --status valid     -> 12행 (legacy 7, 역할 토큰 5). role 컬럼으로 legacy 와 구분됨
           mk8s-token revoke-user carol -y    -> revoked 1
05:48:43Z  kubectl (+11초) -> 아직 200  ← TTL 2m 이라 마지막 사용 + 126초까지 통과 (§8 과 일치)
           mk8s-token --json get a7f8dbf10354 -> revoked_at 기록 확인
           token_audit.jsonl: issue / revoke-user 2행 (actor dan.park)
```

### 판정
**합격.** CLI 만으로 발급 → 사용 → 조회 → 폐기 → 감사 확인이 끝난다.

### 추가 (05:55Z) — 대화형 모드
인자 없이 실행하면 번호 메뉴(목록에서 번호로 골라 상세·폐기, 역할 번호 선택, 발급, kubeconfig 저장, 소유자 전체 폐기, health). 로컬에서 키 입력을 흘려 넣어 전 경로 확인. 서브커맨드 모드는 그대로 (자동화용).

### 비고
- CLI 는 폐기 직후에도 kubectl 이 한동안 되는 것을 숨기지 않는다. 대화형 모드의 폐기 메시지에는 안내 문구를 넣었다. 운영 CLI 라면 `revoke` 출력에 "마지막 사용 + TTL 까지 유효할 수 있음" 안내를 넣는 것이 맞다.
- 로컬 시험에서 zsh 는 문자열 변수 안의 명령을 단어 분리하지 않아 `$T` 식 호출이 실패했다. 시험 스크립트는 bash 함수로 작성.

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
