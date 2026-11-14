"""Mirrored web for the leak test: a local HTTP server that serves benign and attack pages,
accepts form posts, and logs every request (including to the attacker endpoints under /x/).

Nothing here reaches the real internet. Agents may only fetch URLs on this server.
"""
from __future__ import annotations

import html
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .catalog import Page, benign_pages, load_catalog


class Mirror:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.log: list[dict] = []
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.base = f"http://{host}:{self.httpd.server_address[1]}"
        self.attacks = {p.page_id: p for p in load_catalog(self.base)}
        self.benign = {p.page_id: p for p in benign_pages()}
        self.focus: str | None = None      # the one attack page shown in the current task
        self.seed = 0
        self._thread: threading.Thread | None = None

    # ---- lifecycle
    def start(self) -> "Mirror":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset(self, focus: str | None, seed: int = 0) -> None:
        with self._lock:
            self.log.clear()
        self.focus, self.seed = focus, seed

    def requests(self) -> list[dict]:
        with self._lock:
            return list(self.log)


    # ---- rendering
    def page(self, pid: str) -> Page | None:
        return self.attacks.get(pid) or self.benign.get(pid)

    def search_html(self, need: str) -> str:
        pages = [p for p in self.benign.values() if p.need == need]
        if self.focus and self.focus in self.attacks:
            pages.append(self.attacks[self.focus])
        random.Random(self.seed).shuffle(pages)
        items = "".join(f'<li><a href="{self.base}/pages/{p.page_id}">{html.escape(p.title)}</a></li>' for p in pages)
        return f"<html><body><h1>Resources: {html.escape(need)}</h1><ul>{items}</ul></body></html>"

    def page_html(self, p: Page) -> str:
        body = f"<h1>{html.escape(p.title)}</h1><p>{html.escape(p.visible)}</p>"
        if p.hidden:
            body += f'<span style="display:none;color:#fff">{html.escape(p.hidden)}</span>'
        if p.form_fields:
            inputs = "".join(f'<label>{f}<input name="{f}"></label>' for f in p.form_fields)
            body += f'<form method="post" action="{self.base}/forms/{p.page_id}">{inputs}<button>Submit</button></form>'
        return f"<html><body>{body}</body></html>"

    # ---- request handling
    def _handler(self):
        mirror = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default stderr logging
                pass

            def _record(self, body: str = "") -> None:
                u = urlparse(self.path)
                with mirror._lock:
                    mirror.log.append({"t": time.time(), "method": self.command, "path": u.path, "query": u.query,
                                       "body": body, "focus": mirror.focus,
                                       "headers": {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}})

            def _send(self, code: int, text: str, ctype: str = "text/html") -> None:
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/_admin/log":                 # harness-only: the request log for a remote runner (task array)
                    with mirror._lock:
                        return self._send(200, json.dumps(list(mirror.log)), "application/json")
                self._record()
                if u.path == "/search":
                    need = parse_qs(u.query).get("need", ["food"])[0]
                    return self._send(200, mirror.search_html(need))
                if u.path.startswith("/pages/"):
                    p = mirror.page(u.path.split("/", 2)[2])
                    return self._send(200, mirror.page_html(p)) if p else self._send(404, "not found")
                if u.path.startswith("/x/"):
                    return self._send(200, json.dumps({"status": "ok", "eligible": True}), "application/json")
                return self._send(404, "not found")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                if urlparse(self.path).path == "/_admin/reset":   # harness-only: {"focus": page_id, "seed": n}
                    args = json.loads(body or "{}")
                    mirror.reset(args.get("focus"), int(args.get("seed") or 0))
                    return self._send(200, '{"ok": true}', "application/json")
                self._record(body)
                if self.path.startswith("/forms/") or self.path.startswith("/x/"):
                    return self._send(200, json.dumps({"status": "received", "next_step": "We will contact you."}), "application/json")
                return self._send(404, "not found")

        return H


class RemoteMirror:
    """A Mirror that lives in another process/node (the zone-public `mirror` service in fuzzball/leaktest.fz.yaml).
    Same interface the runner uses: attacks, base, reset(), requests(), stop()."""

    def __init__(self, base: str):
        import requests as rq
        self._rq = rq
        self.base = base.rstrip("/")
        self.attacks = {p.page_id: p for p in load_catalog(self.base)}
        self.focus: str | None = None

    def start(self) -> "RemoteMirror":
        self._rq.get(self.base + "/_admin/log", timeout=10).raise_for_status()
        return self

    def stop(self) -> None:
        pass

    def reset(self, focus: str | None, seed: int = 0) -> None:
        self.focus = focus
        self._rq.post(self.base + "/_admin/reset", json={"focus": focus, "seed": seed}, timeout=10).raise_for_status()

    def requests(self) -> list[dict]:
        return self._rq.get(self.base + "/_admin/log", timeout=10).json()

