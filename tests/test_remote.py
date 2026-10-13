import contextlib
import http.client
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from imageharbor.access import AccessPolicy
from imageharbor.mcp import MCPServer
from imageharbor.model import Config, HarborError
from imageharbor.platform import Platform
from imageharbor.remote import MAX_BODY, MAX_SESSIONS, PrivateMCPHTTPServer, token_from_fd
from imageharbor.ssh_plan import plan
from imageharbor.tasks import FakeHarness, TaskStore, work_once
from tests.test_platform import sample


def initialize_message():
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "private-test", "version": "1"}}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(state_dir=Path(self.temp.name), require_digest=False)
        self.platform = Platform(self.cfg)
        self.platform.deploy(sample())
        self.platform.deploy(sample("other"))
        self.tasks = TaskStore(self.cfg.state_dir, scope_provider=self.platform.task_scope)


class AccessTests(Fixture):
    def dispatcher(self, mode="read", data=False):
        server = MCPServer(self.platform, self.tasks, AccessPolicy({"hello"}, mode, data))
        server.handle(initialize_message())
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server, tool, args=None):
        return server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})

    def test_read_catalog_and_direct_mutation_denied(self):
        server = self.dispatcher()
        tools = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"inventory", "status", "task_status", "result"})
        before = self.platform.status("hello")["revision"]
        for tool, args in (("update", {"spec": sample()}), ("remove", {"app_id": "hello"}),
                           ("submit", {"request": {"app_id": "hello", "kind": "reconcile"}, "idempotency_key": "denied"})):
            with self.subTest(tool=tool):
                self.assertTrue(self.call(server, tool, args)["result"]["isError"])
        self.assertEqual(self.platform.status("hello")["state"], "active")
        self.assertEqual(self.platform.status("hello")["revision"], before)
        self.assertIsNone(self.tasks.claim())

    def test_inventory_and_tasks_do_not_leak_outside_app_scope(self):
        server = self.dispatcher("operate")
        out = self.call(server, "inventory")["result"]["structuredContent"]
        self.assertEqual([x["id"] for x in out["applications"]], ["hello"])
        self.assertNotIn("other", json.dumps(out))
        task = self.tasks.submit({"app_id": "other", "kind": "diagnose"}, "foreign")
        for tool in ("task_status", "result", "cancel"):
            with self.subTest(tool=tool):
                response = self.call(server, tool, {"task_id": task["task_id"]})
                self.assertTrue(response["result"]["isError"])
                self.assertNotIn("other", json.dumps(response))
        self.assertEqual(self.tasks.status(task["task_id"])["state"], "queued")
        self.assertTrue(self.call(server, "status", {"app_id": "other"})["result"]["isError"])
        self.assertTrue(self.call(server, "deploy", {"spec": sample("outside")})["result"]["isError"])
        self.assertNotIn("outside", [x["id"] for x in self.platform.inventory()["applications"]])

    def test_data_permission_is_additional_and_platform_guards_remain(self):
        args = {"app_id": "hello", "volumes": ["state"], "confirm": "DELETE DATA hello"}
        self.assertTrue(self.call(self.dispatcher("operate"), "volumes_delete", args)["result"]["isError"])
        self.assertTrue(self.call(self.dispatcher("operate", True), "volumes_delete", args)["result"]["isError"])
        self.platform.remove("hello")
        self.cfg.allow_volume_delete = True
        self.assertFalse(self.call(self.dispatcher("operate", True), "volumes_delete", args)["result"]["isError"])

    def test_policy_is_immutable_and_validates_scope(self):
        apps = {"hello"}
        policy = AccessPolicy(apps)
        apps.add("other")
        self.assertEqual(policy.apps, {"hello"})
        with self.assertRaises(AttributeError):
            policy.mode = "operate"
        for apps, mode, data in ((set(), "read", False), ({"*"}, "read", False), ({"hello"}, "admin", False), ({"hello"}, "read", True)):
            with self.assertRaises(HarborError):
                AccessPolicy(apps, mode, data)


class TokenAndPlanTests(unittest.TestCase):
    def test_token_pipe_is_consumed_closed_and_never_persisted(self):
        read, write = os.pipe()
        token = secrets.token_urlsafe(32).encode()
        os.write(write, token)
        os.close(write)
        self.assertEqual(token_from_fd(read), token)
        with self.assertRaises(OSError):
            os.fstat(read)

    def test_token_file_is_rejected_without_reading(self):
        with tempfile.TemporaryFile() as file:
            file.write(b"DO-NOT-READ")
            file.flush()
            duplicate = os.dup(file.fileno())
            with patch("imageharbor.remote.os.read") as read, self.assertRaises(HarborError):
                token_from_fd(duplicate)
            read.assert_not_called()

    def test_empty_and_oversized_pipe_tokens_rejected(self):
        for value in (b"", b"short", b"x" * 129, b"x" * 43 + b"\n"):
            read, write = os.pipe()
            os.write(write, value)
            os.close(write)
            with self.subTest(length=len(value)), self.assertRaises(HarborError):
                token_from_fd(read)

    def test_ssh_plan_is_fixed_dry_run_and_rejects_injection(self):
        with patch("subprocess.run") as run:
            result = plan("example-host", "/srv/imageharbor", ["hello"])
        run.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertFalse(result["chatgpt_connected"])
        argv = result["argv"]
        for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ForwardAgent=no", "ClearAllForwardings=yes", "PermitLocalCommand=no", "ControlPath=none"):
            self.assertIn(option, argv)
        self.assertIn("--access read --allow-app hello", argv[-1])
        for alias, root in (("-oProxyCommand=id", "/srv/app"), ("example-host;id", "/srv/app"), ("example-host", "/srv/../root"), ("example-host", "/srv/$(id)")):
            with self.subTest(alias=alias, root=root), self.assertRaises(HarborError):
                plan(alias, root, ["hello"])

    def test_restricted_stdio_real_cli_and_ssh_plan_never_contacts_host(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"state_dir": "state", "require_digest": False}))
            messages = [initialize_message(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
            completed = subprocess.run([sys.executable, "-m", "imageharbor", "--config", str(config), "mcp", "--allow-app", "hello"],
                                       cwd=root, input="".join(json.dumps(m) + "\n" for m in messages), text=True, capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(json.loads(completed.stdout.splitlines()[-1])["result"]["tools"]), 4)
            completed = subprocess.run([sys.executable, "-m", "imageharbor", "--config", str(config), "mcp", "--access", "read"],
                                       cwd=root, input="", text=True, capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("explicit allowed applications", completed.stderr)
            completed = subprocess.run([sys.executable, "-m", "imageharbor", "ssh-plan", "--alias", "example-host", "--remote-root", "/srv/imageharbor", "--allow-app", "hello"],
                                       cwd=root, text=True, capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(json.loads(completed.stdout)["executed"])


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.token = secrets.token_urlsafe(32).encode()
        try:
            self.server = PrivateMCPHTTPServer(self.platform, self.tasks, AccessPolicy({"hello"}, "operate"), self.token,
                                               token_ttl=900, clock=lambda: self.now)
        except HarborError as error:
            if "bind" in str(error):
                self.skipTest("Sandbox forbids loopback bind; transport test was not executed")
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.sid = None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def request(self, message=None, method="POST", headers=None, raw=None):
        self.now += 2  # Normal clients stay within the enforced 1 request/s budget.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        default = {"Authorization": "Bearer " + self.token.decode(), "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.sid:
            default.update({"MCP-Session-Id": self.sid, "MCP-Protocol-Version": "2025-11-25"})
        for key, value in (headers or {}).items():
            if value is None:
                default.pop(key, None)
            else:
                default[key] = value
        body = raw if raw is not None else json.dumps(message).encode() if message is not None else None
        try:
            connection.request(method, "/mcp", body=body, headers=default)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), json.loads(data) if data else None
        finally:
            connection.close()

    def initialize(self):
        status, headers, response = self.request(initialize_message())
        self.assertEqual(status, 200)
        self.sid = headers["MCP-Session-Id"]
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)

    def tool(self, name, args=None):
        status, _, response = self.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
        self.assertEqual(status, 200)
        return response["result"]

    def test_real_loopback_vertical_mcp_flow_with_fake_platform(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.initialize()
        self.assertEqual([x["id"] for x in self.tool("inventory")["structuredContent"]["applications"]], ["hello"])
        updated = sample()
        updated["image"] = "docker.io/traefik/whoami:v1.11.1"
        self.assertFalse(self.tool("update", {"spec": updated})["isError"])
        self.assertEqual(self.tool("status", {"app_id": "hello"})["structuredContent"]["revision"], 2)
        self.assertFalse(self.tool("rollback", {"app_id": "hello", "revision": 1})["isError"])
        task = self.tool("submit", {"request": {"app_id": "hello", "kind": "diagnose"}, "idempotency_key": "http"})["structuredContent"]
        self.assertEqual(self.tool("task_status", {"task_id": task["task_id"]})["structuredContent"]["state"], "queued")
        work_once(self.tasks, self.platform, FakeHarness())
        self.assertEqual(self.tool("result", {"task_id": task["task_id"]})["structuredContent"]["state"], "succeeded")
        task = self.tool("submit", {"request": {"app_id": "hello", "kind": "diagnose"}, "idempotency_key": "http-cancel"})["structuredContent"]
        self.assertEqual(self.tool("cancel", {"task_id": task["task_id"]})["structuredContent"]["state"], "cancelled")
        self.assertFalse(self.tool("remove", {"app_id": "hello"})["isError"])
        self.assertTrue(self.tool("volumes_delete", {"app_id": "hello", "volumes": ["state"], "confirm": "DELETE DATA hello"})["isError"])
        self.assertEqual(self.request(method="DELETE")[0], 204)
        self.assertEqual(self.request({"jsonrpc": "2.0", "id": 4, "method": "ping"})[0], 404)

    def test_authentication_required_on_every_method_and_session_is_not_auth(self):
        self.initialize()
        for method in ("POST", "GET", "DELETE", "OPTIONS"):
            for auth in (None, "Bearer wrong", "Basic fake"):
                with self.subTest(method=method, auth=auth):
                    status, headers, _ = self.request({"jsonrpc": "2.0", "id": 3, "method": "ping"}, method=method, headers={"Authorization": auth})
                    self.assertEqual(status, 401)
                    self.assertIn("WWW-Authenticate", headers)
        self.assertIn(self.sid, self.server.sessions)
        self.now = self.server.expires + 1
        self.assertEqual(self.request(method="DELETE")[0], 401)

    def test_host_origin_forwarding_and_protocol_fail_closed(self):
        self.initialize()
        for headers, expected in (({"Origin": "https://attacker.invalid"}, 403), ({"Origin": "null"}, 403),
                                   ({"Host": "attacker.invalid"}, 403), ({"X-Forwarded-Host": "127.0.0.1"}, 400),
                                   ({"Forwarded": "for=127.0.0.1"}, 400), ({"MCP-Protocol-Version": "unknown"}, 400)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request({"jsonrpc": "2.0", "id": 3, "method": "ping"}, headers=headers)[0], expected)

    def test_session_handshake_unknown_expiry_and_no_sse(self):
        self.assertEqual(self.request({"jsonrpc": "2.0", "id": 3, "method": "ping"})[0], 400)
        self.initialize()
        self.assertEqual(self.request(method="GET")[0], 405)
        self.assertEqual(self.request({"jsonrpc": "2.0", "id": 3, "method": "ping"}, headers={"MCP-Session-Id": "unknown"})[0], 404)
        self.assertEqual(self.request({"jsonrpc": "2.0", "id": 3, "method": "ping"}, headers={"MCP-Protocol-Version": None})[0], 400)
        self.assertEqual(self.request(initialize_message())[0], 400)
        self.now += 301
        self.assertEqual(self.request(method="GET")[0], 404)

    def test_body_limits_invalid_json_types_and_headers(self):
        for raw in (b"not-json", b"\xff", b"[]", b'{"jsonrpc":"2.0","id":NaN}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(self.request(raw=b"x" * (MAX_BODY + 1))[0], 413)
        for headers, expected in (({"Content-Type": "text/plain"}, 415), ({"Accept": "application/json"}, 406),
                                   ({"Content-Length": "invalid"}, 411), ({"Transfer-Encoding": "chunked"}, 400)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(initialize_message(), headers=headers)[0], expected)

    def test_duplicate_sensitive_headers_rejected(self):
        for header in ("Authorization", "Content-Length", "Host", "MCP-Session-Id"):
            with self.subTest(header=header):
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
                connection.putrequest("POST", "/mcp")
                connection.putheader("Authorization", "Bearer " + self.token.decode())
                connection.putheader("Content-Length", "0")
                if header == "MCP-Session-Id":
                    connection.putheader(header, "one")
                connection.putheader(header, "duplicate")
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                connection.close()

    def test_sessions_are_independent_and_absolute_lifetime_expires(self):
        self.initialize()
        first = self.sid
        self.sid = None
        status, headers, _ = self.request(initialize_message())
        self.assertEqual(status, 200)
        self.sid = headers["MCP-Session-Id"]
        self.assertNotEqual(first, self.sid)
        response = self.request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})[2]
        self.assertIn("error", response)  # The first session's initialized notification does not unlock another.
        self.server.expires = self.now + 3600
        session = self.server.sessions[self.sid]
        session.created = self.now - 901
        session.last_used = self.now
        self.assertEqual(self.request({"jsonrpc": "2.0", "id": 4, "method": "ping"})[0], 404)
        self.assertIn(first, self.server.sessions)

    def test_idle_socket_times_out_without_dispatch(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            self.assertEqual(connection.recv(1), b"")
            self.assertFalse(self.server.sessions)
            self.assertEqual(self.platform.status("hello")["revision"], 1)
        finally:
            connection.close()

    def test_session_cap_and_request_budget(self):
        for _ in range(MAX_SESSIONS):
            self.assertEqual(self.request(initialize_message())[0], 200)
        self.assertEqual(self.request(initialize_message())[0], 503)
        self.server.budget, self.server.budget_at = 0, self.now + 2
        self.assertEqual(self.request(initialize_message())[0], 429)

    def test_errors_and_logs_do_not_echo_token_or_payload(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status, _, response = self.request(raw=b"DO-NOT-LEAK")
            self.assertEqual(status, 400)
            self.assertNotIn("DO-NOT-LEAK", json.dumps(response))
            self.initialize()
            result = self.tool("update", {"spec": {**sample(), "env": {"API_TOKEN": "DO-NOT-LEAK"}}})
            self.assertTrue(result["isError"])
            self.assertNotIn("DO-NOT-LEAK", json.dumps(result))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        for file in self.cfg.state_dir.iterdir():
            if file.is_file():
                self.assertNotIn(self.token, file.read_bytes())

    def test_http_cli_subprocess_demo_and_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, "-m", "examples.remote_demo"], cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["http_cli_subprocess"])
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["simulated_platform"])
        self.assertTrue(result["data_retained_same_pvc_uid"])
        self.assertFalse(result["data_delete_advertised"])
        self.assertFalse(result["oracle_contacted"])
        self.assertFalse(result["chatgpt_connected"])
        self.assertEqual(result["async_state"], "succeeded")
        self.assertEqual(result["release_revisions"], [1, 2, 3])
