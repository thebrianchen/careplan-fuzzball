"""The one crossing: box 1's gateway service, and box 2's client for it.

Box 2's planner calls find_resources(tract_id, need, days, start, end) through the Fuzzball
endpoint proxy. The proxy authenticates the caller, strips its token, and forwards a signed
X-Fuzzball-Caller-Identity JWT. This service:
  1. verifies that JWT exactly as the Fuzzball docs say (ES256, key from the node's local JWKS
     file, aud bound to this endpoint, exp/sub/aud required, never fetch keys from iss);
  2. admits only allowlisted callers (the zone-private service user or group); fails closed;
  3. validates the body against the same schema the planner's tool uses (no free text);
  4. rate-limits per caller (the covert-channel mitigation) and logs every decision as JSONL.
Rejected bodies are never echoed into logs or responses: a hijacked caller could have stuffed
patient data into a field, and box 1 must never hold it.

    python -m careplan.gateway            # serves on GATEWAY_PORT (default 8090)
"""
from __future__ import annotations

import json
import re
import os
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import jsonschema
import jwt
import requests

from .data import ResourceTable, TractTable
from .tools import TOOL_SPECS, ToolError

HEADER = "X-Fuzzball-Caller-Identity"
DEFAULT_KEYS = "/run/fuzzball-substrate/trusted-certs/signing-keys.json"
REQUEST_SCHEMA = TOOL_SPECS["find_resources"]["function"]["parameters"]


class Denied(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class CallerVerifier:
    def __init__(self, keys_path: str, audiences: list[str], allowed_subs: set[str], allowed_accounts: set[str]):
        self.keys_path, self.audiences = keys_path, audiences
        self.allowed_subs, self.allowed_accounts = allowed_subs, allowed_accounts

    @classmethod
    def from_env(cls) -> "CallerVerifier":
        auds = [f"endpoint:{os.environ[n]}" for n in ("FB_ENDPOINT_ID", "FB_ENDPOINT_REPLICA_ID") if os.environ.get(n)]
        split = lambda name: {v for v in os.environ.get(name, "").split(",") if v}
        return cls(os.environ.get("GATEWAY_KEYS_PATH", DEFAULT_KEYS), auds,
                   split("GATEWAY_ALLOWED_SUBS"), split("GATEWAY_ALLOWED_ACCOUNTS"))

    def verify(self, raw: str | None) -> dict:
        if not raw:
            raise Denied(401, "no caller identity")
        if not self.audiences or not (self.allowed_subs or self.allowed_accounts):
            raise Denied(503, "gateway not configured (no audience or allowlist)")
        try:
            keys = jwt.PyJWKSet.from_dict(json.loads(Path(self.keys_path).read_text()))
            kid = jwt.get_unverified_header(raw).get("kid")
            key = next((k.key for k in keys.keys if k.key_id == kid), None)
            if key is None:
                raise Denied(401, "unknown signing key")
            claims = jwt.decode(raw, key, algorithms=["ES256"], audience=self.audiences,
                                options={"require": ["exp", "sub", "aud"]})
        except Denied:
            raise
        except jwt.ExpiredSignatureError:
            raise Denied(401, "identity expired") from None
        except jwt.InvalidAudienceError:
            raise Denied(401, "identity not issued for this endpoint") from None
        except (jwt.PyJWTError, ValueError, OSError) as e:
            raise Denied(401, f"identity invalid: {type(e).__name__}") from None
        if claims["sub"] not in self.allowed_subs and claims.get("account_id") not in self.allowed_accounts:
            raise Denied(403, "caller not allowed")
        return claims


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute, self.seen = per_minute, defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.seen[who]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def validate_request(body: bytes) -> dict:
    try:
        req = json.loads(body or b"{}")
        jsonschema.validate(req, REQUEST_SCHEMA)
    except json.JSONDecodeError:
        raise Denied(400, "body is not JSON") from None
    except jsonschema.ValidationError as e:
        # Name the field and the rule, never the value.
        where = "/".join(str(p) for p in e.absolute_path) or "(body)"
        raise Denied(400, f"schema violation at {where}: {e.validator}") from None
    return req


def make_server(tracts: TractTable, resources: ResourceTable, verifier: CallerVerifier, log: Callable[[dict], None],
                host: str = "0.0.0.0", port: int = 8090, per_minute: int = 30) -> ThreadingHTTPServer:
    limiter = RateLimiter(per_minute)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"ok": True}) if self.path == "/healthz" else self._send(404, {"error": "not found"})

        def do_POST(self):
            entry: dict = {"t": round(time.time(), 3), "path": self.path}
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                if self.path != "/find_resources":
                    raise Denied(404, "not found")
                claims = verifier.verify(self.headers.get(HEADER))
                entry |= {"sub": claims["sub"], "account_id": claims.get("account_id"), "email": claims.get("email")}
                if not limiter.allow(claims["sub"]):
                    raise Denied(429, "rate limited")
                req = validate_request(body)
                entry["request"] = req
                row = tracts.get(req["tract_id"])
                if row is None or row.get("intpt_lat") is None:
                    raise Denied(404, "unknown tract")
                out = resources.search(row["intpt_lat"], row["intpt_lon"], req["need"], req.get("days"),
                                       req.get("start"), req.get("end"))
                entry |= {"decision": "allowed", "results": len(out)}
                log(entry)
                self._send(200, {"records": out})
            except Denied as d:
                entry |= {"decision": "denied", "status": d.status, "reason": d.reason}
                log(entry)
                self._send(d.status, {"error": d.reason})

    return ThreadingHTTPServer((host, port), H)


PII_PATTERNS = {
    "mrn": re.compile(r"\bMRN-?[A-Z2-7]{6,}\b", re.I),
    "date": re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b"),                 # a DOB-shaped date (requests carry only HH:MM)
    "phone": re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    "name_like": re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"),                   # two capitalized words: a name, not an enum
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}


def outbound_pii(obj) -> str | None:
    """Name of the first identifier-shaped value found anywhere in an outbound request, else None."""
    text = json.dumps(obj, ensure_ascii=False)
    for name, pat in PII_PATTERNS.items():
        if pat.search(text):
            return name
    return None


def http_gateway(url: str, token: str | None = None, timeout: int = 30,
                 headers: dict | None = None) -> Callable[[dict], list[dict]]:
    """Box 2's side: a `gateway=` callable for Toolbox that calls the service over HTTP."""
    s = requests.Session()
    s.headers.update(headers or {})
    if token:
        s.headers["Authorization"] = f"Bearer {token}"

    def call(request: dict) -> list[dict]:
        body = {k: v for k, v in request.items() if v is not None}
        # Default-safe, second layer behind the schema: refuse to send anything that even looks like an identifier.
        # The schema already rejects free text; this catches a future field or a bug before it leaves the box.
        if outbound_pii(body):
            raise ToolError(f"gateway request refused on the private side: looks like an identifier ({outbound_pii(body)})")
        r = s.post(url.rstrip("/") + "/find_resources", json=body, timeout=timeout)
        if r.status_code != 200:
            raise ToolError(f"gateway refused ({r.status_code}): {r.json().get('error', '')}")
        return r.json()["records"]
    return call


def main() -> int:
    log_path = os.environ.get("GATEWAY_LOG")
    sink = open(log_path, "a") if log_path else sys.stdout

    def log(entry: dict) -> None:
        sink.write(json.dumps(entry) + "\n")
        sink.flush()

    srv = make_server(TractTable.load(Path(os.environ["GATEWAY_TRACT_TABLE"]) if os.environ.get("GATEWAY_TRACT_TABLE") else None),
                      ResourceTable.load(Path(os.environ["GATEWAY_RESOURCE_TABLE"]) if os.environ.get("GATEWAY_RESOURCE_TABLE") else None),
                      CallerVerifier.from_env(), log, port=int(os.environ.get("GATEWAY_PORT", "8090")),
                      per_minute=int(os.environ.get("GATEWAY_RATE_PER_MINUTE", "30")))
    print(f"gateway listening on :{srv.server_address[1]}", file=sys.stderr)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
