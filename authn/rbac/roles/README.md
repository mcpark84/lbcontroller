# 역할별 RBAC (PoC)

| role | group | 바인딩 | 범위 |
|---|---|---|---|
| skt-admin | `mk8s:skt-admin` | ClusterRoleBinding → `cluster-admin` | 클러스터 전체 (컨트롤 플레인 포함) |
| tenant-operator | `mk8s:tenant-operator` | RoleBinding → `view` | 고객 네임스페이스 `default`, `dynamo-system`, `inferium` 조회만 |
| tenant-user | `mk8s:tenant-user` | RoleBinding → `edit` | 같은 네임스페이스 CRUD |
| (tenant 공통) | 위 두 그룹 | ClusterRole `mk8s-tenant-namespace-get` | 해당 네임스페이스 `get` 만 (목록 불가) |

적용: `kubectl apply -f authn/rbac/roles/` / 제거: `kubectl delete -l app.kubernetes.io/part-of=poc-authn clusterrole,clusterrolebinding,rolebinding -A`
