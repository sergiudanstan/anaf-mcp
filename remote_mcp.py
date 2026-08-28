#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transport MCP Streamable HTTP, stateless si fara autentificare.

Ruleaza local pentru test cu:
    python3 remote_mcp.py --port 8765

Endpoint MCP:
    http://127.0.0.1:8765/mcp

Fisierul foloseste doar biblioteca standard si poate fi importat de runtime-ul
Python Vercel prin api/index.py.
"""

import argparse
import json
import lzma
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


# Functiile serverless au doar /tmp drept spatiu de lucru persistent pe durata
# instantei. Variabilele trebuie setate inainte de importul anaf_mcp, deoarece
# acesta isi calculeaza caile cache-ului la import.
if os.environ.get("VERCEL"):
    os.environ.setdefault("ANAF_MCP_CACHE_DIR", "/tmp/anaf-mcp-cache")
    os.environ.setdefault("ANAF_MCP_INDEX_DB", "/tmp/anaf-remote-index.sqlite")

import anaf_mcp  # noqa: E402  (import deliberat dupa configurarea mediului)


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_INDEX_ARCHIVE = os.environ.get("ANAF_MCP_REMOTE_INDEX_XZ") or os.path.join(
    ROOT_DIR, "data", "anaf-remote-index.sqlite.xz"
)
REMOTE_INDEX_DB = os.environ.get("ANAF_MCP_INDEX_DB") or os.path.join(
    anaf_mcp.CACHE_DIR, "anaf-remote-index.sqlite"
)
REMOTE_INDEX_MAX_BYTES = 350 * 1024 * 1024
REMOTE_INDEX_TOOLS = {"search_company", "top_firme"}
MAX_REQUEST_BYTES = 1024 * 1024
_index_lock = threading.Lock()


def ensure_remote_index():
    """Dezarhiveaza atomic indexul compact numai la primul apel care-l cere."""
    if anaf_mcp.index_db() is not None:
        return
    if not os.path.exists(REMOTE_INDEX_ARCHIVE):
        return

    with _index_lock:
        if os.path.exists(REMOTE_INDEX_DB):
            os.environ["ANAF_MCP_INDEX_DB"] = REMOTE_INDEX_DB
            return
        os.makedirs(os.path.dirname(REMOTE_INDEX_DB), exist_ok=True)
        tmp = REMOTE_INDEX_DB + ".tmp"
        written = 0
        try:
            with lzma.open(REMOTE_INDEX_ARCHIVE, "rb") as source, open(tmp, "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > REMOTE_INDEX_MAX_BYTES:
                        raise RuntimeError("Indexul remote decomprimat depaseste limita de siguranta")
                    target.write(chunk)
            os.replace(tmp, REMOTE_INDEX_DB)
            os.environ["ANAF_MCP_INDEX_DB"] = REMOTE_INDEX_DB
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def _needs_remote_index(request):
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return False
    params = request.get("params") or {}
    return params.get("name") in REMOTE_INDEX_TOOLS


def _handle_one(request):
    if _needs_remote_index(request):
        ensure_remote_index()
    return anaf_mcp.handle(request)


class RemoteMCPHandler(BaseHTTPRequestHandler):
    """Endpoint HTTP stateless: fiecare POST contine un mesaj JSON-RPC MCP."""

    server_version = "anaf-mcp/%s" % anaf_mcp.SERVER_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, HEAD, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Accept, Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id",
        )
        self.send_header("Access-Control-Expose-Headers", "Mcp-Protocol-Version")

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Mcp-Protocol-Version", anaf_mcp.PROTOCOL_VERSION)
        self._cors_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, value, extra=None):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, extra=extra)

    def _jsonrpc_error(self, status, code, message, request_id=None):
        self._json(status, {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def _route(self):
        return urlsplit(self.path).path.rstrip("/") or "/"

    def do_OPTIONS(self):
        self._send(204, content_type="text/plain; charset=utf-8")

    def do_HEAD(self):
        if self._route() in ("/health", "/api/health"):
            self._send(200)
        else:
            self._send(405, content_type="text/plain; charset=utf-8", extra={"Allow": "POST"})

    def do_GET(self):
        if self._route() in ("/health", "/api/health"):
            self._json(200, {
                "status": "ok",
                "name": anaf_mcp.SERVER_NAME,
                "version": anaf_mcp.SERVER_VERSION,
                "transport": "streamable-http",
                "authentication": "none",
                "mcp_endpoint": "/mcp",
            })
            return
        # Serverul este stateless si nu deschide un flux SSE prin GET.
        self._send(405, content_type="text/plain; charset=utf-8", extra={"Allow": "POST"})

    def do_DELETE(self):
        self._send(405, content_type="text/plain; charset=utf-8", extra={"Allow": "POST"})

    def do_POST(self):
        route = self._route()
        # /api/index este ruta nativa Vercel; /mcp este ruta publica rescrisa.
        if route not in ("/mcp", "/api", "/api/index"):
            self._jsonrpc_error(404, -32601, "Endpoint MCP negasit")
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._jsonrpc_error(400, -32700, "Corpul cererii lipseste")
            return
        if length > MAX_REQUEST_BYTES:
            self._jsonrpc_error(413, -32600, "Cererea depaseste 1 MB")
            return

        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._jsonrpc_error(400, -32700, "JSON invalid")
            return

        try:
            if isinstance(request, list):
                if not request:
                    self._jsonrpc_error(400, -32600, "Batch JSON-RPC gol")
                    return
                responses = [response for response in (_handle_one(item) for item in request) if response]
                response = responses
            elif isinstance(request, dict):
                response = _handle_one(request)
            else:
                self._jsonrpc_error(400, -32600, "Mesajul JSON-RPC trebuie sa fie obiect sau lista")
                return
        except Exception as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            self._jsonrpc_error(500, -32603, "Eroare interna: %s" % exc, request_id)
            return

        # Notificarile JSON-RPC nu au raspuns.
        if response is None or response == []:
            self._send(202, content_type="text/plain; charset=utf-8")
            return

        accept = (self.headers.get("Accept") or "application/json").lower()
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        if "application/json" not in accept and "text/event-stream" in accept:
            body = ("event: message\ndata: %s\n\n" % payload).encode("utf-8")
            self._send(200, body, "text/event-stream; charset=utf-8")
        else:
            self._send(200, payload.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="anaf-mcp prin Streamable HTTP")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RemoteMCPHandler)
    print("anaf-mcp remote: http://%s:%d/mcp" % (args.host, args.port), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
