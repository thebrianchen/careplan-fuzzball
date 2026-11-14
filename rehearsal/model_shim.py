"""REHEARSAL ONLY: stands in for the on-box vLLM server (http://llm:8000/v1) by forwarding
OpenAI-compatible requests to OpenRouter with our key. On the real box 2 this container is vLLM
and has no internet route; here it is the only box 2 container with egress, so the planner itself
can stay on an internal network exactly as it will in the cluster.
"""
from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

UPSTREAM = os.environ.get("SHIM_UPSTREAM", "https://openrouter.ai/api/v1")
KEY = os.environ.get("OPENROUTER_API_KEY", "")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _relay(self, method: str):
        if not self.path.startswith("/v1/"):
            self.send_response(404)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else None
        r = requests.request(method, UPSTREAM.rstrip("/") + self.path[3:], data=body, timeout=600,
                             headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
        self.send_response(r.status_code)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("SHIM_PORT", "8000"))), H)
    print(f"model shim on :{srv.server_address[1]} -> {UPSTREAM}", file=sys.stderr)
    srv.serve_forever()
    sys.exit(0)
