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

---

## 9. T8 — `--domain-filter` 경계 (멀티테넌시 통제)

> 시험일: 2026-10-07 09:38 ~ 09:40 UTC / 매니페스트 `manifests/t8-evil-ingress.yaml`

### 9.1 시험 내용

tenant-a 네임스페이스에 **타 테넌트 도메인**을 host 로 갖는 Ingress 생성.

```yaml
kind: Ingress
metadata: { name: evil, namespace: tenant-a }
spec:
  ingressClassName: neocloud
  rules:
    - host: evil.tenant-b.poc.internal     # tenant-a 가 tenant-b 도메인을 참칭
```

### 9.2 결과 (보정 전 — 두 ExternalDNS 모두 전 네임스페이스 감시 상태)

| 관찰 대상 | 결과 |
|---|---|
| NIC | Ingress 수락, ADDRESS `10.10.0.100` 기록. **NIC 은 host 값을 검사하지 않음** |
| tenant-a ExternalDNS (`--domain-filter=tenant-a.poc.internal`) | `ignoring record evil.tenant-b.poc.internal that does not match domain filter` → tenant-a 존 미등록 |
| tenant-b ExternalDNS (`--domain-filter=tenant-b.poc.internal`) | `Adding RR: evil.tenant-b.poc.internal 60 A 10.10.0.100` → **tenant-b 존에 등록됨** |

```
$ dig @10.233.18.38 evil.tenant-a.poc.internal +short     → (없음)
$ dig @10.233.18.38 evil.tenant-b.poc.internal +short     → 10.10.0.100
$ dig @10.233.18.38 TXT evil.tenant-b.poc.internal +short
"heritage=external-dns,external-dns/owner=tenant-b-cluster,external-dns/resource=ingress/tenant-a/evil"
```

TXT 의 `resource=ingress/tenant-a/evil` 이 "tenant-b 존의 레코드가 tenant-a 의 리소스에서 비롯됨" 을 그대로 보여줌.

### 9.3 보정 — 운영 구성(테넌트당 클러스터) 모사

운영에서는 tenant-b 의 ExternalDNS 가 tenant-b 클러스터 안에 있어 tenant-a 의 Ingress 를 볼 수 없음. 한 클러스터 PoC 에서 이를 모사하기 위해 각 인스턴스에 `--namespace=<자기 테넌트>` 추가 (`manifests/external-dns.yaml`).

재배포(09:39:10) 직후 tenant-b 인스턴스가 `--policy=sync` 로 자기 소유 evil 레코드를 즉시 회수:
```
09:39:11 Removing RR: evil.tenant-b.poc.internal 60 A 10.10.0.100
09:39:11 Removing RR: evil.tenant-b.poc.internal 0 TXT "...owner=tenant-b-cluster..."
09:39:11 Removing RR: a-evil.tenant-b.poc.internal 0 TXT "..."
```
tenant-a 의 `web.tenant-a.poc.internal` 은 영향 없이 유지. evil Ingress 삭제 후 tenant-b 존은 초기 상태(NS, ns1)로 복귀.

### 9.4 판정

| # | 항목 | 합격 기준 | 결과 | 판정 |
|---|---|---|---|---|
| T8 | `--domain-filter` 경계 | tenant-a ExternalDNS 가 타 테넌트 host 를 미등록 | `does not match domain filter` 로 거부 | **합격** |

### 9.5 설계 시사점 (★ G2 핵심 산출물)

- **`--domain-filter` 는 "이 인스턴스가 어느 존에 쓰는가" 만 제한한다.** "누가 그 이름을 요구할 수 있는가" 는 걸러주지 않는다. 같은 클러스터에 두 인스턴스가 있으면 tenant-a 의 Ingress 가 tenant-b 존을 차지할 수 있음(9.2 로 실증).
- **도메인 보안의 실제 경계 = "어느 클러스터의 ExternalDNS 가 어느 존의 쓰기 키를 갖는가".** 설계 덱의 "도메인 보안은 설정으로 달성" 은 **클러스터 분리와 결합될 때만** 성립.
- 공유 클러스터에서 테넌트를 나눠야 한다면 `--namespace` 또는 `--label-filter` 가 필수. 추가로 BIND 측에서 **존별 TSIG 키 분리**(현재 PoC 는 키 1개를 두 존이 공유)를 해야 ExternalDNS 오설정 시에도 DNS 서버가 2차로 막아줌.
- NIC 은 host 를 검증하지 않으므로 Ingress 단계에서 막으려면 별도 어드미션 정책(예: Kyverno/Gatekeeper 로 네임스페이스별 허용 도메인 접미사 강제)이 필요.
- 이 보정(`--namespace`)은 T9 의 전제. tenant-b 인스턴스가 tenant-a 의 Ingress 를 **못 봐야** "내 소유 레코드인데 소스에 없다 → 삭제" 라는 T9 사고가 재현됨.

---

## 10. T9 — `--txt-owner-id` 충돌 (★ 운영 사고 재현)

> 시험일: 2026-10-07 09:42 ~ 09:48 UTC / 오설정 매니페스트 `manifests/t9-owner-collision.yaml`, 복구 `manifests/external-dns.yaml`

### 10.1 오설정 내용 (의도적)

| 플래그 | tenant-a | tenant-b (오설정) | 비고 |
|---|---|---|---|
| `--txt-owner-id` | tenant-a-cluster | **tenant-a-cluster** (원래 tenant-b-cluster) | ★ 충돌 |
| `--domain-filter` | poc.internal | poc.internal | 상위 도메인으로 확장 |
| `--rfc2136-zone` | tenant-a + tenant-b | tenant-a + tenant-b | BIND 에 poc.internal 상위 존이 없어 존 단위로 열거 |
| `--namespace` | tenant-a | tenant-b | 유지 (T8 보정. tenant-b 는 tenant-a 의 Ingress 를 못 봄) |

사전 상태: `web.tenant-a.poc.internal A 10.10.0.100`, TXT `owner=tenant-a-cluster` (T5 에서 생성, 안정 상태)

### 10.2 결과 — flapping 재현

오설정 적용 09:42:01. `web.tenant-a.poc.internal` 을 5초 간격으로 질의한 결과:

```
09:42:03 A=(없음)        ← tenant-b 가 삭제
09:42:08 A=10.10.0.100   ← tenant-a 가 재생성
09:43:04 A=(없음)
09:43:14 A=10.10.0.100
09:44:09 A=(없음)
09:44:14 A=10.10.0.100
```

**1분 주기(`--interval=1m`)로 삭제 → 재생성이 무한 반복.** 매 주기마다 5~10초 동안 레코드가 존재하지 않음.

tenant-b ExternalDNS 로그 (삭제 주체):
```
09:42:03 ApplyChanges (Create: 0, UpdateOld: 0, UpdateNew: 0, Delete: 3)
09:42:03 Removing RR: web.tenant-a.poc.internal 60 A 10.10.0.100
09:42:03 Removing RR: web.tenant-a.poc.internal 0 TXT "heritage=external-dns,external-dns/owner=tenant-a-cluster,..."
09:42:03 Removing RR: a-web.tenant-a.poc.internal 0 TXT "..."
(09:43:03, 09:44:04 동일 반복)
```

tenant-a ExternalDNS 로그 (재생성 주체):
```
09:42:08 ApplyChanges (Create: 3, UpdateOld: 0, UpdateNew: 0, Delete: 0)
09:42:08 Adding RR: web.tenant-a.poc.internal 60 A 10.10.0.100
(09:43:09, 09:44:10 동일 반복)
```

BIND9 로그 (누가 썼는지 Pod IP 로 식별. .45 = tenant-b, .5 = tenant-a):
```
09:44:04 client 10.233.95.45/key externaldns: updating zone 'tenant-a.poc.internal/IN': deleting an RR at web.tenant-a.poc.internal A
09:44:10 client 10.233.95.5/key externaldns:  updating zone 'tenant-a.poc.internal/IN': adding an RR at 'web.tenant-a.poc.internal' A 10.10.0.100
09:45:04 client 10.233.95.45/key externaldns: updating zone 'tenant-a.poc.internal/IN': deleting an RR at web.tenant-a.poc.internal A
```

**메커니즘.** tenant-b 인스턴스는 AXFR 로 tenant-a 존을 읽고 `web` 레코드의 TXT `owner=tenant-a-cluster` 가 자기 owner-id 와 같으므로 "내 소유" 로 판단. 그런데 자기 소스(tenant-b 네임스페이스)에는 해당 Ingress 가 없으므로 `--policy=sync` 규칙에 따라 "더 이상 필요 없는 내 레코드" 로 간주해 삭제. tenant-a 인스턴스는 다음 루프에서 소스에 Ingress 가 있는데 레코드가 없으니 재생성. **양쪽 모두 각자의 규칙대로 정상 동작하면서 사고가 발생**하며, 어느 쪽에도 에러 로그가 남지 않음.

### 10.3 복구 — owner-id 분리

09:45:25 원래 설정(`tenant-b-cluster`, 각자 존/필터) 재적용. 이후 2분 30초 관측:

```
09:45:27 A=10.10.0.100   (이후 변화 없음)
```

- tenant-b 로그 `Removing RR` 건수: **0**
- tenant-b 는 자기 존의 ns1 레코드에 대해 `Skipping endpoint ... because owner id does not match` 만 반복 (정상 보호 동작)
- tenant-a 로그: `All records are already up to date` 반복, 재생성 없음
- 최종: `web.tenant-a.poc.internal A 10.10.0.100`, TXT `owner=tenant-a-cluster` 안정

### 10.4 판정

| # | 항목 | 합격 기준 | 결과 | 판정 |
|---|---|---|---|---|
| T9 | `--txt-owner-id` 충돌 | 삭제가 재현되어야 함(위험 실증), owner-id 분리 후 삭제 중지 | 1분 주기 flapping 3회 관측, 분리 후 0건 | **합격** |

### 10.5 설계 시사점 (★ G2 핵심 산출물 — 프로비저닝 자동화 요건)

- **`--txt-owner-id` 는 클러스터 간 전역 유일해야 한다.** 같은 DNS 존 집합을 공유하는 모든 ExternalDNS 인스턴스에 대해, 하나라도 겹치면 서로의 레코드를 지운다. 사고는 **에러 없이, 1분마다, 조용히** 발생하므로 모니터링으로 잡기 어려움. 사용자 증상은 "간헐적 NXDOMAIN".
- **클러스터 프로비저닝 자동화에서 owner-id 를 UUID(또는 클러스터 ID)로 자동 주입할 것.** 사람이 값을 적는 구조(복사 붙여넣기)에서는 이 사고가 반드시 발생한다. ExternalDNS 의 기본값은 `"default"` 이므로 **플래그를 빼먹으면 모든 클러스터가 같은 owner-id 가 된다** = 최악의 경우가 기본값.
- T8 과 결합하면: `--domain-filter` 를 넓게(상위 도메인) 주는 운영이 owner-id 충돌과 만나면 피해 범위가 전 테넌트로 확대. **domain-filter 는 가능한 좁게**(테넌트 존 단위) 유지해야 피해를 격리할 수 있음.
- 2차 방어: BIND 측 **존별 TSIG 키 분리**. tenant-b 의 키로 tenant-a 존을 못 쓰게 하면 ExternalDNS 오설정이 있어도 DNS 서버가 거부(`REFUSED`). 현재 PoC 는 키 1개 공유라 이 방어가 없음. 운영 DNS 요건에 반영 필요.
- 탐지 수단 후보: ExternalDNS 로그의 `Delete: N` 이 **매 interval 마다 같은 레코드에 반복** 되는 패턴, 또는 DNS 서버 측 동일 RR 의 add/delete 반복. 운영 런북(§ T11 산출물)에 포함할 것.

---

## 11. T10 — 삭제 동기화 (Ingress 삭제 → A/TXT 소멸)

> 시험일: 2026-10-07 22:55 UTC (= 10-08 07:55 KST) / 대상 `tenant-a/web` Ingress (T5 생성, 13시간 유지)

### 11.1 사전 상태

```
NAME   CLASS      HOSTS                       ADDRESS       PORTS   AGE
web    neocloud   web.tenant-a.poc.internal   10.10.0.100   80      13h
```
존: `web A 10.10.0.100` + TXT `web` + TXT `a-web` (owner=tenant-a-cluster) 3건 존재.

### 11.2 결과

Ingress 삭제 22:55:35.262 → **6.4초 후 A / TXT / a-TXT 3건 동시 소멸.**

```
22:55:35 A=10.10.0.100 TXT=있음 a-TXT=있음      ← 삭제 직후
22:55:41 A/TXT/TXT 모두 소멸 (+6.4s)
```

ExternalDNS tenant-a 로그:
```
22:55:41 ApplyChanges (Create: 0, UpdateOld: 0, UpdateNew: 0, Delete: 3)
22:55:41 Removing RR: web.tenant-a.poc.internal 60 A 10.10.0.100
22:55:41 Removing RR: web.tenant-a.poc.internal 0 TXT "...owner=tenant-a-cluster,...resource=ingress/tenant-a/web"
22:55:41 Removing RR: a-web.tenant-a.poc.internal 0 TXT "..."
```
BIND9 로그: 같은 초에 `deleting an RR` 3건 (단일 rfc2136 트랜잭션).

삭제 후 존 전체(AXFR): NS, ns1 만 남음. **잔여 레코드 0건.**

재생성(T11/T12 재사용 목적) 22:55:41.863 → 22:55:47 A 재등록. **T7 2회차 측정 5.4초** (1회차 6.4초).

### 11.3 판정

| # | 항목 | 합격 기준 | 결과 | 판정 |
|---|---|---|---|---|
| T10 | 삭제 동기화 | Ingress 삭제 후 1분 내 A + TXT 동시 소멸 | 6.4초, 3건 단일 트랜잭션 | **합격** |
| T7 (누적) | 반영 지연 | 60초 이내 | 1회차 6.4s / 2회차 5.4s | 합격 |

### 11.4 비고

- A 와 소유권 TXT 가 **한 트랜잭션으로** 지워지므로 "A 는 사라졌는데 TXT 가 남아 다음 생성을 막는" 고아 상태가 생기지 않음. 운영에서 레코드 누적을 걱정할 지점은 ExternalDNS 쪽이 아니라 **ExternalDNS 가 죽어 있는 동안 삭제된 리소스** (복구 후 sync 루프가 정리하는지는 T11 과 연관).
- 생성(5~6s)과 삭제(6s)의 지연이 거의 같음 → `--events` 가 삭제 이벤트에도 즉시 반응.

### 11.5 환경 이슈 (13시간 운영 중 발견)

| Pod | 재시작 | 원인 | 조치 |
|---|---|---|---|
| bind9 | 2회 (OOMKilled, exit 137, 마지막 13:55 UTC) | 288 CPU 노드에서 named 가 워커 스레드/UDP 리스너를 **288개** 자동 생성 → 메모리 한도 256Mi 초과 | `named -n 2 -U 2` 로 스레드 수 제한 + 한도 512Mi (`manifests/bind9.yaml`) |
| external-dns-tenant-a | 2회 (exit 1, 09:54 UTC) | bind9 재시작 순간 AXFR 접속 거부 → 기동 시 run-once 가 `fatal` 종료 | 조치 불필요. K8s 가 재시작해 자동 복구. **ExternalDNS 는 DNS 서버 불가 시 crash-loop 로 드러난다** (조용히 멈추지 않음) → 모니터링 포인트 |

- bind9 가 컨테이너 재시작(Pod 재생성 아님)이었기 때문에 emptyDir 의 존 저널이 보존되어 레코드가 유지됨. **Pod 가 재생성되면 존은 ConfigMap 초기 상태로 리셋**되며, 다음 ExternalDNS 루프(≤1분)가 레코드를 재생성함. PoC 에서는 허용. 운영 DNS 는 영속 스토리지가 있으므로 해당 없음.
