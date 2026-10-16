# mk8s-token — 토큰 API CLI (PoC)

python 표준 라이브러리 단일 파일. 설치 없이 실행한다.

**대화형 모드**: 인자 없이 실행하면 번호 메뉴가 뜬다. 목록에서 번호로 토큰을 골라 상세 보기·폐기, 역할도 번호로 선택한다. 어디서든 `q` 로 돌아간다.
```bash
python3 ~/lbcontroller/authn/cli/mk8s_token.py
 1) 토큰 목록 보기 / 선택해서 폐기
 2) 토큰 발급 (운영자, 평문 1회 출력)
 3) kubeconfig 만들기 (새 발급 또는 기존 토큰 평문으로)
 4) 소유자 전체 폐기
 5) API 상태 (health)
 q) 종료
```
서브커맨드를 주면 기존 비대화형 CLI 로 동작한다 (스크립트·자동화용).

- 발급 시 **이미 토큰이 있는 소유자**는 최근 토큰의 tenant / role 을 그대로 쓴다 (확인 한 번). 새 소유자만 tenant / role 을 묻는다.
- 메뉴 3 은 두 방식: **새 토큰 발급** 또는 **이미 받은 토큰 평문 붙여넣기**. 서버에는 해시만 있어 기존 토큰의 kubeconfig 를 서버가 다시 만들 수 없으므로, 평문은 CLI 가 로컬에서 해시해 소유자 토큰 목록과 대조한 뒤(유효성 확인) endpoint / CA 만 받아 조립한다. 평문은 서버로 가지 않는다.
- kubeconfig 저장 위치는 **이 폴더** `authn/cli/<owner>.kubeconfig` (권한 600, `.gitignore` 로 커밋 제외). 사용: `export KUBECONFIG=~/lbcontroller/authn/cli/<owner>.kubeconfig`
- 비대화형: `mk8s-token wrap OWNER --token pt_...` 또는 `echo pt_... | mk8s-token wrap OWNER`

```bash
alias mk8s-token='python3 ~/lbcontroller/authn/cli/mk8s_token.py'
export MK8S_ACTOR=dan.park            # 감사 로그의 actor (기본 $USER)
export MK8S_TOKEN_API=http://127.0.0.1:8080

mk8s-token health
mk8s-token issue alice a tenant-user --ttl 3600          # 평문 1회 출력
mk8s-token kubeconfig alice a tenant-user          # 새 토큰 + kubeconfig
echo 'pt_...' | mk8s-token wrap alice               # 기존 평문 토큰으로 kubeconfig
mk8s-token list --owner alice --status valid
mk8s-token get <id>
mk8s-token revoke <id> [<id> ...]
mk8s-token revoke-user alice          # 확인 질문. -y 로 생략
mk8s-token --json list                # 원본 JSON
```

| 종료 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | API 가 4xx/5xx (잘못된 role, 없는 id 등). 메시지는 stderr |
| 2 | API 에 연결 불가, 사용법 오류, 확인 질문에서 중단 |

`list` 의 `--role`, `--status` 는 CLI 가 로컬에서 거른다 (API 는 `owner` 필터만 제공). `status` 는 `revoked_at` / `expires_at` 으로 계산한다.
