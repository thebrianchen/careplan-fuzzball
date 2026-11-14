"""Serve the case review packets and log submitted reviews. Static files from reports/case_review*/, plus
POST /submit {case_id, answers, text} -> appended to reviews.jsonl (timestamped, with the reviewer's IP).
POST /prep {answers} -> appended to the pitch-prep log (kept outside the served folder); GET /prep/latest prefills the prep page.

    python scripts/review_server.py --port 8095 --root reports --log reports/reviews.jsonl
Open http://<host>:8095/case_review/index.html (dev) or /case_review_test/index.html (held-out).
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--root", type=Path, default=Path("reports"))
    ap.add_argument("--log", type=Path, default=Path("reports/reviews.jsonl"))
    ap.add_argument("--prep-log", type=Path, default=Path("runs/pitch_prep.jsonl"))
    a = ap.parse_args()
    root, log, prep = a.root.resolve(), a.log.resolve(), a.prep_log.resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    prep.parent.mkdir(parents=True, exist_ok=True)

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(root), **kw)

        def log_message(self, fmt, *args):
            print(time.strftime("%H:%M:%S"), self.client_address[0], fmt % args, flush=True)

        def do_GET(self):
            if self.path in ("", "/"):
                self.send_response(302); self.send_header("Location", "/case_review/index.html"); self.end_headers(); return
            if self.path.startswith("/answers/"):          # latest submission for a case, to prefill the form
                cid = self.path.rsplit("/", 1)[1]
                latest = None
                if log.exists():
                    for line in log.read_text().splitlines():
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if r.get("case_id") == cid:
                            latest = r
                data = json.dumps(latest or {}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if self.path == "/prep/latest":
                rows = [l for l in prep.read_text().splitlines() if l.strip()] if prep.exists() else []
                data = (rows[-1] if rows else "{}").encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if self.path == "/reviews":
                body = log.read_text() if log.exists() else ""
                data = body.encode(); self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            return super().do_GET()

        def do_POST(self):
            if self.path not in ("/submit", "/prep"):
                self.send_response(404); self.end_headers(); return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self.send_response(400); self.end_headers(); return
            if self.path == "/prep":
                answers = {str(k)[:40]: str(v)[:6000] for k, v in (body.get("answers") or {}).items()}
                with prep.open("a") as fh:
                    fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "answers": answers}, ensure_ascii=False) + "\n")
                print(json.dumps({"prep": len(answers), "chars": sum(len(v) for v in answers.values())}), flush=True)
                data = b'{"ok": true}'; self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": self.client_address[0],
                   "case_id": body.get("case_id"), "answers": body.get("answers"), "text": (body.get("text") or "")[:20000]}
            with log.open("a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps({"review": row["case_id"], "from": row["from"], "n_answers": len(row["answers"] or {})}), flush=True)
            data = b'{"ok": true}'; self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    print(f"review server on :{a.port}, root {root}, log {log}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
