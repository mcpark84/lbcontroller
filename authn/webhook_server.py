#!/usr/bin/env python3
"""PoC TokenReview authentication webhook.

Phase 1: allow-all. Any non-empty bearer token is accepted and mapped to a
fixed identity (--username / --uid / --groups). Phase 2 replaces
`authenticate()` with a hash lookup against the token store.

Standard library only. HTTPS is mandatory because kube-apiserver's webhook
client refuses plain HTTP.
"""
import argparse
import json
import logging
import ssl
import sys
import time
from collections import namedtuple
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Decision = namedtuple("Decision", "ok username uid groups error")

LOG = logging.getLogger("poc-authn")


def allow_all(token, audiences, cfg):
    """Phase-1 verifier: accept every non-empty token."""
    if not token:
        return Decision(False, None, None, None, "empty token")
    return Decision(True, cfg.username, cfg.uid, cfg.groups, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "poc-authn/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route http.server's own messages to our log
        LOG.info("http %s %s", self.client_address[0], fmt % args)

    def _drain(self):
        """Always consume the request body so leftover bytes are never parsed
        as the next request on a keep-alive connection."""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _route(self):
        return urlsplit(self.path).path

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
            self._send(200, {"status": "ok", "mode": self.server.cfg.mode,
                             "username": self.server.cfg.username,
                             "groups": self.server.cfg.groups})
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
        d = self.server.verify(token, audiences, self.server.cfg)

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
        LOG.info("addr=%s token=%s... len=%d auds=%s authenticated=%s user=%s groups=%s ms=%.2f",
                 self.client_address[0], token[:8], len(token), audiences, d.ok,
                 d.username, list(d.groups) if d.groups else None, ms)
        self._send(200, resp)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--username", default="poc-user")
    ap.add_argument("--uid", default="poc-1")
    ap.add_argument("--groups", default="poc-tenant-a", help="comma separated")
    ap.add_argument("--log", help="log file (also logs to stderr)")
    cfg = ap.parse_args()
    cfg.groups = [g for g in cfg.groups.split(",") if g]
    cfg.mode = "allow-all"

    handlers = [logging.StreamHandler(sys.stderr)]
    if cfg.log:
        handlers.append(logging.FileHandler(cfg.log))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S")

    srv = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    srv.cfg = cfg
    srv.verify = allow_all
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cfg.cert, cfg.key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    LOG.info("[start] mode=%s bind=%s:%d username=%s uid=%s groups=%s",
             cfg.mode, cfg.bind, cfg.port, cfg.username, cfg.uid, cfg.groups)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    LOG.info("[stop]")


if __name__ == "__main__":
    main()
