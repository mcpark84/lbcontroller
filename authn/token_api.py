#!/usr/bin/env python3
"""PoC token issuing API (the "portal" side). Standard library only.

  POST   /tokens                        body {owner_id, tenant, role, cluster?, ttl_seconds?}
  GET    /tokens[?owner=]               list (hash_prefix only, never plaintext)
  DELETE /tokens/{id}                   revoke one
  DELETE /tokens?user={owner}           revoke every token of one owner
  GET    /clusters/{id}/kubeconfig?owner=&tenant=&role=&ttl=
                                        issue (issued_via=kubeconfig) and return a kubeconfig
  GET    /healthz

role is one of skt-admin / tenant-operator / tenant-user (400 otherwise) and is
bound to the token for life: changing a role means a new token.
Plaintext is returned exactly once (POST /tokens, GET .../kubeconfig).
Every issue / revoke is appended to --audit-log as one JSON line.
Requests carry an optional X-Actor header, recorded in the audit line.
No API authentication: bind to loopback only (PoC scope).
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tokenstore as ts  # noqa: E402

LOG = logging.getLogger("poc-token-api")

# Scalars are JSON-quoted: an owner like "3" or "true" must stay a YAML string.
KUBECONFIG_TMPL = """apiVersion: v1
kind: Config
clusters:
  - name: {cluster}
    cluster:
      certificate-authority-data: {ca}
      server: {server}
users:
  - name: {owner}
    user:
      token: {token}
contexts:
  - name: {cluster}
    context:
      cluster: {cluster}
      user: {owner}
current-context: {cluster}
"""


def render_kubeconfig(cluster, ca, server, owner, token):
    q = json.dumps
    return KUBECONFIG_TMPL.format(cluster=q(cluster), ca=q(ca), server=q(server), owner=q(owner), token=q(token))


def cluster_endpoint(kubeconfig):
    """Read endpoint + CA from an admin kubeconfig (stands in for NKE's
    'get cluster endpoint / serving CA' API, question N3)."""
    cmd = ["kubectl", "config", "view", "--raw", "-o", "json"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    doc = json.loads(subprocess.check_output(cmd))
    c = doc["clusters"][0]["cluster"]
    return c["server"], c["certificate-authority-data"]


class Handler(BaseHTTPRequestHandler):
    server_version = "poc-token-api/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # we log ourselves, with hash prefixes only

    # ---- helpers -------------------------------------------------------
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _audit(self, action, **fields):
        rec = {"ts": ts.iso(ts.now()), "actor": self.headers.get("X-Actor") or "anonymous",
               "action": action, **fields}
        with open(self.server.cfg.audit_log, "a") as f:
            f.write(json.dumps(rec) + "\n")
        LOG.info("%s %s", action, json.dumps(fields))

    def _issue(self, owner, tenant, role, cluster, ttl, via):
        plaintext, row = ts.issue(self.server.db, owner, tenant, cluster, via, ttl, role)
        self._audit("issue", token_id=row["id"], owner_id=owner, tenant=tenant, role=role,
                    cluster=cluster, issued_via=via, expires_at=row["expires_at"],
                    hash_prefix=row["hash"][:12])
        return plaintext, row

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        self._body()
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        cfg = self.server.cfg

        if u.path == "/healthz":
            n = self.server.db.execute("SELECT count(*) FROM tokens").fetchone()[0]
            return self._send(200, {"status": "ok", "db": cfg.db, "tokens": n, "roles": list(ts.ROLES),
                                    "cluster_id": cfg.cluster_id, "server": self.server.endpoint})

        if u.path == "/tokens":
            return self._send(200, {"items": ts.list_tokens(self.server.db, q.get("owner"))})

        if len(parts) == 2 and parts[0] == "tokens":
            row = ts.get(self.server.db, parts[1])
            return self._send(200, row) if row else self._send(404, {"error": "not found"})

        if len(parts) == 3 and parts[0] == "clusters" and parts[2] == "kubeconfig":
            cid = parts[1]
            if cid != cfg.cluster_id:
                return self._send(404, {"error": f"unknown cluster {cid}"})
            owner, tenant, role = q.get("owner"), q.get("tenant"), q.get("role")
            if not owner or not tenant or not role:
                return self._send(400, {"error": "owner, tenant and role are required"})
            if role not in ts.ROLES:
                return self._send(400, {"error": f"role must be one of {list(ts.ROLES)}"})
            bad = ts.invalid_identifier(owner=owner, tenant=tenant)
            if bad:
                return self._send(400, {"error": bad})
            ttl = int(q.get("ttl") or cfg.default_ttl)
            plaintext, row = self._issue(owner, tenant, role, cid, ttl, "kubeconfig")
            body = render_kubeconfig(cid, self.server.ca, self.server.endpoint, owner, plaintext)
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml")
            self.send_header("X-Token-Id", row["id"])
            self.send_header("X-Token-Expires", row["expires_at"] or "")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlsplit(self.path)
        try:
            b = self._body()
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        cfg = self.server.cfg
        if u.path != "/tokens":
            return self._send(404, {"error": "not found"})
        owner, tenant, role = b.get("owner_id"), b.get("tenant"), b.get("role")
        if not owner or not tenant or not role:
            return self._send(400, {"error": "owner_id, tenant and role are required"})
        if role not in ts.ROLES:
            return self._send(400, {"error": f"role must be one of {list(ts.ROLES)}"})
        bad = ts.invalid_identifier(owner_id=owner, tenant=tenant)
        if bad:
            return self._send(400, {"error": bad})
        cluster = b.get("cluster") or cfg.cluster_id
        ttl = int(b.get("ttl_seconds") or cfg.default_ttl)
        plaintext, row = self._issue(owner, tenant, role, cluster, ttl, "operator")
        out = ts.public(self.server.db.execute("SELECT * FROM tokens WHERE id=?", (row["id"],)).fetchone())
        out["token"] = plaintext  # the one and only time it leaves the server
        self._send(201, out)

    def do_DELETE(self):
        self._body()
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        if u.path == "/tokens" and q.get("user"):
            n = ts.revoke_user(self.server.db, q["user"])
            self._audit("revoke-user", owner_id=q["user"], revoked=n)
            return self._send(200, {"owner_id": q["user"], "revoked": n})
        if len(parts) == 2 and parts[0] == "tokens":
            row = ts.get(self.server.db, parts[1])
            if not row:
                return self._send(404, {"error": "not found"})
            n = ts.revoke(self.server.db, parts[1])
            self._audit("revoke", token_id=parts[1], owner_id=row["owner_id"],
                        hash_prefix=row["hash_prefix"], already_revoked=(n == 0))
            return self._send(200, ts.get(self.server.db, parts[1]))
        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", required=True)
    ap.add_argument("--cluster-id", default="gasandevcontrol")
    ap.add_argument("--kubeconfig", help="admin kubeconfig to read endpoint/CA from (default: kubectl's current)")
    ap.add_argument("--default-ttl", type=int, default=86400, help="seconds")
    ap.add_argument("--audit-log", required=True, help="JSON lines: issue / revoke events")
    ap.add_argument("--log")
    cfg = ap.parse_args()

    handlers = [logging.StreamHandler(sys.stderr)]
    if cfg.log:
        handlers.append(logging.FileHandler(cfg.log))
    logging.Formatter.converter = time.gmtime  # UTC, same as token_audit.jsonl / apiserver audit.log
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    srv = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    srv.cfg = cfg
    srv.db = ts.open_db(cfg.db)
    srv.endpoint, srv.ca = cluster_endpoint(cfg.kubeconfig)
    LOG.info("[start] bind=%s:%d db=%s cluster_id=%s endpoint=%s", cfg.bind, cfg.port, cfg.db,
             cfg.cluster_id, srv.endpoint)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    LOG.info("[stop]")


if __name__ == "__main__":
    main()
