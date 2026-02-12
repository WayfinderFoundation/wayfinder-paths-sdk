#!/usr/bin/env python3
"""Local JSON-RPC mock that always returns HTTP 429 for POST requests."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Mock429Handler(BaseHTTPRequestHandler):
    retry_after: str = "30"

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(content_length) if content_length > 0 else b""
        request_id = 1
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and "id" in parsed:
                request_id = parsed["id"]
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            # Keep default request_id for malformed request payloads.
            request_id = 1

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": 429, "message": "Too Many Requests"},
        }
        response = json.dumps(payload).encode("utf-8")

        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", self.retry_after)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt: str, *args) -> None:
        print(f"mock429: {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18545)
    parser.add_argument("--retry-after", default="30", dest="retry_after")
    args = parser.parse_args()

    _Mock429Handler.retry_after = str(args.retry_after)
    server = HTTPServer((args.host, args.port), _Mock429Handler)
    print(f"mock429 listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
