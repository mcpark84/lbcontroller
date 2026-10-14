#!/usr/bin/env python3
"""PoC TokenReview authentication webhook.

Modes
  allow-all (phase 1, default): any non-empty bearer token is accepted and
            mapped to a fixed identity (--username / --uid / --groups).
  db        (phase 2, --db PATH --cluster-id ID): SHA-256 lookup in the token
            store; rejects unknown / revoked / expired / cluster-mismatch.

mTLS (phase 2, optional): --client-ca PATH makes the server verify the
apiserver's client certificate; --client-cert-required rejects connections
without one (otherwise the peer CN is only logged).

Logs never contain the plaintext token, only the first 12 hex chars of its
SHA-256. Standard library only. HTTPS is mandatory because kube-apiserver's
webhook client refuses plain HTTP.
"""
import argparse
import json
import logging
import os
import ssl
import sys
import time
from collections import namedtuple
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tokenstore as ts  # noqa: E402

Decision = namedtuple("Decision", "ok username uid groups error")

LOG = logging.getLogger("poc-authn")


def allow_all(token, audiences, srv):
    """Phase-1 verifier: accept every non-empty token."""
    if not token:
        return Decision(False, None, None, None, "empty token")
    cfg = srv.cfg
    return Decision(True, cfg.username, cfg.uid, cfg.groups, None)


def db_verify(token, audiences, srv):
    """Phase-2 verifier: hash lookup. `audiences` is not used to tell
    clusters apart (every apiserver here sends the same implicit audience);
    the token row's `cluster` is compared with --cluster-id instead."""
    ok, reason, row = ts.verify(srv.db, token, srv.cfg.cluster_id)
    if not ok:
        return Decision(False, None, None, None, reason)
    ident = ts.identity(row)
    return Decision(True, ident["username"], ident["uid"], ident["groups"], None)


class Handler(BaseHTTPRequestHandler):
    server_version = "poc-authn/0.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route http.server's own messages to our log
        LOG.info("http %s %s", self.client_address[0], fmt % args)

    def setup(self):
        # Handshake here (not in accept) so a failed client-cert check raises in
        # this handler thread and reaches Server.handle_error with the address.
        self.connection = self.request
        self.connection.do_handshake()
        super().setup()

    def _drain(self):
        """Always consume the request body so leftover bytes are never parsed
        as the next request on a keep-alive connection."""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _route(self):
        return urlsplit(self.path).path

    def _peer_cn(self):
        try:
            cert = self.connection.getpeercert()
        except (AttributeError, ValueError):
            return None
        if not cert:
            return None
        for rdn in cert.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    return v
        return "?"

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._drain()
        if self._route() == "/healthz":
            cfg = self.server.cfg
            body = {"status": "ok", "mode": cfg.mode, "mtls": cfg.mtls, "peer_cn": self._peer_cn()}
            if cfg.mode == "db":
                body["cluster_id"] = cfg.cluster_id
                body["db"] = cfg.db
            else:
                body["username"] = cfg.username
                body["groups"] = cfg.groups
            self._send(200, body)
            return
        LOG.warning("addr=%s 404 GET %s", self.client_address[0], self.path)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        t0 = time.monotonic()
        raw = self._drain()
        if self._route() != "/authenticate":
            LOG.warning("addr=%s 404 POST %s ua=%s", self.client_address[0], self.path,
                        self.headers.get("User-Agent"))
            self._send(404, {"error": "not found"})
            return
        try:
            review = json.loads(raw or b"{}")
        except ValueError:
            LOG.warning("addr=%s bad json body", self.client_address[0])
            self._send(400, {"error": "invalid json"})
            return

        spec = review.get("spec") or {}
        token = spec.get("token") or ""
        audiences = spec.get("audiences") or []
        d = self.server.verify(token, audiences, self.server)

        status = {"authenticated": bool(d.ok)}
        if d.ok:
            status["user"] = {"username": d.username, "uid": d.uid, "groups": list(d.groups)}
            if audiences:
                # Echo the requested audiences so the apiserver's audience
                # check passes. TokenReview carries no TTL; cache life is
                # decided solely by --authentication-token-webhook-cache-ttl.
                status["audiences"] = list(audiences)
        else:
            status["error"] = d.error or "unauthenticated"

        resp = {
            "apiVersion": review.get("apiVersion") or "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "status": status,
        }
        ms = (time.monotonic() - t0) * 1000
        LOG.info("addr=%s peer_cn=%s hash=%s len=%d auds=%s %s reason=%s user=%s groups=%s ms=%.2f",
                 self.client_address[0], self._peer_cn(), ts.hash_prefix(token) if token else "-",
                 len(token), audiences, "ok" if d.ok else "deny", d.error or "-",
                 d.username, list(d.groups) if d.groups else None, ms)
        self._send(200, resp)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # TLS handshake failures (no / untrusted client cert) land here: log one
        # line instead of a traceback so P9 evidence is greppable.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionResetError, BrokenPipeError)):
            LOG.warning("addr=%s tls/conn error: %s", client_address[0], exc)
        else:
            super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    # phase-1 identity (allow-all mode)
    ap.add_argument("--username", default="poc-user")
    ap.add_argument("--uid", default="poc-1")
    ap.add_argument("--groups", default="poc-tenant-a", help="comma separated")
    # phase-2 token store
    ap.add_argument("--db", help="sqlite token store; enables db mode")
    ap.add_argument("--cluster-id", default="gasandevcontrol")
    # phase-2 mTLS
    ap.add_argument("--client-ca", help="CA bundle for verifying the apiserver's client cert")
    ap.add_argument("--client-cert-required", action="store_true")
    ap.add_argument("--log", help="log file (also logs to stderr)")
    cfg = ap.parse_args()
    cfg.groups = [g for g in cfg.groups.split(",") if g]
    cfg.mode = "db" if cfg.db else "allow-all"
    cfg.mtls = "off" if not cfg.client_ca else ("required" if cfg.client_cert_required else "optional")

    handlers = [logging.StreamHandler(sys.stderr)]
    if cfg.log:
        handlers.append(logging.FileHandler(cfg.log))
    logging.Formatter.converter = time.gmtime  # UTC, same as token_audit.jsonl / apiserver audit.log
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)sZ %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S")

    srv = Server((cfg.bind, cfg.port), Handler)
    srv.cfg = cfg
    if cfg.mode == "db":
        srv.db = ts.open_db(cfg.db)
        srv.verify = db_verify
    else:
        srv.db = None
        srv.verify = allow_all
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cfg.cert, cfg.key)
    if cfg.client_ca:
        ctx.load_verify_locations(cfg.client_ca)
        ctx.verify_mode = ssl.CERT_REQUIRED if cfg.client_cert_required else ssl.CERT_OPTIONAL
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    LOG.info("[start] mode=%s mtls=%s bind=%s:%d cluster_id=%s db=%s allow_all_identity=%s/%s",
             cfg.mode, cfg.mtls, cfg.bind, cfg.port, cfg.cluster_id, cfg.db, cfg.username, cfg.groups)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    LOG.info("[stop]")


if __name__ == "__main__":
    main()
