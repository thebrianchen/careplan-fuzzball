"""REHEARSAL ONLY: a stand-in for Fuzzball's endpoint proxy, so the boundary can be rehearsed before
we get Fuzzball access (Friday night). It does what the Fuzzball docs say the real proxy does:
authenticates the caller's bearer token, strips it, and forwards a signed X-Fuzzball-Caller-Identity
JWT (ES256, aud = endpoint:<id>, 2-minute expiry). Verification keys are published as a JWKS file,
which the gateway reads from the path Fuzzball mounts (/run/fuzzball-substrate/trusted-certs/).

Routes:   /gateway/*  -> box 1 gateway (callers allowed: svc-private)
          /card/*, /check/*, /  -> box 2 card backend (callers allowed: clinicians)
Env:  PROXY_CALLERS   JSON {token: {"sub", "account_id", "email", "routes": ["gateway"|"card"]}}
      PROXY_ROUTES    JSON {"gateway": {"upstream": "http://gateway:8090", "endpoint_id": "..."}, "card": {...}}
      PROXY_JWKS_OUT  where to write the public JWKS (shared volume)
Browsers may pass the token as ?token=... on GET (rehearsal convenience).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

HEADER = "X-Fuzzball-Caller-Identity"


class Proxy:
    def __init__(self, callers: dict, routes: dict, jwks_out: Path):
        self.callers, self.routes = callers, routes
        self.key, self.kid = ec.generate_private_key(ec.SECP256R1()), f"rehearsal-{uuid.uuid4().hex[:8]}"
        jwk = ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True) | {"kid": self.kid, "alg": "ES256", "use": "sig"}
        jwks_out.parent.mkdir(parents=True, exist_ok=True)
        jwks_out.write_text(json.dumps({"keys": [jwk]}))
        self.s = requests.Session()

    def route_for(self, path: str) -> tuple[str, str] | None:
        if path.startswith("/gateway/") or path == "/gateway":
            return "gateway", path[len("/gateway"):] or "/"
        if path == "/" or path.startswith(("/card/", "/check/", "/healthz")):
            return "card", path
        return None

    def assertion(self, caller: dict, endpoint_id: str) -> str:
        now = int(time.time())
        claims = {"aud": f"endpoint:{endpoint_id}", "sub": caller["sub"], "email": caller.get("email", ""),
                  "account_id": caller.get("account_id", ""), "endpoint_id": endpoint_id, "iat": now, "exp": now + 120,
                  "iss": "https://rehearsal.invalid/issuer", "type": "Bearer"}
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


def make_server(p: Proxy, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forward(self, method: str):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            r = p.route_for(u.path)
            if r is None:
                return self._send(404, '{"error": "no such endpoint"}')
            name, sub_path = r
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else (q.get("token", [""])[0] if method == "GET" else "")
            caller = p.callers.get(token)
            if caller is None:
                print(json.dumps({"event": "denied", "route": name, "reason": "no or unknown token"}), flush=True)
                return self._send(401, '{"error": "authentication required"}')
            if name not in caller.get("routes", []):
                print(json.dumps({"event": "denied", "route": name, "sub": caller["sub"], "reason": "not allowed"}), flush=True)
                return self._send(403, '{"error": "caller may not use this endpoint"}')
            route = p.routes[name]
            fwd_q = {k: v for k, v in q.items() if k != "token"}
            target = route["upstream"].rstrip("/") + sub_path + (("?" + urlencode(fwd_q, doseq=True)) if fwd_q else "")
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("authorization", "host", "content-length", HEADER.lower(), "connection")}
            headers[HEADER] = p.assertion(caller, route["endpoint_id"])     # the caller's token never goes upstream
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else None
            try:
                up = p.s.request(method, target, headers=headers, data=body, timeout=600)
            except requests.RequestException as ex:
                return self._send(502, json.dumps({"error": f"upstream unreachable: {type(ex).__name__}"}))
            print(json.dumps({"event": "proxied", "route": name, "sub": caller["sub"], "status": up.status_code}), flush=True)
            self._send(up.status_code, up.content, up.headers.get("Content-Type", "application/octet-stream"))

        def do_GET(self):
            self._forward("GET")

        def do_POST(self):
            self._forward("POST")

    return ThreadingHTTPServer(("0.0.0.0", port), H)


def main() -> int:
    p = Proxy(json.loads(os.environ["PROXY_CALLERS"]), json.loads(os.environ["PROXY_ROUTES"]),
              Path(os.environ.get("PROXY_JWKS_OUT", "/keys/signing-keys.json")))
    srv = make_server(p, int(os.environ.get("PROXY_PORT", "8443")))
    print(f"rehearsal endpoint proxy on :{srv.server_address[1]} (kid {p.kid})", file=sys.stderr)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
