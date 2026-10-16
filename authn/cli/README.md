# mk8s-token — 토큰 API CLI (PoC)

python 표준 라이브러리 단일 파일. 설치 없이 실행한다.

```bash
alias mk8s-token='python3 ~/lbcontroller/authn/cli/mk8s_token.py'
export MK8S_ACTOR=dan.park            # 감사 로그의 actor (기본 $USER)
export MK8S_TOKEN_API=http://127.0.0.1:8080

mk8s-token health
mk8s-token issue alice a tenant-user --ttl 3600          # 평문 1회 출력
mk8s-token kubeconfig alice a tenant-user -o alice.kubeconfig
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
