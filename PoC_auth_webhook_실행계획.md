# 실행 계획 — kubeconfig 토큰 인증 웹훅 PoC (1차)

> 대상: gasandevcontrol (mgmt01 컨트롤 플레인 단일 + dev001, K8s v1.35.1, kubespray 계열 레이아웃)
> 상위 계획서: `PoC_auth_webook_plan.md` / 선행 사례: `PoC_ExternalDNS_계획서.md`, `test_result.md`
> 작성: 2026-10-12

---

## 0. 사전 확인 결과 (2026-10-12, 읽기 전용)

| 항목 | 확인값 | 의미 |
|---|---|---|
| 웹훅 슬롯 | `--authentication-token-webhook-*` 없음 | 비어 있음 (N1 해당 없음) |
| 감사 로그 | `--audit-*` 없음, `/var/log/kubernetes` 없음 | A9 위해 **감사 정책 + 로그 설정 신규 추가** |
| 인증서 경로 | `--client-ca-file=/etc/kubernetes/ssl/ca.crt`, admin.conf = 인증서 | 복구 경로 |
| audience | `--api-audiences` 없음 → issuer `https://kubernetes.default.svc.cluster.local` 가 암묵 audience | 웹훅은 `spec.audiences` 를 그대로 echo |
| apiserver Pod | `hostNetwork: true`, `--secure-port=6443`, 이미지 `registry.k8s.io/kube-apiserver:v1.35.1` | `127.0.0.1:8443` 웹훅 도달 가능 |
| 익명 접근 | `--anonymous-auth=True` | 무효 토큰 제시 시에는 그래도 401 (A0 기준선 유효) |
| 호스트 도구 | python3 3.12 / openssl / crictl / docker | 웹훅 서버 = python 표준 라이브러리 |
| 공유 사용 | `litellm`(4시간 전 생성), `inferium`, `team-b`, `dynamo-system` 등 활성 | **재기동 전 사전 공지 필수** |

## 1. 확정한 결정

| 결정 | 내용 | 사유 |
|---|---|---|
| 감사 로그 | 웹훅 플래그와 **같은 매니페스트 수정 1회**에 포함 | 공유 클러스터 → 재기동 횟수 최소화 |
| 롤백(A10) | **2차 끝으로 미룸.** 1차 종료 후에도 웹훅 설정 유지 | 재기동 2회 절감. 그 사이 웹훅 서버 상시 기동 유지 |
| 웹훅 실행 | mgmt01 호스트 `nohup` 수동 실행 (systemd 미사용) | 시스템 파일 변경 최소화. **재부팅/세션 종료 시 수동 재기동 필요** |
| 캐시 TTL | `0s` | 1차는 매 요청 웹훅 호출 확인이 목적 |
| 산출물 위치 | `authn/` 폴더 + `test_result_authn.md` | ExternalDNS 산출물과 분리 |
| 코드 전달 | 로컬 commit/push → mgmt01 `~/lbcontroller` git pull | 기존 방식 유지 |

## 2. 변경 범위 (호스트/클러스터에 남는 것)

| 위치 | 내용 | 되돌리기 |
|---|---|---|
| `/etc/kubernetes/webhook/` | `tls.key` `tls.crt` `ca.crt` `webhook-kubeconfig.yaml` `audit-policy.yaml` | 디렉터리 삭제 |
| `/var/log/kubernetes/audit/` | `audit.log` (maxsize 50MB, backup 3, age 3일) | 디렉터리 삭제 |
| `/etc/kubernetes/manifests/kube-apiserver.yaml` | 플래그 8개 + volume/mount 2쌍 | `~/kube-apiserver.yaml.bak` 복원 |
| `~/kube-apiserver.yaml.bak` | 원본 백업 (매니페스트 디렉터리 **밖**) | 2차 종료 후 삭제 |
| `~/poc-authn/` | 웹훅 로그, 토큰 kubeconfig | 삭제 |
| 클러스터 | ClusterRole + ClusterRoleBinding `poc-authn-node-reader` (group `poc-tenant-a` → nodes get/list/watch) | `kubectl delete` |

**apiserver 추가 플래그**
```yaml
- --authentication-token-webhook-config-file=/etc/kubernetes/webhook/webhook-kubeconfig.yaml
- --authentication-token-webhook-version=v1
- --authentication-token-webhook-cache-ttl=0s
- --audit-policy-file=/etc/kubernetes/webhook/audit-policy.yaml
- --audit-log-path=/var/log/kubernetes/audit/audit.log
- --audit-log-maxage=3
- --audit-log-maxbackup=3
- --audit-log-maxsize=50
```
```yaml
volumeMounts:
- { name: poc-webhook, mountPath: /etc/kubernetes/webhook, readOnly: true }
- { name: poc-audit,   mountPath: /var/log/kubernetes/audit }
volumes:
- { name: poc-webhook, hostPath: { path: /etc/kubernetes/webhook, type: Directory } }
- { name: poc-audit,   hostPath: { path: /var/log/kubernetes/audit, type: DirectoryOrCreate } }
```

**감사 정책 (최소)** — poc 신원만 Metadata, 나머지 None
```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: ["RequestReceived"]
rules:
- level: Metadata
  users: ["poc-user"]
- level: Metadata
  userGroups: ["poc-tenant-a", "poc-tenant-b"]
- level: None
```
> 인증 실패(401) 이벤트는 user 가 비어 있어 위 규칙에 걸리지 않는다. 실패 추적(B9)은 **웹훅 서버 자체 로그**로 커버하고, 필요 시 2차에서 정책 확장.

## 3. 웹훅 서버 (`authn/webhook_server.py`)

- python 표준 라이브러리만 (`http.server`, `ssl`, `json`, `argparse`, `logging`)
- 인자: `--port 8443` `--cert` `--key` `--username poc-user` `--uid poc-1` `--groups poc-tenant-a` `--log ~/poc-authn/webhook.log`
- `POST /authenticate` 만 처리. 그 외 404
- 1차 동작: 토큰이 비어 있지 않으면 **무조건** `authenticated: true`. `status.audiences` 는 **`spec.audiences` 를 그대로 echo** (apiserver 의 audience 검증 통과용)
- 로그 1행: 시각 / 원격주소 / 토큰 앞 8자 / 응답 username / groups
- `GET /healthz` 200 (A8 재기동 확인용)
- 2차 확장 지점: 토큰 검증 함수 1개 교체 (해시 조회, 만료, 취소, audience)

보조 파일
| 파일 | 역할 |
|---|---|
| `authn/webhook-kubeconfig.yaml` | apiserver → 웹훅 접속 설정 (의미 반전 kubeconfig) |
| `authn/audit-policy.yaml` | 위 감사 정책 |
| `authn/scripts/w1-setup-webhook.sh` | 인증서 생성, `/etc/kubernetes/webhook` 구성, 파일 배치 (sudo) |
| `authn/scripts/w1-run-webhook.sh` | nohup 기동 / 중지 / 상태 |
| `authn/scripts/w3-make-token-kubeconfig.sh` | admin.conf 에서 CA 추출해 토큰 kubeconfig 생성 |
| `authn/scripts/w2-render-apiserver.py` | 원본 매니페스트를 읽어 플래그/볼륨을 삽입한 **새 파일** 생성 (원본 미수정) + YAML 파싱 검증 |
| `authn/scripts/w2-watch-readyz.sh` | 적용 전후 `/readyz` 1초 폴링으로 다운타임 측정 |
| `authn/rbac/poc-authn-node-reader.yaml` | A5 ClusterRole(nodes get/list/watch) + ClusterRoleBinding(group poc-tenant-a). `view` 는 nodes 권한이 없어 대체 |

## 4. 단계와 체크포인트

✋ = **실행 직전 승인 요청**. 그 외는 로컬 작업이거나 읽기 전용이라 바로 진행 후 보고.

| 단계 | 내용 | 변경 대상 | 승인 | 합격 기준 |
|---|---|---|---|---|
| **S0** | 사전 공지 확인, 백업 `cp /etc/kubernetes/manifests/kube-apiserver.yaml ~/kube-apiserver.yaml.bak`, SSH 세션 2개 확보(작업/감시) | 홈 디렉터리 | ✋ 공지 완료 확인 | 백업 파일 존재, diff 없음 |
| **S1** | 웹훅 서버 + 스크립트 + 정책 파일 작성, 로컬에서 자가 시험(`curl` TokenReview), commit/push, mgmt01 pull | git 만 | — | 로컬 curl 응답 `authenticated: true` |
| **S2** | `w1-setup-webhook.sh`: 인증서 생성, `/etc/kubernetes/webhook/` 구성. `w1-run-webhook.sh start` 로 nohup 기동. mgmt01 에서 curl 자가 시험 | 호스트 파일 | ✋ | `/healthz` 200, TokenReview 응답 정상, 로그 1행 |
| **S3 = A0** | `w3-make-token-kubeconfig.sh` → `~/poc-authn/token.kubeconfig`. `kubectl --kubeconfig ... get nodes` | 홈 디렉터리 | — | **401 Unauthorized**. 웹훅 로그 호출 없음 |
| **S4** | `w2-render-apiserver.py` 로 새 매니페스트 **별도 파일** 생성. 사전 검증 4종: YAML 파싱 / 플래그명 존재(`kube-apiserver --help` 대조) / 참조 파일 존재 / 웹훅 응답. **diff 를 보여 드림** | 없음 (파일 생성만) | ✋✋ **적용 승인** | diff 가 플래그 8개 + 볼륨 2쌍 뿐 |
| **S5 = A1, A2** | 감시 세션에서 `w2-watch-readyz.sh` 시작 → 작업 세션에서 `cp` 적용 → kubelet 재기동 대기 → `crictl ps` / `/readyz` / admin `get nodes` | **apiserver** | (S4 승인에 포함) | crash-loop 없음, **다운타임 초 기록**, admin 경로 정상, 웹훅 로그 호출 없음 |
| **S6 = A3, A4** | 토큰 kubeconfig 로 `get nodes` | 없음 | — | **403 + `User "poc-user"`** ★, 웹훅 로그에 토큰 앞 8자 일치 |
| **S7 = A5** | `kubectl apply -f authn/rbac/poc-authn-node-reader.yaml` | 클러스터 RBAC | ✋ | `get nodes` **200**, `get pods -A` 는 여전히 403 |
| **S8 = A6** | 웹훅을 `--groups poc-tenant-b` 로 재기동 → 재시도 → 원복 | 웹훅 프로세스 | — | 403 복귀 → 원복 후 200 |
| **S9 = A7** | 기존 `poc-lb/mock-lb` Pod 에서 SA 토큰으로 in-cluster API 호출 | 없음 | — | 정상, 웹훅 로그 호출 없음 |
| **S10 = A8** | 웹훅 `stop` → 토큰/admin/SA 세 경로 시도 → `start` | 웹훅 프로세스 | ✋ | 토큰만 401, admin/SA 정상, 재기동 후 복귀. apiserver 측 에러 로그 문구 기록 |
| **S11 = A9** | `/var/log/kubernetes/audit/audit.log` 에서 `poc-user` 이벤트 확인 | 없음 | — | `user.username=poc-user`, `userAgent=kubectl`, 403/200 코드 기록 |
| **S12** | `test_result_authn.md` 작성, commit/push | git | — | ExternalDNS 결과서와 같은 형식 |
| ~~A10~~ | 롤백은 **2차 종료 후** 수행 (결정 §1) | | | |

**S4~S5 롤백 절차 (실패 시 즉시)**
```bash
sudo cp ~/kube-apiserver.yaml.bak /etc/kubernetes/manifests/kube-apiserver.yaml
sudo crictl ps -a | grep kube-apiserver        # 10~30초 후 새 컨테이너 Running
kubectl get --raw /readyz
```
실패 판정 기준: 적용 후 **90초** 내 `/readyz` 200 미복귀, 또는 `crictl ps -a` 에 apiserver Exited 반복.
로그 확인: `sudo crictl logs $(sudo crictl ps -a --name kube-apiserver -q | head -1)`.

## 5. 위험과 대응

| 위험 | 대응 |
|---|---|
| 매니페스트 오타 → apiserver crash-loop → 전 사용자 API 중단 | S4 사전 검증 4종 + diff 승인 + 백업 복원 90초 룰 |
| volume 마운트 누락 (가장 흔한 실패) | 렌더 스크립트가 플래그와 볼륨을 **한 쌍으로** 삽입. 참조 파일 존재 검사 |
| 웹훅 인증서 SAN 불일치 → apiserver 가 웹훅 거부 | SAN `IP:127.0.0.1` 명시. S2 에서 `curl --cacert ca.crt https://127.0.0.1:8443` 로 사전 확인 |
| audience 불일치로 authenticated 인데도 401 | `status.audiences` echo 로 회피. 실패 시 apiserver 로그 `audiences` 문구 확인 |
| nohup 프로세스 사망 → 토큰 경로만 401 | 영향은 토큰 사용자(PoC 본인)뿐. `w1-run-webhook.sh status` 로 확인 후 재기동 |
| kubespray 재실행 시 매니페스트 원복 | 운영 시사점으로 결과서에 기록 (N2 와 연결) |
| 다른 작업자 영향 | S0 공지. 재기동은 사용자가 지정한 시각에만 |

## 6. 2차 개요 (별도 계획으로 상세화)

- `authn/token_api.py`: sqlite + 표준 라이브러리. `POST/GET/DELETE /tokens`, `GET /clusters/{id}/kubeconfig`
- 웹훅 서버의 검증 함수를 해시 조회로 교체 (`issued_via`, `expires_at`, `revoked_at`, `last_used_at`, audience)
- B3 캐시 TTL 측정은 `0s` → `2m` 변경 재기동 1회 포함 (총 재기동: 2차 1회 + 최종 롤백 1회)
- B5 audience 바인딩은 단일 클러스터라 **토큰의 cluster 필드 vs 웹훅 설정 클러스터 ID** 비교로 모사
- 종료 시 A10 롤백 + `/etc/kubernetes/webhook`, 감사 로그, 백업 정리

## 7. 결과서 기록 형식

`test_result_authn.md` — 항목별 §: 절차 / 시간순 로그 / 판정 / 설계 시사점. 마지막에 판정 요약표, 클러스터 잔존 상태, 산출물 위치. (`test_result.md` 와 동일 구조)
