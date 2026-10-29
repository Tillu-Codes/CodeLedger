"""Vercel function: ``GET /api/entries``.

Serves the precomputed snapshot from ``api/data/entries.json``
(entries + aggregate counts, same shape as ``codeledger.webui.api_entries``).
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "entries.json"


def _respond(h: BaseHTTPRequestHandler, body: bytes, status: int = 200) -> None:
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    server_version = "CodeLedger"
    sys_version = ""

    def do_GET(self) -> None:
        try:
            _respond(self, _DATA_PATH.read_bytes())
        except OSError:
            _respond(self, b'{"error": "snapshot data missing"}', 500)

    def do_HEAD(self) -> None:
        _respond(self, b"")

    def do_POST(self) -> None:
        _respond(self, b'{"error": "the CodeLedger UI is read-only"}', 405)

    def log_message(self, *args: object) -> None:
        """Keep the function logs calm."""
