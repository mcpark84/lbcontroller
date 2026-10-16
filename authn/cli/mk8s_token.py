#!/usr/bin/env python3
"""mk8s-token — CLI for the Managed K8s token API (authn/API.md).

  mk8s-token                      # no arguments -> interactive menu
  mk8s-token issue       OWNER TENANT ROLE [--ttl S] [--cluster ID]
  mk8s-token kubeconfig  OWNER TENANT ROLE [--ttl S] [-o FILE]     # new token
  mk8s-token wrap        OWNER [--token PT] [-o FILE]              # kubeconfig around an existing token
  mk8s-token list        [--owner O] [--role R] [--status valid|revoked|expired]
  mk8s-token get         ID
  mk8s-token revoke      ID [ID ...]
  mk8s-token revoke-user OWNER [-y]
  mk8s-token health

Global: --api URL (env MK8S_TOKEN_API, default http://127.0.0.1:8080)
        --actor NAME (env MK8S_ACTOR, default $USER)   --json (raw API output)
Exit codes: 0 ok / 1 API error (4xx, 5xx) / 2 cannot reach API or bad usage.
Standard library only.
"""
import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ROLES = ("skt-admin", "tenant-operator", "tenant-user")
DEFAULT_API = os.environ.get("MK8S_TOKEN_API", "http://127.0.0.1:8080")
DEFAULT_ACTOR = os.environ.get("MK8S_ACTOR") or getpass.getuser()
DEFAULT_CLUSTER = os.environ.get("MK8S_CLUSTER_ID", "gasandevcontrol")


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


class Client:
    def __init__(self, api, actor):
        self.api, self.actor = api.rstrip("/"), actor

    def call(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method,
                                     headers={"X-Actor": self.actor, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = r.read()
                return (payload, dict(r.headers)) if raw else json.loads(payload or b"{}")
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            try:
                txt = json.loads(txt).get("error", txt)
            except ValueError:
                pass
            raise ApiError(e.code, txt)
        except urllib.error.URLError as e:
            print(f"error: cannot reach {self.api}: {e.reason}", file=sys.stderr)
            sys.exit(2)


# ---------- output helpers ----------
def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def status_of(t):
    if t.get("revoked_at"):
        return "revoked"
    if t.get("expires_at") and parse(t["expires_at"]) <= now():
        return "expired"
    return "valid"


def table(rows, cols):
    if not rows:
        print("(none)")
        return
    w = [max(len(c), *(len(str(r.get(c, "") or "-")) for r in rows)) for c in cols]
    print("  ".join(c.upper().ljust(w[i]) for i, c in enumerate(cols)))
    for r in rows:
        print("  ".join(str(r.get(c, "") or "-").ljust(w[i]) for i, c in enumerate(cols)))


LIST_COLS = ("id", "owner_id", "tenant", "role", "status", "issued_via", "expires_at", "last_used_at", "hash_prefix")


def emit(args, obj, rows=None, cols=LIST_COLS):
    if args.json:
        print(json.dumps(obj, indent=1))
    elif rows is not None:
        for r in rows:
            r["status"] = status_of(r)
        table(rows, cols)


# ---------- commands ----------
def cmd_issue(c, a):
    body = {"owner_id": a.owner, "tenant": a.tenant, "role": a.role, "ttl_seconds": a.ttl}
    if a.cluster:
        body["cluster"] = a.cluster
    t = c.call("POST", "/tokens", body)
    if a.json:
        print(json.dumps(t, indent=1))
        return
    print(f"issued  id={t['id']}  owner={t['owner_id']}  role={t['role']}  cluster={t['cluster']}  expires={t['expires_at']}")
    print(f"token (shown once): {t['token']}")


def cmd_kubeconfig(c, a):
    q = f"owner={a.owner}&tenant={a.tenant}&role={a.role}&ttl={a.ttl}"
    payload, hdr = c.call("GET", f"/clusters/{a.cluster or DEFAULT_CLUSTER}/kubeconfig?{q}", raw=True)
    out = save_kubeconfig(payload, a.output or default_kubeconfig_path(a.owner))
    tid, exp = hdr.get("X-Token-Id", "?"), hdr.get("X-Token-Expires", "?")
    if a.json:
        print(json.dumps({"file": out, "token_id": tid, "expires_at": exp}))
    else:
        print(f"wrote {out} (mode 600)  token_id={tid}  expires={exp}")
        print(f"try: kubectl --kubeconfig {out} get pods -n default")


def cmd_wrap(c, a):
    tok = (a.token or sys.stdin.readline()).strip()
    if not tok:
        print("error: token plaintext required", file=sys.stderr); sys.exit(2)
    h12 = hashlib.sha256(tok.encode()).hexdigest()[:12]
    hit = next((t for t in c.call("GET", f"/tokens?owner={a.owner}")["items"] if t["hash_prefix"] == h12), None)
    if not hit:
        print(f"error: token not found among {a.owner}'s tokens (hash {h12})", file=sys.stderr); sys.exit(1)
    if status_of(hit) != "valid":
        print(f"error: token {hit['id']} is {status_of(hit)}", file=sys.stderr); sys.exit(1)
    info = c.call("GET", f"/clusters/{DEFAULT_CLUSTER}")
    out = save_kubeconfig(compose_kubeconfig(info["cluster_id"], info["server"], info["certificate_authority_data"], a.owner, tok),
                          a.output or default_kubeconfig_path(a.owner))
    print(json.dumps({"file": out, "token_id": hit["id"], "role": hit.get("role")}) if a.json
          else f"wrote {out} (mode 600)  token_id={hit['id']}  role={hit.get('role')}")


def cmd_list(c, a):
    items = c.call("GET", "/tokens" + (f"?owner={a.owner}" if a.owner else ""))["items"]
    if a.role:
        items = [t for t in items if t.get("role") == a.role]
    if a.status:
        items = [t for t in items if status_of(t) == a.status]
    emit(a, {"items": items}, rows=items)


def cmd_get(c, a):
    t = c.call("GET", f"/tokens/{a.id}")
    emit(a, t, rows=[t])


def cmd_revoke(c, a):
    rc = 0
    for tid in a.ids:
        try:
            t = c.call("DELETE", f"/tokens/{tid}")
            print(json.dumps(t, indent=1) if a.json else f"revoked {tid}  owner={t['owner_id']}  revoked_at={t['revoked_at']}")
        except ApiError as e:
            print(f"{tid}: {e}", file=sys.stderr)
            rc = 1
    sys.exit(rc)


def cmd_revoke_user(c, a):
    if not a.yes:
        n = len([t for t in c.call("GET", f"/tokens?owner={a.owner}")["items"] if status_of(t) == "valid"])
        ans = input(f"revoke {n} valid token(s) of '{a.owner}'? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted")
            sys.exit(2)
    r = c.call("DELETE", f"/tokens?user={a.owner}")
    print(json.dumps(r) if a.json else f"revoked {r['revoked']} token(s) of {r['owner_id']}")


def cmd_health(c, a):
    h = c.call("GET", "/healthz")
    if a.json:
        print(json.dumps(h, indent=1))
    else:
        for k, v in h.items():
            print(f"{k:11} {v}")


# ---------- interactive mode ----------
def ask(prompt, default=None, choices=None):
    """Prompt with optional default / numbered choices. Empty input -> default. 'q' -> None."""
    while True:
        if choices:
            for i, ch in enumerate(choices, 1):
                print(f"   {i}) {ch}")
        hint = f" [{default}]" if default is not None else ""
        try:
            v = input(f"{prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if v.lower() == "q":
            return None
        if v == "" and default is not None:
            return default
        if choices:
            if v.isdigit() and 1 <= int(v) <= len(choices):
                return choices[int(v) - 1]
            if v in choices:
                return v
            print("   번호를 고르세요 (q = 취소)")
            continue
        if v:
            return v
        print("   값을 입력하세요 (q = 취소)")


def pick_token(c, title="토큰 목록"):
    """List tokens with row numbers and return the chosen token dict (or None)."""
    owner = ask("소유자 필터 (엔터 = 전체)", default="")
    if owner is None:
        return None
    status = ask("상태 필터", default="valid", choices=["valid", "revoked", "expired", "all"])
    if status is None:
        return None
    items = c.call("GET", "/tokens" + (f"?owner={owner}" if owner else ""))["items"]
    for t in items:
        t["status"] = status_of(t)
    if status != "all":
        items = [t for t in items if t["status"] == status]
    print(f"\n{title} ({len(items)}건)")
    if not items:
        return None
    cols = ("#",) + LIST_COLS
    rows = [dict(t, **{"#": i}) for i, t in enumerate(items, 1)]
    table(rows, cols)
    n = ask("\n번호 선택 (q = 돌아가기)")
    if n is None or not n.isdigit() or not 1 <= int(n) <= len(items):
        return None
    return items[int(n) - 1]


def i_list(c):
    t = pick_token(c)
    if not t:
        return
    print(json.dumps(t, indent=1))
    act = ask("동작", default="돌아가기", choices=["돌아가기", "이 토큰 폐기"])
    if act == "이 토큰 폐기":
        if ask(f"정말 {t['id']} ({t['owner_id']}, {t.get('role')}) 를 폐기할까요?", default="n", choices=["y", "n"]) == "y":
            r = c.call("DELETE", f"/tokens/{t['id']}")
            print(f"폐기됨 revoked_at={r['revoked_at']}  (apiserver 캐시 때문에 마지막 사용 + TTL 까지는 통과할 수 있음)")


def owner_profile(c, owner):
    """tenant/role of the owner's most recent token, or None for a new owner."""
    items = c.call("GET", f"/tokens?owner={owner}")["items"]
    items = [t for t in items if t.get("role") in ROLES]
    if not items:
        return None
    latest = max(items, key=lambda t: t["created_at"])
    return latest["tenant"], latest["role"]


CLI_DIR = os.path.dirname(os.path.abspath(__file__))


def default_kubeconfig_path(owner):
    """authn/cli/<owner>.kubeconfig (next to this script; *.kubeconfig is git-ignored)."""
    return os.path.join(CLI_DIR, f"{owner}.kubeconfig")


def compose_kubeconfig(cluster, server, ca, owner, token):
    q = json.dumps
    return (f"apiVersion: v1\nkind: Config\nclusters:\n  - name: {q(cluster)}\n    cluster:\n"
            f"      certificate-authority-data: {q(ca)}\n      server: {q(server)}\n"
            f"users:\n  - name: {q(owner)}\n    user:\n      token: {q(token)}\n"
            f"contexts:\n  - name: {q(cluster)}\n    context:\n      cluster: {q(cluster)}\n      user: {q(owner)}\n"
            f"current-context: {q(cluster)}\n").encode()


def i_wrap_existing(c, owner):
    """Build a kubeconfig around a token the user already holds. The plaintext
    never leaves this machine: it is hashed locally and matched against the
    owner's hash_prefix list to identify the token and check its status."""
    print("   서버에는 해시만 있어 평문은 복구할 수 없습니다. 발급 때 받은 평문을 붙여 넣으세요.")
    tok = ask("토큰 평문 (pt_...)")
    if not tok:
        return
    tok = tok.strip()
    h12 = hashlib.sha256(tok.encode()).hexdigest()[:12]
    items = c.call("GET", f"/tokens?owner={owner}")["items"]
    hit = next((t for t in items if t["hash_prefix"] == h12), None)
    if not hit:
        print(f"   이 토큰은 소유자 '{owner}' 의 토큰 목록에 없습니다 (해시 {h12}). 소유자 또는 토큰을 확인하세요.")
        return
    st = status_of(hit)
    print(f"   일치: id={hit['id']} role={hit.get('role')} status={st} expires={hit.get('expires_at')}")
    if st != "valid":
        print(f"   {st} 상태라 kubeconfig 를 만들어도 401 입니다. 새 토큰을 발급하세요.")
        return
    info = c.call("GET", f"/clusters/{DEFAULT_CLUSTER}")
    out = ask("저장 파일", default=default_kubeconfig_path(owner))
    if not out:
        return
    out = save_kubeconfig(compose_kubeconfig(info["cluster_id"], info["server"], info["certificate_authority_data"], owner, tok), out)
    print(f"\n저장: {out} (600)  token_id={hit['id']}  role={hit.get('role')}")
    print(f"사용: export KUBECONFIG={out}   또는   kubectl --kubeconfig {out} get pods -n default")


def save_kubeconfig(payload, out):
    out = os.path.abspath(os.path.expanduser(out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    os.chmod(out, 0o600)
    return out


def i_issue(c, kubeconfig=False):
    owner = ask("소유자 이름 (owner_id, 예: alice)")
    if not owner:
        return
    if kubeconfig:
        how = ask("방식", default="새 토큰 발급", choices=["새 토큰 발급", "이미 받은 토큰 평문으로 만들기"])
        if how is None:
            return
        if how.startswith("이미"):
            return i_wrap_existing(c, owner)
    prof = owner_profile(c, owner)
    if prof:
        tenant, role = prof
        print(f"   기존 소유자: tenant={tenant} role={role} 로 발급합니다")
        if ask("   그대로 진행", default="y", choices=["y", "n"]) != "y":
            prof = None
    if not prof:
        tenant = ask("테넌트", default="a")
        if not tenant:
            return
        role = ask("역할", choices=list(ROLES))
        if not role:
            return
    ttl = ask("만료 (초)", default="86400")
    if not ttl or not ttl.isdigit():
        return
    if kubeconfig:
        out = ask("저장 파일", default=default_kubeconfig_path(owner))
        if not out:
            return
        payload, hdr = c.call("GET", f"/clusters/{DEFAULT_CLUSTER}/kubeconfig?owner={owner}&tenant={tenant}&role={role}&ttl={ttl}", raw=True)
        out = save_kubeconfig(payload, out)
        print(f"\n저장: {out} (600)  token_id={hdr.get('X-Token-Id')}  expires={hdr.get('X-Token-Expires')}")
        print(f"사용: export KUBECONFIG={out}   또는   kubectl --kubeconfig {out} get pods -n default")
    else:
        t = c.call("POST", "/tokens", {"owner_id": owner, "tenant": tenant, "role": role, "ttl_seconds": int(ttl)})
        print(f"\n발급 id={t['id']} role={t['role']} expires={t['expires_at']}")
        print(f"토큰 (지금 한 번만 표시): {t['token']}")


def i_revoke_user(c):
    owner = ask("소유자 (owner_id)")
    if not owner:
        return
    items = c.call("GET", f"/tokens?owner={owner}")["items"]
    valid = [t for t in items if status_of(t) == "valid"]
    print(f"{owner}: 전체 {len(items)}건, 유효 {len(valid)}건")
    if valid and ask("유효 토큰을 전부 폐기할까요?", default="n", choices=["y", "n"]) == "y":
        r = c.call("DELETE", f"/tokens?user={owner}")
        print(f"폐기됨 {r['revoked']}건")


def i_health(c):
    for k, v in c.call("GET", "/healthz").items():
        print(f"  {k:11} {v}")


def interactive(c):
    menu = [
        ("토큰 목록 보기 / 선택해서 폐기", i_list),
        ("토큰 발급 (운영자, 평문 1회 출력)", lambda c: i_issue(c, False)),
        ("kubeconfig 만들기 (새 발급 또는 기존 토큰 평문으로)", lambda c: i_issue(c, True)),
        ("소유자 전체 폐기", i_revoke_user),
        ("API 상태 (health)", i_health),
    ]
    while True:
        print(f"\n=== mk8s-token  api={c.api}  actor={c.actor} ===")
        for i, (label, _) in enumerate(menu, 1):
            print(f" {i}) {label}")
        print(" q) 종료")
        ch = ask("선택")
        if ch is None or ch.lower() == "q":
            print("bye")
            return
        if ch.isdigit() and 1 <= int(ch) <= len(menu):
            try:
                menu[int(ch) - 1][1](c)
            except ApiError as e:
                print(f"오류: {e}")
        else:
            print("1~5 또는 q")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mk8s-token", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--actor", default=DEFAULT_ACTOR)
    ap.add_argument("--json", action="store_true", help="print raw API JSON")
    sp = ap.add_subparsers(dest="cmd", required=False)

    p = sp.add_parser("issue", help="issue a token (operator path); prints the plaintext once")
    p.add_argument("owner"); p.add_argument("tenant"); p.add_argument("role", choices=ROLES)
    p.add_argument("--ttl", type=int, default=86400, help="seconds (default 86400)")
    p.add_argument("--cluster", help="target cluster id (default: server's)")
    p.set_defaults(fn=cmd_issue)

    p = sp.add_parser("kubeconfig", help="issue a token and save a kubeconfig")
    p.add_argument("owner"); p.add_argument("tenant"); p.add_argument("role", choices=ROLES)
    p.add_argument("--ttl", type=int, default=86400)
    p.add_argument("--cluster", help="cluster id in the URL (default gasandevcontrol)")
    p.add_argument("-o", "--output", help="file (default authn/cli/<owner>.kubeconfig)")
    p.set_defaults(fn=cmd_kubeconfig)

    p = sp.add_parser("wrap", help="build a kubeconfig around an existing token (plaintext from stdin or --token)")
    p.add_argument("owner"); p.add_argument("--token", help="plaintext; default: read one line from stdin")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_wrap)

    p = sp.add_parser("list", help="list tokens")
    p.add_argument("--owner"); p.add_argument("--role", choices=ROLES)
    p.add_argument("--status", choices=("valid", "revoked", "expired"))
    p.set_defaults(fn=cmd_list)

    p = sp.add_parser("get", help="show one token"); p.add_argument("id"); p.set_defaults(fn=cmd_get)

    p = sp.add_parser("revoke", help="revoke one or more tokens by id")
    p.add_argument("ids", nargs="+"); p.set_defaults(fn=cmd_revoke)

    p = sp.add_parser("revoke-user", help="revoke every valid token of an owner")
    p.add_argument("owner"); p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p.set_defaults(fn=cmd_revoke_user)

    sp.add_parser("health", help="API health / configuration").set_defaults(fn=cmd_health)

    a = ap.parse_args(argv)
    c = Client(a.api, a.actor)
    if a.cmd is None:          # no subcommand -> interactive menu
        interactive(c)
        return
    try:
        a.fn(c, a)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
