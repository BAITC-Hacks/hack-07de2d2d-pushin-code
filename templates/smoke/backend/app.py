"""Smoke-test backend: no dependencies, just enough to prove the deploy path works.
Replace with the real service on the day — the only contract it keeps is GET /health."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path.startswith("/api/"):
            self._json(
                200,
                {
                    "smoke": True,
                    "path": self.path,
                    "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
                },
            )
        else:
            self._json(404, {"error": "not found", "path": self.path})

    def log_message(self, fmt, *args):
        print("backend:", fmt % args, flush=True)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
