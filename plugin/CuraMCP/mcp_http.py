# Cura MCP — minimal MCP-over-HTTP server (Python standard library only).
#
# This is a *downstream* MCP server: the mcp-proxy aggregator connects to it and
# fronts it to the real client. It speaks JSON-RPC 2.0 over HTTP POST and replies
# with application/json (the proxy accepts either JSON or SSE, so no SSE needed).
#
# Security:
#   * binds strictly to the configured host (127.0.0.1 by default);
#   * rejects cross-origin browser requests (DNS-rebinding guard);
#   * optional shared bearer token;
#   * exposes only the fixed tool set; there is no code-exec surface here.

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from UM.Logger import Logger

PROTOCOL_VERSION = "2025-03-26"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CuraMCP/1.0"

    # Silence the default stderr access log.
    def log_message(self, *args):
        pass

    # -- guards ---------------------------------------------------------- #

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client (the proxy sends no Origin)
        return (origin == "null"
                or origin.startswith("http://127.0.0.1")
                or origin.startswith("http://localhost")
                or origin.startswith("https://127.0.0.1")
                or origin.startswith("https://localhost"))

    def _auth_ok(self):
        token = self.server.token
        if not token:
            return True
        if self.headers.get("Authorization", "") == "Bearer " + token:
            return True
        return self.headers.get("X-Cura-MCP-Token") == token

    # -- low-level writers ----------------------------------------------- #

    def _write(self, code, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _result(self, rid, result, extra_headers=None):
        self._write(200, {"jsonrpc": "2.0", "id": rid, "result": result}, extra_headers)

    def _error(self, rid, code, message):
        self._write(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # -- HTTP verbs ------------------------------------------------------ #

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/"):
            self._write(200, {"status": "ok", "server": "cura-mcp"})
        else:
            self._write(404, {"error": "not found"})

    def do_POST(self):
        if not self._origin_ok():
            return self._write(403, {"error": "cross-origin request refused"})
        if not self._auth_ok():
            return self._write(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._write(400, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}})
        if isinstance(msg, list):
            return self._write(400, {"error": "batch requests not supported"})
        self._dispatch(msg)

    # -- MCP dispatch ---------------------------------------------------- #

    def _dispatch(self, msg):
        rid = msg.get("id")
        method = msg.get("method")

        # Notifications carry no id — acknowledge with 202 and no body.
        if rid is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
            session = uuid.uuid4().hex
            return self._result(rid, {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cura-mcp", "version": "1.0.0"},
            }, extra_headers={"Mcp-Session-Id": session})

        if method == "ping":
            return self._result(rid, {})

        if method == "tools/list":
            return self._result(rid, {"tools": self.server.tool_defs})

        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                data = self.server.dispatch(name, arguments)
                if isinstance(data, dict) and "__mcp_content__" in data:
                    content = data["__mcp_content__"]  # tool returned MCP content blocks (e.g. an image)
                else:
                    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]
                return self._result(rid, {"content": content, "isError": False})
            except Exception as exc:  # noqa: BLE001 - surfaced to the client as a tool error
                Logger.log("w", "Cura MCP tool '%s' failed: %s", name, exc)
                return self._result(rid, {"content": [{"type": "text", "text": "ERROR: %s" % exc}],
                                          "isError": True})

        return self._error(rid, -32601, "method not found: %s" % method)


class MCPServer:
    """Owns the ThreadingHTTPServer and its background thread."""

    def __init__(self, host, port, tool_defs, dispatch, token=None):
        self.host = host
        self.port = port
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.tool_defs = tool_defs
        self._httpd.dispatch = dispatch
        self._httpd.token = token or None
        self._thread = None

    @property
    def url(self):
        return "http://%s:%d/mcp" % (self.host, self.port)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="CuraMCP-HTTP", daemon=True)
        self._thread.start()
        Logger.log("i", "Cura MCP HTTP server listening on %s:%d", self.host, self.port)

    def stop(self):
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        Logger.log("i", "Cura MCP HTTP server stopped")
