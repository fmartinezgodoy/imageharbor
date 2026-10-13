"""Loopback-only experimental Streamable HTTP profile for trusted private clients.

This is NOT OAuth MCP authorization or a configured ChatGPT connection. Use SSH
encryption for remote access; public/HTTPS deployment is deliberately unsupported.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import select
import stat
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from .access import AccessPolicy
from .mcp import MCPServer
from .model import HarborError, integer

HTTP_PROTOCOLS = ("2025-11-25", "2025-06-18")
MAX_BODY = 262_144
MAX_RESPONSE = 1_048_576
MAX_SESSIONS = 16
SESSION_IDLE = 300
SESSION_LIFETIME = 900


def token_value(value):
    if not isinstance(value, bytes) or not re.fullmatch(rb"[A-Za-z0-9_-]{43,128}", value):
        raise HarborError("Private transport requires an ephemeral high-entropy token")
    return value


def token_from_fd(fd):
    """Consume only an inherited pipe, never read a credential file or argv token."""
    integer(fd, 3, 2**31 - 1)
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise HarborError("Token descriptor must be an inherited pipe")
        deadline, data = time.monotonic() + 3, b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise HarborError("Ephemeral token pipe timed out")
            chunk = os.read(fd, 129 - len(data))
            if not chunk:
                return token_value(data)
            data += chunk
            if len(data) > 128:
                raise HarborError("Invalid ephemeral token size")
    except (OSError, ValueError):
        raise HarborError("Cannot consume ephemeral token pipe") from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@dataclass
class Session:
    server: MCPServer
    protocol: str
    created: float
    last_used: float


class PrivateMCPHTTPServer(HTTPServer):
    """Serial, bounded single-operator listener. Sessions/tokens live only in RAM."""
    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, platform, tasks, policy, token, port=0, token_ttl=900, clock=time.monotonic):
        if not isinstance(policy, AccessPolicy):
            raise HarborError("Private HTTP requires an explicit access policy")
        integer(port, 0, 65535)
        integer(token_ttl, 60, 3600)
        self._token = token_value(token)
        self.platform, self.tasks, self.policy = platform, tasks, policy
        self.clock = clock
        self.expires = clock() + token_ttl
        self.sessions = {}
        self.budget, self.budget_at = 20.0, clock()
        # Literal bind only: no configurable network interface or reverse-proxy mode.
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except OSError:
            raise HarborError("Cannot bind the private loopback MCP listener") from None

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def handle_error(self, request, client_address):
        # Never dump headers, token, request body or platform exceptions to stderr.
        pass

    def authorized(self, header):
        if self.clock() >= self.expires or not isinstance(header, str):
            return False
        try:
            supplied = header.encode("ascii")
        except UnicodeError:
            return False
        return hmac.compare_digest(supplied, b"Bearer " + self._token)

    def consume_budget(self):
        now = self.clock()
        self.budget = min(20.0, self.budget + max(0, now - self.budget_at))
        self.budget_at = now
        if self.budget < 1:
            return False
        self.budget -= 1
        return True

    def prune(self):
        now = self.clock()
        self.sessions = {sid: s for sid, s in self.sessions.items()
                         if now - s.created < SESSION_LIFETIME and now - s.last_used < SESSION_IDLE}

    def server_close(self):
        self.sessions.clear()
        self._token = b""
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ImageHarborPrivate"
    sys_version = ""

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code)

    def respond(self, code, response=None, extra=None):
        body = b"" if response is None else json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > MAX_RESPONSE:
            code, body = 500, b'{"error":"Response limit exceeded; inspect operation state before retrying"}'
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if body:
            self.send_header("Content-Type", "application/json")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def guard(self):
        if self.path != "/mcp":
            self.respond(404)
            return False
        unique = ("Host", "Authorization", "Origin", "Content-Length", "Content-Type", "Accept",
                  "MCP-Session-Id", "MCP-Protocol-Version", "Transfer-Encoding", "Expect")
        if sum(len(k) + len(v) for k, v in self.headers.items()) > 8192 or any(len(self.headers.get_all(k, [])) > 1 for k in unique):
            self.respond(400)
            return False
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}" or "Origin" in self.headers:
            self.respond(403)
            return False
        if any(k.lower() == "forwarded" or k.lower().startswith("x-forwarded-") for k in self.headers):
            self.respond(400)
            return False
        if not self.server.consume_budget():
            self.respond(429, extra={"Retry-After": "1"})
            return False
        if not self.server.authorized(self.headers.get("Authorization")):
            self.respond(401, extra={"WWW-Authenticate": 'Bearer realm="imageharbor-private"'})
            return False
        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in HTTP_PROTOCOLS:
            self.respond(400)
            return False
        self.server.prune()
        return True

    def session(self):
        sid = self.headers.get("MCP-Session-Id")
        if not sid:
            self.respond(400)
            return None
        session = self.server.sessions.get(sid)
        if not session:
            self.respond(404)
            return None
        if self.headers.get("MCP-Protocol-Version") != session.protocol:
            self.respond(400)
            return None
        session.last_used = self.server.clock()
        return session

    def do_GET(self):
        if self.guard() and (not self.headers.get("MCP-Session-Id") or self.session()):
            self.respond(405, extra={"Allow": "POST, DELETE"})

    def do_DELETE(self):
        if self.guard() and self.session():
            del self.server.sessions[self.headers["MCP-Session-Id"]]
            self.respond(204)

    def do_POST(self):
        if not self.guard():
            return
        if "Transfer-Encoding" in self.headers or "Expect" in self.headers:
            self.respond(400)
            return
        if self.headers.get("Content-Type", "").lower() not in ("application/json", "application/json; charset=utf-8"):
            self.respond(415)
            return
        accept = {item.strip() for item in self.headers.get("Accept", "").lower().split(",")}
        if not {"application/json", "text/event-stream"}.issubset(accept):
            self.respond(406)
            return
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,7}", length):
            self.respond(411)
            return
        length = int(length)
        if not 0 < length <= MAX_BODY:
            self.respond(413)
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self.respond(400)
                return
            message = json.loads(body.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            self.respond(400, MCPServer.error(None, -32700, "Parse error"))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self.respond(400, MCPServer.error(None, -32600, "Invalid request"))
            return
        if message.get("method") == "initialize":
            if "id" not in message or self.headers.get("MCP-Session-Id"):
                self.respond(400)
                return
            header_version = self.headers.get("MCP-Protocol-Version")
            if header_version and header_version not in HTTP_PROTOCOLS:
                self.respond(400)
                return
            if len(self.server.sessions) >= MAX_SESSIONS:
                self.respond(503, extra={"Retry-After": "5"})
                return
            dispatcher = MCPServer(self.server.platform, self.server.tasks, self.server.policy)
            response = dispatcher.handle(message)
            if response is None or "error" in response:
                self.respond(400, response)
                return
            proposed = message["params"]["protocolVersion"]
            protocol = proposed if proposed in HTTP_PROTOCOLS else HTTP_PROTOCOLS[0]
            response["result"]["protocolVersion"] = protocol
            sid = secrets.token_urlsafe(32)
            now = self.server.clock()
            self.server.sessions[sid] = Session(dispatcher, protocol, now, now)
            self.respond(200, response, {"MCP-Session-Id": sid})
            return
        session = self.session()
        if not session:
            return
        if "method" not in message:
            self.respond(400)
            return
        try:
            response = session.server.handle(message)
        except Exception:
            response = MCPServer.error(message.get("id"), -32603, "Internal operation failure")
        self.respond(202 if response is None else 200, response)

    def handle_expect_100(self):
        self.respond(417)
        return False

    def do_OPTIONS(self):
        if self.guard():
            self.respond(405, extra={"Allow": "POST, DELETE"})

    do_HEAD = do_OPTIONS
    do_PUT = do_OPTIONS
    do_PATCH = do_OPTIONS
