# PoC 계획서 — LB write-back / ExternalDNS 자동화 검증

> 대상: 기보유 K8s PoC 장비 (F5 장비 없음)
> 작성: 2026-10 / 관련 설계 덱 `ManagedK8S_네트워킹모델_설계_r1_out.pptx` 11~13p

---

## 1. 목표와 범위

### 증명 대상
**Service / Ingress 생성 → VIP write-back → DNS 자동 등록** 전 구간 체인의 실동작.

### 정상 동작으로 가정 (검증 제외)
- F5 CIS 의 AS3 프로비저닝 / write-back
- BIG-IP 의 L4 포워딩 / TLS 종료 / 헬스체크
- BGP 기반 VIP 광고

### 대체 구성
| 운영 구성 | PoC 대체물 | 비고 |
|---|---|---|
| CIS (LB 컨트롤러) | **mock LB 컨트롤러** (자체 개발, 고정 IP 1개 응답) | 제어 동작만 모사 |
| BIG-IP (데이터플레인) | **없음 (검증 대상 아님)** | VIP 는 도달 불가 주소로 충분 |
| 사내 DNS | **BIND9** (권한 DNS) | rfc2136 동적 갱신 |
| NGINX IC | **F5 NIC OSS** 그대로 | BIG-IP 불필요, 무료 |

### 목표 3가지
- **G1** write-back → ExternalDNS → DNS 서버 체인 동작 확인 (**제어 평면 한정, 실트래픽 미흘림**)
- **G2** 멀티테넌시 통제 수단(`--domain-filter` / `--txt-owner-id`)의 실효성 확인
- **G3** 사내 DNS provider 결정 근거 확보 (rfc2136 가능 여부 / webhook 구현 난이도)

> **G2 / G3 는 당초 요청에 없던 추가분.** 사유는 §7 참조.

---

## 2. 착수 전 확인 사항

| 항목 | 요구 | 확인 명령 |
|---|---|---|
| K8s 버전 | **v1.24 이상** (`--subresource` 지원) | `kubectl version` |
| 노드 수 | 2 이상 권장 (`externalTrafficPolicy` 비교용) | `kubectl get nodes` |
| CNI | 무관 | `kubectl -n kube-system get pods` |
| 여유 IP 대역 | **불필요** — VIP 는 도달 불가 주소(예 `10.10.0.100`) 사용 | — |
| 이미지 pull | 외부 레지스트리 접근 또는 사내 미러 | `crictl pull registry.k8s.io/pause:3.9` |
| 도구 | `kubectl` / `helm` / `dig` | — |
| StorageClass | 불필요 (전부 ephemeral) | — |

**PoC 도메인**: `poc.internal` 사용 권장. `.internal` 은 ICANN 이 사설 용도로 유보한 TLD 이며, `.local` 은 mDNS 와 충돌.
테넌트 존은 `tenant-a.poc.internal` / `tenant-b.poc.internal`.

---

## 3. 전체 구성

```
┌─ K8s 클러스터 ──────────────────────────────────────────────────┐
│                                                                 │
│  [mock LB 컨트롤러]  ──write-back ①──▶ Service.status           │
│         (CIS 대체)                          │                   │
│                                             ▼                   │
│  [F5 NIC]  ──write-back ②──▶ Ingress.status                     │
│      │                             │                            │
│      │ L7 라우팅                    ▼                            │
│      ▼                      [ExternalDNS]                       │
│  [nginx 앱 Pod]                     │ rfc2136 + TSIG            │
│                                     ▼                            │
│                              [BIND9 Pod]  ◀── dig 로 검증        │
└─────────────────────────────────────────────────────────────────┘
```

**네임스페이스 분리**
| ns | 구성요소 |
|---|---|
| `poc-dns` | BIND9 |
| `poc-lb` | mock LB 컨트롤러 |
| `poc-edns` | ExternalDNS (테넌트별 2개) |
| `nginx-ingress` | F5 NIC |
| `tenant-a` / `tenant-b` | 앱 Pod, Service, Ingress |

---

## 4. 설치

### 4.1 DNS 서버 선정 — BIND9

| 후보 | ExternalDNS provider | 장점 | 단점 | 채택 |
|---|---|---|---|---|
| **BIND9** | `rfc2136` | 사내 DNS 최빈 구현체, 표준 동적 갱신, TSIG 인증 | 설정 파일 수기 | **주** |
| PowerDNS | `pdns` | REST API 로 상태 확인 용이, DB 백엔드 | 사내 도입 사례 상대적 희소 | 보조 |
| CoreDNS + etcd | `coredns` | 클러스터 내 기존 구성 재사용 | etcd 레코드 구조가 사내 DNS 와 이질적 | 미채택 |

**BIND9 채택 사유**: 사내 DNS 가 BIND 또는 Windows DNS 일 가능성이 높고, 양쪽 모두 **RFC2136 동적 갱신**을 지원. rfc2136 경로가 검증되면 **webhook provider 개발이 불필요**해지므로 개발량 판단에 직결.

**TSIG 키 생성**
```bash
tsig-keygen -a hmac-sha256 externaldns
# key "externaldns" { algorithm hmac-sha256; secret "AbCd...=="; };
```

**named.conf 요지**
```
key "externaldns" { algorithm hmac-sha256; secret "<SECRET>"; };

zone "tenant-a.poc.internal" {
    type master;
    file "/var/lib/bind/tenant-a.zone";
    allow-update { key "externaldns"; };
    allow-transfer { key "externaldns"; };   # ExternalDNS 가 AXFR 로 현재 상태 조회
};
zone "tenant-b.poc.internal" { ... 동일 ... };
```

**배포**: Deployment 1 replica + ConfigMap(named.conf, zone 파일) + Service(`53/UDP` + `53/TCP`).
`allow-transfer` 누락 시 ExternalDNS 가 기존 레코드를 읽지 못해 **매 루프마다 중복 생성 시도**가 발생하므로 필수.

### 4.2 ExternalDNS

이미지 `registry.k8s.io/external-dns/external-dns:v0.15.x` (webhook provider 는 v0.14 이상).
**테넌트당 1 인스턴스**로 배포 — 운영 구성(테넌트당 클러스터)을 한 클러스터 안에서 모사.

```
--source=service
--source=ingress
--provider=rfc2136
--rfc2136-host=<bind9 ClusterIP>
--rfc2136-port=53
--rfc2136-zone=tenant-a.poc.internal
--rfc2136-tsig-keyname=externaldns
--rfc2136-tsig-secret=<SECRET>
--rfc2136-tsig-secret-alg=hmac-sha256
--rfc2136-tsig-axfr
--domain-filter=tenant-a.poc.internal      # ★ T8 검증 대상
--txt-owner-id=tenant-a-cluster            # ★ T9 검증 대상
--registry=txt
--policy=sync                              # ★ T10 검증 대상
--interval=1m
--log-level=debug                          # PoC 기간 한정
--events
```

> 플래그 명칭은 버전별 차이가 있으므로 `external-dns --help` 로 1회 대조할 것.

**RBAC**: `services` / `endpoints` / `pods` / `nodes` / `ingresses` 에 `get,list,watch` (전부 읽기 전용).

### 4.3 F5 NIC (OSS)

```bash
helm repo add nginx-stable https://helm.nginx.com/stable
helm install nic nginx-stable/nginx-ingress -n nginx-ingress --create-namespace \
  --set controller.ingressClass.name=neocloud \
  --set controller.service.type=LoadBalancer \
  --set controller.service.loadBalancerClass=neocloud.poc/mock \
  --set controller.reportIngressStatus.enable=true \
  --set controller.enableCustomResources=false
```

- `controller.ingressClass.name` 은 차트 버전에 따라 `controller.ingressClass` 일 수 있음
- **`reportIngressStatus.enable=true` 가 write-back ②의 실체** — NIC 이 자기 Service 의 VIP 를 모든 Ingress 의 `status` 로 복사. 이 값이 ExternalDNS 의 입력
- `enableCustomResources=false` 로 VirtualServer CRD 비활성화 → **표준 Ingress 경로만 검증**

---

## 5. 개발 — mock LB 컨트롤러

### 5.1 사양

| 항목 | 내용 |
|---|---|
| 감시 대상 | `Service` 전체 네임스페이스 |
| 처리 조건 | `spec.type == LoadBalancer` **그리고** `spec.loadBalancerClass == "neocloud.poc/mock"` |
| 동작 | `status.loadBalancer.ingress[0].ip` 에 **고정 IP 1개** 기록 |
| 멱등성 | 이미 동일 값이면 패치 생략 |
| 삭제 | Service 삭제 시 별도 처리 불필요 (status 가 함께 소멸) |
| 구현 | Python + `kubernetes` 클라이언트 (약 40행) |

### 5.2 핵심 구현

**표준 라이브러리만 사용** — `pip install` 없이 `python:3.12-slim` 에서 바로 구동(폐쇄망 대응).
ServiceAccount 토큰과 CA 를 읽어 API 서버에 직접 호출.

```python
API   = "https://kubernetes.default.svc"
SA    = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN = open(f"{SA}/token").read().strip()
CTX   = ssl.create_default_context(cafile=f"{SA}/ca.crt")

def write_back(ns, name):
    body = {"status": {"loadBalancer": {"ingress": [{FIELD: VIP}]}}}
    call(f"/api/v1/namespaces/{ns}/services/{name}/status",     # ★ /status 경로
         "PATCH", body, "application/merge-patch+json").read()

while True:                                       # watch 는 300초마다 끊기므로 재접속
    with call("/api/v1/services?watch=true&timeoutSeconds=300") as stream:
        for line in stream:
            svc  = json.loads(line).get("object") or {}
            spec = svc.get("spec") or {}
            if spec.get("type") != "LoadBalancer":            continue
            if spec.get("loadBalancerClass") != LBCLS:        continue   # ★ 미지정이면 건너뜀
            cur = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
            if cur and cur[0].get(FIELD) == VIP:              continue   # 멱등
            write_back(svc["metadata"]["namespace"], svc["metadata"]["name"])
```

전문은 **`manifests/mock-lb.yaml`** 의 ConfigMap 에 포함.
`VIP_FIELD` 환경변수를 `hostname` 으로 바꾸면 **T14(CNAME 생성)** 를 코드 수정 없이 검증 가능.

### 5.3 주의 사항 (실패 유발 지점)

- **`status` 는 서브리소스.** 일반 `patch` 로는 기록되지 않음. Python 은 `patch_namespaced_service_status`, kubectl 은 `--subresource=status` 필요
  ```bash
  kubectl patch svc nic-nginx-ingress -n nginx-ingress --subresource=status --type=merge \
    -p '{"status":{"loadBalancer":{"ingress":[{"ip":"10.10.0.100"}]}}}'
  ```
- **RBAC 에 `services/status` 의 `patch` 포함 필요.** `services` 의 `update` 만으로는 403
- **`loadBalancerClass` 는 생성 시에만 설정 가능**(immutable). 나중에 못 바꾸므로 Service 생성 YAML 에 처음부터 명시. mock 이 무관한 Service 를 잡지 않도록 범위를 좁히는 용도
- 고정 IP 1개이므로 **모든 Service 가 같은 IP 를 받음.** Ingress 패턴에서는 정상(IC 1개가 VIP 1개 공유), 시나리오 B 검증 시에는 IP 중복을 감안

### 5.4 배포 매니페스트

전문: **`manifests/mock-lb.yaml`** (Namespace / ServiceAccount / ClusterRole / ClusterRoleBinding / ConfigMap / Deployment 6종 단일 파일)

```bash
kubectl apply -f manifests/mock-lb.yaml
kubectl -n poc-lb logs -f deploy/mock-lb
# [start] class=neocloud.poc/mock vip=10.10.0.100 field=ip
```

**RBAC — 이 블록이 핵심**
```yaml
rules:
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "watch"]      # spec 은 읽기만 (CIS 와 동일한 최소 권한)
  - apiGroups: [""]
    resources: ["services/status"]       # ★ 누락 시 403 Forbidden
    verbs: ["get", "patch", "update"]
```

**Service 측 필수 설정** — NIC 설치 시 `--set controller.service.loadBalancerClass=neocloud.poc/mock`
```yaml
spec:
  type: LoadBalancer
  loadBalancerClass: neocloud.poc/mock   # ★ 생성 후 변경 불가(immutable)
```

**동작 확인 (T3 / T4 합격 기준)**
```bash
kubectl get svc -n nginx-ingress -w
# NAME                TYPE           EXTERNAL-IP    ...
# nic-nginx-ingress   LoadBalancer   <pending>      ...   ← 적용 전
# nic-nginx-ingress   LoadBalancer   10.10.0.100    ...   ← write-back ① 성공
```

**증상별 원인**
| 증상 | 원인 | 조치 |
|---|---|---|
| 로그에 `403 Forbidden` | RBAC 에 `services/status` 누락 | ClusterRole 보정 |
| **로그에 아무것도 안 찍힘** | Service 에 `loadBalancerClass` 미지정 | Service 재생성(immutable) |
| `EXTERNAL-IP` 가 계속 `<pending>` | 위 둘 중 하나 | 로그부터 확인 |
| 같은 Service 에 write-back 반복 | 멱등 비교 누락 | `cur[0].get(FIELD)` 비교 확인 |

> `loadBalancerClass` 미지정 시 **로그가 전혀 남지 않는다**는 점에 주의. 컨트롤러가 조건문에서 걸러내므로 에러가 아니라 무반응으로 나타남.

---

## 6. 테스트 시나리오

### 필수 (G1 / G2)

| # | 항목 | 합격 기준 |
|---|---|---|
| **T0** | 환경 점검 | §2 전 항목 충족 |
| **T1** | BIND9 단독 동작 | `dig @<bind> SOA tenant-a.poc.internal` 응답 |
| **T2** | ExternalDNS ↔ BIND 연동 | 기동 로그에 AXFR 성공, 에러 0건 |
| **T3** | mock write-back ① | `kubectl get svc` 의 `EXTERNAL-IP` 가 `<pending>` → 고정 IP |
| **T4** | NIC 설치 + Service(type=LB) | NIC 의 Service 가 VIP 수령 |
| **T5** | NIC write-back ② | `kubectl get ingress` 의 `ADDRESS` 칸에 동일 VIP 표시 |
| **T6** | Ingress → DNS 등록 | `dig @<bind> web.tenant-a.poc.internal` 이 VIP 반환 |
| **T7** | 반영 지연 측정 | `apply` → `dig` 성공까지 **60초 이내** |
| **T8** | `--domain-filter` 경계 | `host: evil.tenant-b.poc.internal` 생성 시 **tenant-a ExternalDNS 가 미등록** |
| **T9** | `--txt-owner-id` 충돌 ★ | 아래 상세 |
| **T10** | 삭제 동기화 | Ingress 삭제 후 1분 내 A 레코드와 TXT 레코드 동시 소멸 |
| **T11** | write-back 누락 무증상 실패 ★ | 아래 상세 |
| **T12** | 와일드카드 | `*.tenant-a` 등록 후 앱 추가 시 **DNS 작업 0건으로 신규 URL 응답** |

### 부가 (G3 / 장비 요건 도출) — 전부 제어 평면, 실트래픽 불필요

| # | 항목 | 합격 기준 |
|---|---|---|
| **T13** | webhook provider 난이도 ★ | 최소 구현으로 레코드 1건 등록 성공, **소요 공수 기록** |
| **T14** | `status` 에 `hostname` 반환 시 동작 | mock 이 `ip` 대신 `hostname` 기록 → ExternalDNS 가 **A 가 아니라 CNAME** 생성 (설계 덱 6p 의 AWS 차이 실증) |
| **T15** | `ipMode` 필드 확인 ★ | 아래 상세 |
| **T16** | `healthCheckNodePort` | `externalTrafficPolicy: Local` 지정 시 포트 자동 할당, Pod 유무에 따라 노드에서 **200 / 503** |

### T9 상세 — txt-owner-id 충돌

**이번 PoC 에서 가장 가치가 큰 항목.** 운영 사고 재현 테스트.

1. tenant-a / tenant-b 두 ExternalDNS 를 **같은 `--txt-owner-id`** 로 기동 (의도적 오설정)
2. 양쪽 `--domain-filter` 를 공통 상위 존 `poc.internal` 로 넓힘
3. tenant-a 쪽 Ingress 를 생성해 레코드 등록 확인
4. tenant-b ExternalDNS 의 `--policy=sync` 루프가 **tenant-a 레코드를 삭제하는지** 관찰

**합격 기준**: 삭제가 재현되어야 함(= 위험 실증). 이후 owner-id 를 분리하면 삭제가 멈추는 것까지 확인.
**산출물**: 클러스터 프로비저닝 자동화에 UUID 주입이 필수임을 뒷받침하는 로그.

### T11 상세 — write-back 누락 무증상 실패

설계 덱 13p 의 운영 런북을 실증하는 항목.

1. mock LB 컨트롤러를 `scale --replicas=0`
2. Ingress 신규 생성
3. **ExternalDNS 로그 관찰** — 에러가 남는지, 아무 기록도 없는지
4. `kubectl get ingress` 의 ADDRESS 칸 상태 확인

**합격 기준**: ExternalDNS 에 **에러 로그가 남지 않고** DNS 에도 레코드가 생기지 않음.
**산출물**: "DNS 미생성 신고 시 ExternalDNS 로그가 아니라 ADDRESS 칸부터 확인" 런북의 근거.

### T13 상세 — webhook provider 난이도

사내 DNS 가 자체 시스템일 경우를 대비한 **개발량 실측**.

- 구현 범위: `GET /records` / `POST /records` / `POST /adjustendpoints` 3개 엔드포인트
- 저장소: 인메모리 dict (영속성 불필요)
- ExternalDNS 를 `--provider=webhook` 으로 전환하고 사이드카로 기동
- **측정값: 코드 라인 수, 소요 시간, 막힌 지점**

이 수치가 WBS 의 DNS 항목 공수 산정 근거가 됨.

### T15 상세 — `ipMode` 필드 (장비 도입 시 실제 영향)

`status.loadBalancer.ingress[].ipMode` 는 K8s 1.29 전후 도입된 필드로 **기본값이 `VIP`**.

| 값 | 클러스터 내부 Pod 가 VIP 로 접속할 때 |
|---|---|
| `VIP` (기본) | **kube-proxy 가 가로채 장비를 우회** — Pod 로 직결 |
| `Proxy` | 장비까지 나갔다 들어옴 |

**운영 영향**: 기본값 `VIP` 에서는 클러스터 내부에서 자기 VIP 를 호출하면 **BIG-IP 의 TLS 종료 / WAF / 로깅을 모두 건너뜀.** 외부 호출과 내부 호출의 동작이 달라짐.

- 확인 방법: mock 이 `ipMode` 를 명시하지 않았을 때 API 서버가 채우는 기본값 관찰, `Proxy` 로 지정 시 수용되는지 확인
- 산출물: **CIS 가 `ipMode` 를 어떻게 설정하는지** F5 에 확인할 질문 1건 (PoC 로는 설정 가능 여부까지만 확인 가능)

---

## 7. 당초 요청 대비 추가한 항목과 사유

| 추가 항목 | 사유 |
|---|---|
| **T5 (write-back ②)** | 원안은 write-back ①만 검증. **ExternalDNS 가 Ingress 에서 읽는 값은 ②의 결과**이므로 이 단계가 빠지면 체인이 끊긴 것을 모름 |
| **T8 / T9 (멀티테넌시)** | 설계상 "도메인 보안은 설정으로 달성"이라고 단정했으나 미검증. **보안 주장의 근거** |
| **T10 (삭제)** | 생성만 검증하면 운영 중 레코드 누적을 발견 못 함 |
| **T11 (무증상 실패)** | 운영 런북의 실증. 장애 대응 시간 단축에 직결 |
| **T12 (와일드카드)** | (a)+(d) 채택안의 핵심 이득을 미검증 상태로 두면 안 됨 |
| **T13 (webhook)** | 사내 DNS 가 자체 시스템이면 개발이 발생. **WBS 공수 산정의 최대 미지수** |
| **T14 (hostname/CNAME)** | 설계 덱 6p 의 "AWS 는 hostname, 당사는 IP" 대조를 코드 레벨로 실증 |
| **T15 (ipMode)** | 장비 도입 후 **내부 호출이 BIG-IP 을 우회**하는 문제. 설계 전 인지 필요 |

---

## 8. 산출물

1. **측정값 표** — T7 반영 지연, T15 webhook 공수, T14 헬스체크 동작
2. **provider 결정서** — rfc2136 가능 여부에 따른 개발량 분기
3. **운영 런북 초안** — T11 기반 장애 대응 절차
4. **장비 요건서 평가 항목** — `healthCheckNodePort` 대응, `Service.status` write-back 지원 여부를 **벤더 중립 문구**로 작성
5. **PoC 체크리스트 갱신** — 설계 덱 23p 의 8항목 중 CIS 의존 항목과 OSS 로 선검증 가능한 항목 구분

---

## 9. 본 PoC 로 확인할 수 없는 항목

| 항목 | 사유 | 대안 |
|---|---|---|
| CIS 의 `healthCheckNodePort` 자동 사용 | 장비 필요 | BIG-IP VE 평가판 |
| `f5-ipam-controller` IP 누수 | 장비 필요 | BIG-IP VE 평가판 |
| cert-manager 갱신 시 장비 인증서 교체 | 장비 필요 | BIG-IP VE 평가판 |
| rSeries 테넌트 생성 소요 시간 (K01) | 실장비 필요 | F5 PoC 요청 |
| CIS 의 Gateway API 지원 | 장비 필요 | F5 로드맵 확인 |
| BIG-IP Next 전환 시 CIS 대체 수단 | 제품 로드맵 | F5 직접 확인 |

> **BIG-IP VE 평가판을 병행하면 위 3건이 본 PoC 안으로 들어옴.** 단 본 PoC 의 주목적(ExternalDNS 연동)과는 별개 트랙이므로, **T0~T12 완료 후 별도 판단**할 것.

---

## 10. 진행 순서 제안

```
1일차   T0 ~ T2    환경 점검, BIND9, ExternalDNS 연동
2일차   T3 ~ T7    mock 컨트롤러 개발, NIC, 기본 체인 + 지연 측정
3일차   T8 ~ T12   멀티테넌시 통제, 삭제, 무증상 실패, 와일드카드
4일차   T13 ~ T16  (부가) webhook 난이도, CNAME, ipMode, healthCheckNodePort
```

각 단계는 독립적이므로 중단 시에도 **T7 까지만 완료되면 G1 은 달성**.
**실트래픽 검증은 범위 밖** — VIP 는 도달 불가 주소로 두고 제어 평면 동작만 확인.
