# PoC 시험 결과 — LB write-back / ExternalDNS 체인

> 대상 클러스터: gasandevcontrol (mgmt01 + dev001, K8s v1.35.1)
> 시험일: 2026-10-07 / 관련 계획서 `PoC_ExternalDNS_계획서.md`
> 범위: T1(BIND9), T2(ExternalDNS 연동), T3(mock write-back), 시나리오 B(Service 어노테이션 → DNS 등록/삭제)

---

## 1. 구성요소 기동 상태

| 네임스페이스 | Pod | 이미지 | 노드 | 상태 | 기동 시각(UTC) |
|---|---|---|---|---|---|
| poc-dns | bind9-7c4bf59967-q9dxf | mgmt01:5000/poc/bind9:9.18 | dev001 | Running 1/1, RESTARTS 0 | 06:21:28 |
| poc-edns | external-dns-tenant-a-657c488864-6pc75 | mgmt01:5000/poc/external-dns:v0.15.1 | mgmt01 | Running 1/1, RESTARTS 0 | 07:03:07 |
| poc-edns | external-dns-tenant-b-9b4d8846b-zbs6z | mgmt01:5000/poc/external-dns:v0.15.1 | mgmt01 | Running 1/1, RESTARTS 0 | 07:03:07 |
| poc-lb | mock-lb-c5664cdc6-slk7w | mgmt01:5000/poc/python:3.12-slim | mgmt01 | Running 1/1, RESTARTS 0 | 08:12:23 |

- BIND9 Service: `bind9.poc-dns.svc` ClusterIP `10.233.18.38`, 53/UDP + 53/TCP
- ExternalDNS 주요 플래그: `--source=service,ingress` / `--provider=rfc2136` / `--rfc2136-tsig-axfr` / `--domain-filter=tenant-{a,b}.poc.internal` / `--txt-owner-id=tenant-{a,b}-cluster` / `--registry=txt` / `--policy=sync` / `--interval=1m` / `--events`
- mock LB 컨트롤러 기동 로그: `[start] class=neocloud.poc/mock vip=10.10.0.100 field=ip`
- 이미지는 전부 사설 레지스트리 `mgmt01:5000/poc/*` 경유 (dev001 노드가 외부 레지스트리 접근 불가)

**T1 / T2 합격 근거 (요약)**

| 항목 | 결과 |
|---|---|
| T1 `dig @10.233.18.38 SOA tenant-a.poc.internal` | NOERROR, serial 2026100701 (tenant-b 동일) |
| T1 TSIG 없는 AXFR | `Transfer failed` 로 거부 (정상) |
| T2 ExternalDNS 기동 로그 | `Configured RFC2136 with zone '[tenant-a.poc.internal]' and nameserver 'bind9.poc-dns.svc.cluster.local:53'` |
| T2 BIND 측 AXFR 로그 | 두 인스턴스 모두 `AXFR started / ended ... 4 records` |
| T2 level=error 건수 | tenant-a 0 / tenant-b 0 |

---

## 2. 시험용 Service 생성

type=LoadBalancer, `loadBalancerClass` 지정, ExternalDNS 호스트명 어노테이션 추가.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: svc-dns-probe
  namespace: poc-lb
  annotations:
    external-dns.alpha.kubernetes.io/hostname: svcprobe.tenant-a.poc.internal
spec:
  type: LoadBalancer
  loadBalancerClass: neocloud.poc/mock       # mock 컨트롤러 처리 조건 (immutable)
  selector: { app: none }                    # 백엔드 Pod 없음 — 제어 평면만 검증
  ports:
    - { port: 80, targetPort: 80 }
```

생성 시각: `2026-10-07T09:04:19Z`

---

## 3. 결과

### 3.1 write-back ① — Service.status 에 VIP 기록

생성 1초 후:

```
NAME            TYPE           CLUSTER-IP     EXTERNAL-IP   PORT(S)        AGE
svc-dns-probe   LoadBalancer   10.233.5.168   10.10.0.100   80:32739/TCP   1s
```

`status` 원문:
```json
{"loadBalancer":{"ingress":[{"ip":"10.10.0.100","ipMode":"VIP"}]}}
```

mock 컨트롤러 로그:
```
[write-back] poc-lb/svc-dns-probe -> ip=10.10.0.100
```

- `<pending>` 상태가 관측되지 않을 만큼 즉시 기록됨 (watch 이벤트 기반)
- `ipMode: VIP` 는 mock 이 쓰지 않은 값으로, API 서버가 채운 기본값 (T15 선행 확인)
- 동일 Service 에 대한 write-back 은 1회 (멱등). 로그에 2줄 보이는 것은 08:57 수동 시험 1회 + 09:04 본 시험 1회

### 3.2 DNS 전파 — ExternalDNS → BIND9 (rfc2136)

**반영 지연: 생성 후 7초** (`--events` 로 watch 트리거, 1분 interval 을 기다리지 않음)

ExternalDNS tenant-a 로그 (09:04:24):
```
ApplyChanges (Create: 3, UpdateOld: 0, UpdateNew: 0, Delete: 0)
Adding RR: svcprobe.tenant-a.poc.internal 60 A 10.10.0.100
Adding RR: svcprobe.tenant-a.poc.internal 60 TXT "heritage=external-dns,external-dns/owner=tenant-a-cluster,external-dns/resource=service/poc-lb/svc-dns-probe"
Adding RR: a-svcprobe.tenant-a.poc.internal 60 TXT "heritage=external-dns,external-dns/owner=tenant-a-cluster,external-dns/resource=service/poc-lb/svc-dns-probe"
```

BIND9 로그 (TSIG 서명 승인):
```
09:04:24.710 client 10.233.91.104#52220/key externaldns: signer "externaldns" approved
```

`dig` 질의 결과:
```
$ dig @10.233.18.38 svcprobe.tenant-a.poc.internal
;; status: NOERROR
svcprobe.tenant-a.poc.internal.  60 IN A   10.10.0.100

$ dig @10.233.18.38 TXT svcprobe.tenant-a.poc.internal +short
"heritage=external-dns,external-dns/owner=tenant-a-cluster,external-dns/resource=service/poc-lb/svc-dns-probe"
```

존 전체(AXFR) — 등록 직후:
```
tenant-a.poc.internal.             60 IN SOA  ns1.tenant-a.poc.internal. admin.poc.internal. 2026100706 ...
tenant-a.poc.internal.             60 IN NS   ns1.tenant-a.poc.internal.
ns1.tenant-a.poc.internal.         60 IN A    10.10.0.53        ← 존 파일 초기값(자리 채우기)
svcprobe.tenant-a.poc.internal.    60 IN A    10.10.0.100       ← ★ ExternalDNS 생성
svcprobe.tenant-a.poc.internal.    60 IN TXT  "heritage=external-dns,...owner=tenant-a-cluster,..."
a-svcprobe.tenant-a.poc.internal.  60 IN TXT  "heritage=external-dns,...owner=tenant-a-cluster,..."
```

- A 레코드 1건 + 소유권 TXT 2건(신형식 `svcprobe`, 구형식 `a-svcprobe`) 생성. SOA serial 증가(2026100701 → 2026100706)로 동적 갱신 확인
- TXT 의 `owner=tenant-a-cluster` 가 `--txt-owner-id` 값. T9 에서 충돌시킬 대상

### 3.3 멀티테넌시 경계 (T8 선행 확인)

tenant-b ExternalDNS 도 같은 Service 를 읽었으나 도메인 필터로 무시:
```
Endpoints generated from service: poc-lb/svc-dns-probe: [svcprobe.tenant-a.poc.internal 0 IN A 10.10.0.100 []]
ignoring record svcprobe.tenant-a.poc.internal that does not match domain filter
```
tenant-b 존에는 아무 변화 없음.

### 3.4 삭제 동기화 (T10 선행 확인)

Service 삭제(09:04:27) 후 **6초** 만에 A / TXT 레코드 동시 소멸:
```
Removing RR: svcprobe.tenant-a.poc.internal 0 TXT "..."
Removing RR: a-svcprobe.tenant-a.poc.internal 0 TXT "..."
$ dig @10.233.18.38 svcprobe.tenant-a.poc.internal +short   → (빈 응답)
$ dig @10.233.18.38 TXT svcprobe.tenant-a.poc.internal +short → (빈 응답)
```

---

## 4. 판정

| # | 항목 | 합격 기준 | 결과 | 판정 |
|---|---|---|---|---|
| T1 | BIND9 단독 동작 | `dig SOA` 응답 | NOERROR | **합격** |
| T2 | ExternalDNS ↔ BIND 연동 | AXFR 성공, 에러 0건 | 양쪽 AXFR 성공, 에러 0 | **합격** |
| T3 | mock write-back ① | EXTERNAL-IP `<pending>` → 고정 IP | 10.10.0.100 즉시 기록 | **합격** |
| (B) | Service 어노테이션 → DNS 등록 | `dig` 가 VIP 반환 | 7초 후 A=10.10.0.100 | **합격** (T6 의 Service 경로 버전) |
| T7 선행 | 반영 지연 | 60초 이내 | 생성 7초 / 삭제 6초 | 기준 충족 |
| T8 선행 | domain-filter 경계 | 타 테넌트 미등록 | tenant-b "ignoring ... domain filter" | 기준 충족 |
| T10 선행 | 삭제 동기화 | 1분 내 A+TXT 소멸 | 6초 | 기준 충족 |

**G1(제어 평면 체인) 은 Service 경로에서 달성.** Ingress 경로(NIC write-back ② → T5/T6)는 미실시.

---

## 5. 비고 / 운영 시사점

- **`--events` 효과.** 계획서는 `--interval=1m` 기준 60초 이내를 목표로 했으나 watch 트리거로 10초 미만 반영. 운영에서 `--events` 를 끄면 최대 interval 만큼 지연됨.
- **`loadBalancerClass` 미지정 Service 는 mock 이 무시**(T3 대조군 `t3-probe-noclass` 로 확인). 로그도 남지 않음. 실제 CIS 는 `cis.f5.com/ipamLabel` 또는 `cis.f5.com/ip` 어노테이션까지 요구하므로 운영에서는 "어노테이션 누락 → pending → DNS 미생성"의 무증상 실패 경로가 하나 더 있음 (T11 런북에 반영 필요).
- **ExternalDNS 는 자기 소유가 아닌 레코드를 건드리지 않음.** 존 초기 레코드 `ns1 A 10.10.0.53` 에 대해 `Skipping endpoint ... owner id does not match` 로그. `--policy=sync` 이지만 기존 사내 DNS 레코드가 삭제되지 않는다는 근거. 단 owner-id 가 겹치면 이 보호가 깨짐 (T9 에서 재현 예정).
- **Service 경로는 Service 마다 VIP 1개 소모.** mock 은 고정 IP 1개라 모든 Service 가 같은 IP 를 받지만, 실제 CIS/IPAM 환경에서는 Service 수만큼 IP 가 필요. Ingress 패턴(VIP 1개 공유)과의 핵심 차이.
- 시험용 Service 는 전부 삭제 완료. 현재 poc-lb 에 Service 없음.

## 6. 재현 명령

```bash
BIND=$(kubectl -n poc-dns get svc bind9 -o jsonpath='{.spec.clusterIP}')
kubectl -n poc-lb create -f - <<'Y'
apiVersion: v1
kind: Service
metadata:
  name: svc-dns-probe
  annotations:
    external-dns.alpha.kubernetes.io/hostname: svcprobe.tenant-a.poc.internal
spec:
  type: LoadBalancer
  loadBalancerClass: neocloud.poc/mock
  selector: { app: none }
  ports: [{ port: 80, targetPort: 80 }]
Y
kubectl -n poc-lb get svc svc-dns-probe                               # EXTERNAL-IP 10.10.0.100
kubectl -n poc-lb logs deploy/mock-lb | grep svc-dns-probe            # [write-back]
sleep 10; dig @$BIND svcprobe.tenant-a.poc.internal +short            # 10.10.0.100
dig @$BIND TXT svcprobe.tenant-a.poc.internal +short                  # owner TXT
kubectl -n poc-edns logs deploy/external-dns-tenant-a | grep "Adding RR"
kubectl -n poc-lb delete svc svc-dns-probe
sleep 10; dig @$BIND svcprobe.tenant-a.poc.internal +short            # (빈 응답)
```

---
---

# 2차 시험 — Ingress 경로 (T4 / T5 / T6 / T7)

> 시험일: 2026-10-07 09:08 ~ 09:33 UTC
> 추가 구성요소: F5 NGINX Ingress Controller OSS 5.6.3 (helm chart nginx-stable/nginx-ingress 2.7.3)

## 7. T4 — NIC 설치 + Service(type=LB) VIP 수령

### 7.1 설치 내용

- helm release `nic`, 네임스페이스 `nginx-ingress`, 차트 tgz 는 서버 `~/charts/nginx-ingress-2.7.3.tgz` 에 보관(오프라인 재설치용)
- values: `manifests/nic-values.yaml`, 설치 스크립트: `scripts/t4-install-nic.sh`

| values 키 | 값 | 목적 |
|---|---|---|
| controller.image.repository / tag | `mgmt01:5000/poc/nginx-ingress` / `5.6.3` | 사설 레지스트리 경유 |
| controller.ingressClass.name | `neocloud` (setAsDefaultIngress=false) | 테넌트 Ingress 가 명시적으로 선택 |
| controller.service.type / loadBalancerClass | `LoadBalancer` / `neocloud.poc/mock` | mock 컨트롤러 처리 조건 |
| controller.service.externalTrafficPolicy | `Local` (차트 기본값) | healthCheckNodePort 자동 할당 (T16) |
| controller.reportIngressStatus.enable | `true` | **write-back ② 의 실체** |
| controller.enableCustomResources | `false` + `--skip-crds` | VirtualServer CRD 미설치, 표준 Ingress 만 검증 |

### 7.2 결과

```
NAME                                                READY   STATUS    NODE
pod/nic-nginx-ingress-controller-5b4578c9bd-6rjkh   1/1     Running   mgmt01

NAME                                   TYPE           CLUSTER-IP      EXTERNAL-IP   PORT(S)
service/nic-nginx-ingress-controller   LoadBalancer   10.233.23.245   10.10.0.100   80:31048/TCP,443:32288/TCP

NAME       CONTROLLER                     PARAMETERS   AGE
neocloud   nginx.org/ingress-controller   <none>       3s
```

- NIC Service status: `{"loadBalancer":{"ingress":[{"ip":"10.10.0.100","ipMode":"VIP"}]}}`, `loadBalancerClass=neocloud.poc/mock`, `healthCheckNodePort=32066`
- mock-lb 로그: `[write-back] nginx-ingress/nic-nginx-ingress-controller -> ip=10.10.0.100` (1회)
- NIC 기동 로그: `NGINX Ingress Controller Version=5.6.3 ... Kubernetes version: 1.35.1`, 에러 없음
- 클러스터 내 nginx 관련 CRD: 0개

**판정: 합격** — 설치 3초 시점에 EXTERNAL-IP 수령.

> 참고: 설치한 것은 F5(NGINX Inc.) 의 NGINX Ingress Controller **OSS 에디션**이며, 커뮤니티 Ingress-NGINX(`k8s.io/ingress-nginx`)와 다른 제품. IngressClass 컨트롤러 값 `nginx.org/ingress-controller` 가 식별자. OSS/Plus 차이는 데이터플레인 기능이라 본 PoC 검증 항목(reportIngressStatus, 표준 Ingress 처리)에는 영향 없음.

## 8. T5 / T6 / T7 — Ingress 생성 → write-back ② → DNS 등록 + 지연 측정

### 8.1 적용 매니페스트 (`manifests/tenant-a-web.yaml`)

- Namespace `tenant-a`
- Deployment `web` (`mgmt01:5000/poc/nginx:1.28.0-alpine`, 1 replica)
- Service `web` **ClusterIP** (앱 Service 는 VIP 를 소모하지 않음, NIC Service 1개가 VIP 공유)
- Ingress `web`: `ingressClassName: neocloud`, `host: web.tenant-a.poc.internal`, `/ → web:80`

apply 시각: `2026-10-07T09:32:44.354Z`

### 8.2 결과 — 시간순

| 경과 | 단계 | 근거 |
|---|---|---|
| +0.1s | NIC 이 Ingress 감지 | NIC 로그 `resource_kind=Ingress resource_name=web ... AddedOrUpdated` |
| +0.3s | **write-back ② 완료** | NIC 로그 `status.go:169 ... updated status for ing: tenant-a web` |
| +1.2s | `kubectl get ingress` ADDRESS 표시 | `10.10.0.100` |
| +5s | ExternalDNS 가 Ingress 읽음 | `Endpoints generated from ingress: tenant-a/web: [web.tenant-a.poc.internal 0 IN A 10.10.0.100 []]` |
| +5s | rfc2136 갱신 | `Adding RR: web.tenant-a.poc.internal 60 A 10.10.0.100` + TXT 2건 |
| **+6.4s** | **`dig` 성공 (T7 측정값)** | `dig @10.233.18.38 web.tenant-a.poc.internal +short → 10.10.0.100` |

```
$ kubectl -n tenant-a get ingress
NAME   CLASS      HOSTS                       ADDRESS       PORTS   AGE
web    neocloud   web.tenant-a.poc.internal   10.10.0.100   80      6s

$ kubectl -n tenant-a get ingress web -o jsonpath='{.status}'
{"loadBalancer":{"ingress":[{"ip":"10.10.0.100"}]}}

$ dig @10.233.18.38 TXT web.tenant-a.poc.internal +short
"heritage=external-dns,external-dns/owner=tenant-a-cluster,external-dns/resource=ingress/tenant-a/web"
```

- 앱 Pod `web-5bdd85699f-9rmt5` Running 1/1 (mgmt01)
- NIC 로그의 `Error retrieving endpoints for the service web: no endpointslices for target port 80` 경고는 Ingress 가 Pod 보다 먼저 처리되어 발생한 일시 메시지. 직후 Pod 기동으로 해소
- Ingress.status 에는 `ipMode` 가 없음 (Service.status 에만 존재하는 필드)

### 8.3 판정

| # | 항목 | 합격 기준 | 결과 | 판정 |
|---|---|---|---|---|
| T4 | NIC 설치 + Service(type=LB) | NIC Service 가 VIP 수령 | 10.10.0.100, 3초 내 | **합격** |
| T5 | NIC write-back ② | `get ingress` ADDRESS 에 VIP | 10.10.0.100, 1.2초 | **합격** |
| T6 | Ingress → DNS 등록 | `dig web.tenant-a.poc.internal` 이 VIP 반환 | 10.10.0.100 | **합격** |
| T7 | 반영 지연 | apply → dig 성공 60초 이내 | **6.4초** (1회 측정) | **합격** |

**G1 달성.** ① mock → Service.status, ② NIC → Ingress.status, ExternalDNS → BIND9 전 구간이 Ingress 경로로 동작 확인. Service 어노테이션 경로(§3)와 Ingress 경로(§8) 모두 성립.

### 8.4 비고

- **write-back ② 의 의존 관계.** NIC 은 Ingress.status 에 쓸 IP 를 자기 Service(`nic-nginx-ingress-controller`) 의 `status.loadBalancer.ingress` 에서 읽음. ① 이 비면 ② 도 비고, ExternalDNS 는 입력이 없어 조용히 아무것도 안 함 → T11 에서 재현할 무증상 실패의 원인 지점.
- **지연의 구성.** 6.4초 중 NIC 처리 ~0.3초, ExternalDNS 의 이벤트 반응 + rfc2136 전송 ~5초. `--events` 가 없으면 `--interval=1m` 까지 늘어남. T7 은 1회 측정값이며 반복 측정 시 ±수 초 변동 예상.
- **Ingress 패턴의 IP 효율.** 앱 Service 가 ClusterIP 이므로 테넌트 앱이 늘어나도 VIP 는 NIC Service 1개분만 소모. 시나리오 B(§3) 와의 핵심 차이.
- 현재 상태: tenant-a 의 web Deployment/Service/Ingress 와 DNS 레코드는 **유지 중** (T8~T12 에서 재사용).
