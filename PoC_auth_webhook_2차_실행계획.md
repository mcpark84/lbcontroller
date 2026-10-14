# 실행 계획 — kubeconfig 토큰 인증 웹훅 PoC (2차)

> 대상: gasandevcontrol (mgmt01 컨트롤 플레인 단일 + dev001, K8s v1.35.1)
> 상위 계획서: `PoC_auth_webook_plan.md` §5 / 1차 실행계획 `PoC_auth_webhook_실행계획.md` / 1차 결과 `test_result_webhook.md`
> 작성: 2026-10-13

---

## 0. 출발점 (1차 종료 시점의 상태)

| 항목 | 상태 | 2차에서의 의미 |
|---|---|---|
| apiserver | 웹훅 + 감사 플래그 적용됨, `cache-ttl=0s` | **재기동 없이** B1~B7 진행 가능 |
| 웹훅 서버 | `127.0.0.1:8443` nohup, allow-all, groups=poc-tenant-a | 검증 함수를 DB 조회로 교체 (웹훅만 재기동) |
| RBAC | group `poc-tenant-a` → nodes get/list/watch | 그대로 재사용. tenant b 는 바인딩 없음 → 403 으로 "인증은 됐으나 인가 없음" 구분에 활용 |
| 감사 정책 | users `poc-user`, groups `poc-tenant-a/b` Metadata | 2차 사용자도 group 규칙으로 잡힘 → 정책 변경 불필요 |
| 발견 F1 | apiserver 전역 토큰 성공 캐시 **10초** (플래그 무관) | B3 의 기준선. "TTL 0s = 매 요청 웹훅 호출" 은 성립하지 않음 |
| 발견 F2 | apiserver 는 `POST /authenticate?timeout=30s` | B8 hang 시나리오의 예상 상한 |

## 1. 확정할 결정 (✋ 이 계획 승인 = 아래 결정 승인)

| 결정 | 내용 | 사유 |
|---|---|---|
| 프로세스 구성 | **토큰 API** (`token_api.py`, `127.0.0.1:8080`, 평문 HTTP) 와 **웹훅** (기존 8443) 을 **별도 프로세스**로 두고 sqlite 파일 1개(`~/poc-authn/tokens.db`, WAL) 공유 | 설계상 발급면(포탈 API)과 인증면(웹훅)은 다른 컴포넌트. "발급 API 가 죽어도 인증은 계속된다"를 그대로 보여줄 수 있음 |
| 토큰 형식 | `pt_` + 32바이트 난수 base64url (43자). DB 에는 **SHA-256 hex 만** 저장 | 계획서 §5.1 스키마. 접두사는 로그/지원 시 식별용 |
| 해시 알고리즘 | **SHA-256 단독** (salt / 느린 KDF 없음). 1차는 저장 자체가 없었음 | NIST SP 800-63B: 엔트로피 112비트 이상 look-up secret 은 승인 해시로 충분. 256비트 난수라 bcrypt 류는 불필요하고 웹훅 지연만 증가. SOC 2 는 알고리즘을 지정하지 않음 |
| 신원 매핑 | `username = owner_id`, `groups = ["poc-tenant-<tenant>"]`, `uid = token.owner_id` | 계획서 "2차 그룹은 고정값". 기존 RBAC 과 감사 정책을 그대로 재사용 |
| 클러스터 ID | `gasandevcontrol`. 웹훅은 `--cluster-id` 로 자기 ID 를 알고, 토큰 row 의 `cluster` 와 비교 | 단일 클러스터라 B5 는 **다른 cluster 값으로 발급한 토큰**으로 모사 (1차 계획 §6 결정) |
| 로그 | 웹훅·토큰 API 모두 **해시 앞 12자**만 기록. 1차의 "평문 앞 8자" 제거 | 1차 결과서 §15.3 신규 항목 |
| 재기동 횟수 | **2회**: ① P8 (`cache-ttl 2m` + mTLS 클라이언트 인증서 동시 반영) ② P12 A10 롤백 | TTL 변경과 mTLS 를 한 번에 묶음. TTL 을 0s 로 되돌리는 재기동은 하지 않음 (롤백이 원본 복원) |
| mTLS 실패 시 복구 | 웹훅을 `--client-cert-optional` 로 재기동 (웹훅 재기동만, apiserver 재기동 불필요) | apiserver 는 인증서를 계속 제시하고 웹훅이 검증을 안 할 뿐 |
| 토큰 API 자체 인증 | 없음 (`X-Actor` 헤더를 감사 기록용으로만 받음) | 포탈 ↔ API 인증은 PoC 범위 밖. loopback 바인딩으로 노출 차단 |
| 산출물 | 결과서는 `test_result_webhook.md` 에 **§16 부터 이어서** 기록 (항목마다 "배경" 소절) | 1차와 한 문서 |

## 2. 변경 범위 (1차 대비 추가분)

| 위치 | 내용 | 되돌리기 |
|---|---|---|
| `~/poc-authn/tokens.db` | sqlite (해시만) | 삭제 |
| `~/poc-authn/token_api.log`, `token_audit.jsonl` | API 로그 / 발급·취소 감사 (JSON lines) | 삭제 |
| `~/poc-authn/tls/client-ca.*`, `apiserver-client.*` | mTLS 용 클라이언트 CA 와 apiserver 클라이언트 인증서 | 삭제 |
| `/etc/kubernetes/webhook/apiserver-client.{crt,key}` | apiserver 가 웹훅에 제시할 인증서 (이미 마운트된 디렉터리라 파일 추가는 재기동 불필요, **참조는 재기동 필요**) | 디렉터리 삭제 (A10) |
| `/etc/kubernetes/webhook/webhook-kubeconfig.yaml` | `users[0].user` 에 `client-certificate` / `client-key` 추가 | A10 |
| `/etc/kubernetes/manifests/kube-apiserver.yaml` | `--authentication-token-webhook-cache-ttl=0s` → `2m` | A10 (원본 복원) |
| 클러스터 | 추가 리소스 **없음** | — |

## 3. 코드 (P1 에서 작성, 전부 python 표준 라이브러리)

### 3.1 `authn/token_api.py`

계획서 §5.1 의 4개 API 를 동일 계약으로 구현.

| 메서드 | 경로 | 동작 |
|---|---|---|
| `POST /tokens` | body `{owner_id, tenant, cluster, ttl_seconds}` | 발급(`issued_via=operator`). 응답에 **평문 1회** + `id`, `expires_at` |
| `GET /tokens[?owner=]` | | `id, owner_id, tenant, cluster, issued_via, created_at, expires_at, revoked_at, last_used_at, hash_prefix`. 평문·전체 해시 없음 |
| `DELETE /tokens/{id}` | | `revoked_at` 기록 |
| `DELETE /tokens?user={owner}` | | 소유자 토큰 **전량** `revoked_at` 기록, 건수 반환 (K21) |
| `GET /clusters/{id}/kubeconfig?owner=&tenant=&ttl=` | | 토큰 발급(`issued_via=kubeconfig`) + endpoint/CA 조립 → kubeconfig YAML. endpoint/CA 는 기동 시 `kubectl config view --raw` 에서 읽음 (N3 모사) |
| `GET /healthz` | | DB 경로, 토큰 건수 |

- 스키마: `id, hash, owner_id, tenant, cluster, issued_via, created_at, expires_at, revoked_at, last_used_at`
- 발급 / 취소마다 `token_audit.jsonl` 에 `{ts, actor, action, token_id, owner_id, hash_prefix}` 1행

### 3.2 `authn/webhook_server.py` 확장

- 새 인자 `--db PATH --cluster-id ID` → **DB 모드**. 없으면 1차 allow-all 그대로 (재현성 유지)
- 검증 순서 (계획서 §5.1): 해시 → 조회 없음 `unknown` / `revoked_at` 있음 `revoked` / `expires_at` 경과 `expired` / `cluster ≠ --cluster-id` `cluster-mismatch` → `authenticated:false` + `status.error=<사유>`. 통과 시 `last_used_at` 갱신 후 `username=owner_id, groups=[poc-tenant-<tenant>]`
- 로그 1행: 시각 / 해시 앞 12자 / 결과 / 사유 / username / groups / 처리 ms / **peer 인증서 CN (mTLS 시)**
- 새 인자 `--client-ca PATH [--client-cert-required]` → `ssl` 컨텍스트 `CERT_REQUIRED` 또는 `CERT_OPTIONAL`
- `/healthz` 에 `mode: db|allow-all`, `cluster_id`, `mtls: off|optional|required` 노출

### 3.3 스크립트

| 파일 | 역할 |
|---|---|
| `authn/scripts/w4-run-token-api.sh start\|stop\|status` | nohup, pidfile `~/poc-authn/token_api.pid` |
| `authn/scripts/w4-tok.sh issue\|list\|revoke\|revoke-user\|kubeconfig` | curl 래퍼. 결과서에 그대로 붙일 명령 |
| `authn/scripts/w5-measure-revoke.sh TOKEN_ID KUBECONFIG` | `kubectl get nodes --request-timeout=5s` 1초 폴링 → `DELETE` → **첫 401 까지 초** 출력. 반복 3회 |
| `authn/scripts/w5-webhook-rate.sh KUBECONFIG SECONDS [PARALLEL]` | 연속 `kubectl get nodes` 루프 중 웹훅 로그 초당 건수 집계 |
| `authn/scripts/w6-make-client-cert.sh` | client CA + apiserver 클라이언트 인증서 생성, `/etc/kubernetes/webhook/` 배치, 새 webhook-kubeconfig 렌더 (별도 파일) |
| `authn/scripts/w2-render-apiserver.py` 확장 | `--set-flag authentication-token-webhook-cache-ttl=2m` 모드 (기존 값 치환만, 새 파일 생성 + YAML 검증) |

`w1-run-webhook.sh` 는 env `WEBHOOK_DB`, `WEBHOOK_CLUSTER_ID`, `WEBHOOK_CLIENT_CA`, `WEBHOOK_CLIENT_CERT_REQUIRED` 를 추가로 전달.

## 4. 핵심 시나리오 — 발급 → 해시 저장 → 사용 → 폐기 → 인증 실패 (end-to-end)

P3 ~ P4 ~ P7 에 흩어진 항목을 **토큰 하나의 생애**로 이어서 실행하고, 각 단계에서 네 곳(API 응답 / DB / 웹훅 로그 / kubectl)을 동시에 확인한다. 결과서에는 이 순서 그대로 시간순 로그를 붙인다.

| # | 단계 | 명령 | API 응답 | DB (`sqlite3 tokens.db`) | 웹훅 로그 | kubectl |
|---|---|---|---|---|---|---|
| E1 | 발급 | `w4-tok.sh kubeconfig alice a` → `alice.kubeconfig` | 평문 `pt_…` **1회** 포함된 kubeconfig | row 1건: `hash=<sha256 hex 64자>`, `revoked_at=NULL`, `last_used_at=NULL` | 호출 없음 | — |
| E2 | 해시 확인 | `sha256sum <<< $TOKEN` vs `SELECT hash` | — | **두 값 일치**. `SELECT * WHERE hash LIKE 'pt_%'` → 0건 (평문 미저장) | — | — |
| E3 | 재조회 | `w4-tok.sh list alice` | `hash_prefix` 12자만, 평문·전체 해시 없음 | 변화 없음 | — | — |
| E4 | 사용 | `kubectl --kubeconfig alice.kubeconfig get nodes` | — | `last_used_at` 갱신 | `ok alice groups=poc-tenant-a hash=<12자>` | **200** |
| E5 | 캐시 확인 | 10초 내 재실행 | — | — | **호출 없음** (전역 10초 캐시) | 200 |
| E6 | 폐기 | `w4-tok.sh revoke <id>` (`DELETE /tokens/{id}`) | `revoked_at` 시각 | `revoked_at` 기록, row 는 **삭제하지 않음** (감사 추적) | — | — |
| E7 | 폐기 직후 | 1초 간격 `get nodes` 폴링 (`w5-measure-revoke.sh`) | — | — | 캐시 만료 시점에 `deny reason=revoked` | 200 → **401** 전환, 전환까지 초 기록 (≤10 예상) |
| E8 | 폐기 후 재시도 | 11초 이상 뒤 `get nodes` 3회 | — | `last_used_at` **갱신 안 됨** | 매 요청 `deny reason=revoked` (실패는 캐시되지 않음) | 401 × 3 |
| E9 | 존재하지 않는 토큰 | kubeconfig 의 token 을 한 글자 바꿔 실행 | — | — | `deny reason=unknown` | 401 |
| E10 | 감사 연결 | `token_audit.jsonl` / `webhook.log` / apiserver `audit.log` 를 `hash_prefix` 와 `alice` 로 grep | 발급·폐기 2행 | — | E4 ok 1건, E7~E9 deny N건 | audit.log 에 E4 요청 `user.username=alice` |

**배경 (결과서에 그대로 옮길 내용)**
- 웹훅은 매 요청마다 평문을 받지만(apiserver 가 Bearer 값을 TokenReview `spec.token` 으로 전달), 서버 측은 해시로만 조회하므로 DB·로그·API 응답 어디에도 평문이 남지 않는다. 평문은 사용자의 kubeconfig 파일에만 존재한다.
- 폐기는 row 삭제가 아니라 `revoked_at` 기록이다. 삭제하면 "누가 언제 폐기했나" 와 "폐기 후 누가 계속 시도했나" 를 추적할 수 없다.
- E7 의 지연은 웹훅 TTL 이 아니라 apiserver 전역 성공 캐시(10초) 때문이다. E8 에서 실패가 매번 웹훅에 도달하는 것은 실패가 그 캐시에 들어가지 않기 때문이다.

E1~E10 은 P3·P4 실행 중에 이 순서로 수행하며, P7(B6 일괄 폐기)은 같은 흐름을 `DELETE /tokens?user=alice` 로 한 번 더 반복한다.

## 5. 단계와 체크포인트

✋ = 실행 직전 승인. ✋✋ = apiserver 재기동 (사전 공지 + 시각 지정). 표시 없음 = 로컬 작업 / 읽기 전용 / 토큰 사용자(PoC 본인)에게만 영향.

| 단계 | 내용 | 영향 범위 | 승인 | 합격 기준 |
|---|---|---|---|---|
| **P0** | 사전 확인: 웹훅 생존, apiserver 플래그, 1차 토큰 kubeconfig 200, 감사 로그 증가, 8080 포트 비어 있음, `sqlite3` 유무 | 없음 | — | 1차 종료 상태와 동일 |
| **P1** | §3 코드 작성. 로컬 자가 시험 (sqlite + curl 로 발급→TokenReview→취소→거부), commit/push, mgmt01 pull | git | — | 로컬에서 unknown / revoked / expired / cluster-mismatch 4개 사유가 각각 `authenticated:false` |
| **P2** | 토큰 API 기동 → 웹훅을 DB 모드로 재기동 | **1차 토큰 `poc-token-0001` 은 이 시점부터 401** (DB 에 없음) | ✋ | `/healthz` `mode: db`, 1차 토큰 401 + 웹훅 로그 `unknown` |
| **P3 = B1, B2, B7** | **§4 E1~E5** 수행. `GET /clusters/gasandevcontrol/kubeconfig?owner=alice&tenant=a` → `kubectl get nodes`. `owner=bob&tenant=b` 도 발급. sqlite 덤프 / `GET /tokens` 확인 | 없음 | — | alice **200**, bob **403** (인증 OK, 바인딩 없음). DB·API 응답·로그에 평문 없음. `?owner=` 로 목록 분리. `last_used_at` 갱신 |
| **P4 = B3 (TTL 0s)** | **§4 E6~E10** 수행. `w5-measure-revoke.sh` 3회. `w5-webhook-rate.sh` 직렬 30초 + 병렬 5. **장기 연결**: `kubectl get nodes -w` 띄운 채 취소 → 60초 관찰 | 없음 | — | 취소→401 **≤ 10초** 기록. 호출률 기록. watch 는 **유지**, 같은 시각 신규 요청은 401 |
| **P5 = B4** | `ttl_seconds=30` 발급 → 즉시 200 → 만료 후 401 | 없음 | — | 만료 후 ≤ 40초(30 + 캐시 10) 내 401, 웹훅 로그 `expired` |
| **P6 = B5** | `cluster=other-cluster` 로 발급 → 이 클러스터에 사용 | 없음 | — | **401**, 웹훅 로그 `cluster-mismatch` |
| **P7 = B6** | alice 토큰 2개 보유 상태에서 `DELETE /tokens?user=alice` | 없음 | — | alice 2개 모두 401, bob 은 여전히 **403** (인증 유지) |
| **P8 = B3 (TTL 2m) + mTLS 적용** | ① `w6` 로 인증서 생성, 웹훅 `--client-ca` **optional** 재기동, curl(인증서 포함/미포함) 자가 시험 ② 렌더: `cache-ttl=2m` + webhook-kubeconfig 에 client cert. **diff 제시** ③ 적용, `/readyz` 다운타임 측정 ④ 웹훅 로그에 peer CN 확인 → 웹훅 **required** 로 재기동 ⑤ `w5-measure-revoke.sh` 3회 + 호출률 재측정 | **apiserver 재기동 (약 40초)** | ✋✋ | 다운타임 기록. peer CN = apiserver 클라이언트 인증서. required 전환 후에도 200. 취소→401 이 **10초 초과 ~ 120초** 구간에 분포 |
| **P9 = mTLS 부정 시험** | 웹훅을 **다른 CA** 로 required 재기동 → 토큰 요청 → 복원 | 토큰 사용자 401 (수 초) | ✋ | 401, apiserver 로그 `tls: bad certificate` 류 문구 기록. 복원 후 200 |
| **P10 = B8** | 웹훅 `kill -STOP` → 토큰 `kubectl` 시간 측정 / admin / SA 경로 → `kill -CONT`. 토큰 API `stop` 상태에서 인증 계속되는지 | 토큰 사용자 지연·실패 | ✋ | 토큰 요청 **실패까지 초** (예상 30초 × 재시도 횟수), admin/SA 정상, CONT 후 복귀. **토큰 API 중지 중에도 웹훅 인증 정상** |
| **P11 = B9 + 로그 점검** | alice 토큰 하나를 발급→사용→취소→재사용. 세 로그(`token_audit.jsonl`, `webhook.log`, apiserver `audit.log`) 를 해시 앞 12자 / username 으로 연결. 전 로그에서 평문 토큰 grep | 없음 | — | 발급·취소 / 인증 성공·실패 / API 호출 3곳에 동일 식별자. **평문 0건** |
| **P12 = A10 롤백** | `w2-watch-readyz.sh` 기동 → `cp ~/kube-apiserver.yaml.bak` 복원 → 다운타임 측정 → 토큰 401(웹훅 미호출) / admin 200 → 웹훅·토큰 API stop → RBAC 삭제 → 증적 보관(`~/poc-authn` 로그를 `~/poc-authn-evidence.tgz` 로) → `/etc/kubernetes/webhook`, `/var/log/kubernetes/audit`, `~/poc-authn`, `.bak` 삭제 | **apiserver 재기동 (약 40초)** | ✋✋ | 플래그 0개, 토큰 401 + 웹훅 로그 호출 없음, admin 정상, 호스트 잔존물 없음 |
| **P13** | 결과서 §16~ 작성 + 계획서 §6 산출물 4종 (캐시 TTL 결정서 / 웹훅 규격 초안 / 런북 완성 / 설정 변경 절차서) 를 결과서 부록으로, commit/push | git | — | 항목별 배경 소절 포함 |

**클러스터 전체 영향은 P8, P12 두 번(각 약 40초, 신규 API 요청만 실패)이고 나머지는 토큰 사용자(PoC 본인)에게만 영향.**

**P8 / P12 롤백 절차 (1차와 동일)**
```bash
sudo cp ~/kube-apiserver.yaml.bak /etc/kubernetes/manifests/kube-apiserver.yaml   # P8 실패 시
sudo crictl ps -a | grep kube-apiserver
kubectl get --raw /readyz
```
실패 판정: 적용 후 90초 내 `/readyz` 200 미복귀, 또는 apiserver Exited 반복.
P8 에서 apiserver 는 떴는데 토큰 경로만 401 이면 → apiserver 로그 `webhook.go` 확인 → `x509`/`tls` 면 웹훅을 `--client-cert-optional` 로 재기동 (apiserver 재기동 불필요).

## 6. B3 측정 설계 (★ 캐시 TTL 결정서의 근거)

**배경.** apiserver 에는 캐시가 두 겹이다.
1. 웹훅 인증기 자체 캐시: `--authentication-token-webhook-cache-ttl` (성공·실패 모두, 기본 2m)
2. 전역 토큰 성공 캐시 ≈ 10초 (`TokenSuccessCacheTTL`, 플래그 없음, 1차 발견 F1)

따라서 **취소 반영 지연의 하한은 TTL 과 무관하게 10초**이고, TTL 0s 라도 동일 토큰의 성공 요청은 10초에 1번만 웹훅에 간다. 반대로 **실패는 전역 캐시에 들어가지 않으므로** 무효 토큰을 반복 제시하면 매 요청 웹훅에 간다 (TTL 0s 일 때). 이 비대칭이 "TTL 0s 가 웹훅에 주는 실제 부하" 의 핵심이다.

| 측정 | 조건 | 방법 | 기록 |
|---|---|---|---|
| M1 취소→차단 | TTL 0s | `w5-measure-revoke.sh` 3회 (취소 직전 요청으로 캐시를 채운 상태에서) | 초 단위 3개 값. 예상 0~10 |
| M2 취소→차단 | TTL 2m | 동일 | 예상 10~120. 캐시 채운 시각 기준 |
| M3 성공 호출률 | TTL 0s / 2m | 유효 토큰 직렬 루프 30초, 병렬 5개 30초. 웹훅 로그 초당 건수 | 예상 0s: 토큰당 0.1/s, 2m: 거의 0 |
| M4 실패 호출률 | TTL 0s / 2m | **취소된 토큰**으로 같은 루프 | 예상 0s: 요청수 = 호출수, 2m: 첫 1건 후 0 (실패도 캐시) |
| M5 장기 연결 | TTL 0s | `kubectl get nodes -w` 중 취소 | watch 유지 여부, 종료 시점 |

M4 가 "TTL 0s + 무효 토큰 반복 = 웹훅 DoS 가능" 여부를 보여주므로 TTL 결정서에 반드시 포함.

## 7. 위험과 대응

| 위험 | 대응 |
|---|---|
| P2 전환으로 1차 토큰 kubeconfig 무효 | PoC 본인만 사용. 전환 전 1차 결과를 결과서에 이미 고정했음 |
| sqlite 동시 접근 (API 쓰기 + 웹훅 `last_used_at` 쓰기) | WAL + `busy_timeout=2000`. 웹훅은 `last_used_at` 쓰기 실패해도 인증 결과는 반환 (가용성 우선) |
| mTLS 적용 후 apiserver 가 웹훅 거부 → 토큰 전원 401 | P8 ① 에서 optional 로 먼저 기동해 apiserver 재기동 뒤 peer CN 을 눈으로 확인한 뒤에만 required 전환. 실패 시 웹훅만 optional 로 재기동 |
| webhook-kubeconfig 오타로 apiserver 기동 실패 | P8 ② 렌더 파일을 `kubectl --kubeconfig <렌더파일> version` 류로 파싱 검증 (apiserver 와 같은 kubeconfig 로더) + diff 승인 + 90초 룰 |
| `kill -STOP` 중 apiserver 가 웹훅 연결을 오래 점유 | F2 의 `timeout=30s` 로 상한. 측정 후 즉시 `CONT`. 측정은 1회 |
| 재기동 중 다른 작업자 영향 | P8 / P12 모두 사전 공지 + 사용자 지정 시각 |
| 롤백 후 잔존물 | P12 체크리스트로 호스트 7곳 + 클러스터 RBAC 확인. 증적은 tgz 1개로만 보관 |

## 8. 결과서 기록 형식

`test_result_webhook.md` §16 이후에 P 단계별로 **배경 → 절차 → 로그 → 판정 → 설계 시사점**. 끝에 2차 판정 요약표, 최종 상태(롤백 후), 부록 4종(캐시 TTL 결정서 / 웹훅 서버 규격 초안 / 운영 런북 / apiserver 설정 변경 절차서).
