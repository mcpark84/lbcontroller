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
