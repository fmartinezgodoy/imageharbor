import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from imageharbor.mcp import MCPServer
from imageharbor.model import Config, HarborError
from imageharbor.platform import Platform
from imageharbor.tasks import FakeHarness, TaskStore, UnconfiguredHarness, work_once
from tests.test_platform import sample


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(state_dir=Path(self.temp.name), require_digest=False)
        self.platform = Platform(self.cfg)
        self.platform.deploy(sample())
        self.tasks = TaskStore(self.cfg.state_dir, scope_provider=self.platform.task_scope)
        self.request = {"app_id": "hello", "kind": "diagnose"}

    def test_submit_status_result_and_persistence(self):
        task = self.tasks.submit(self.request, "demo-1")
        self.assertEqual(task["state"], "queued")
        self.assertEqual(self.tasks.submit(self.request, "demo-1")["task_id"], task["task_id"])
        with self.assertRaises(HarborError):
            self.tasks.submit({**self.request, "kind": "reconcile"}, "demo-1")
        result = work_once(self.tasks, self.platform, FakeHarness())
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(result["result"]["simulated"])
        restored = TaskStore(self.cfg.state_dir)
        self.assertEqual(restored.result(task["task_id"])["state"], "succeeded")
        self.assertEqual(self.tasks.path.stat().st_mode & 0o777, 0o600)

    def test_unconfigured_adapter_fails_honestly(self):
        task = self.tasks.submit(self.request, "no-harness")
        result = work_once(self.tasks, self.platform, UnconfiguredHarness())
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "harness_not_configured")
        self.assertIsNone(result["result"])

    def test_cancel_queued_and_running(self):
        queued = self.tasks.submit(self.request, "queued")
        self.assertEqual(self.tasks.cancel(queued["task_id"])["state"], "cancelled")
        running = self.tasks.submit(self.request, "running")
        entered = threading.Event()
        released = threading.Event()
        class BlockingHarness:
            def execute(self, request, capabilities, cancelled):
                entered.set()
                released.wait(3)
                return {"simulated": True}
        worker = threading.Thread(target=work_once, args=(self.tasks, self.platform, BlockingHarness()))
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.tasks.cancel(running["task_id"])["state"], "cancel_requested")
        released.set()
        worker.join(3)
        self.assertEqual(self.tasks.result(running["task_id"])["state"], "cancelled")
        self.assertIsNone(self.tasks.result(running["task_id"])["result"])

    def test_scoped_capabilities_and_reconcile(self):
        self.platform.deploy(sample("other"))
        deployment = self.platform.backend.get("Deployment", self.cfg.namespace("hello"), "app")
        self.platform.backend.delete(deployment)
        self.tasks.submit({"app_id": "hello", "kind": "reconcile"}, "repair")
        result = work_once(self.tasks, self.platform, FakeHarness())
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["app_id"], "hello")
        self.assertTrue(self.platform.status("hello")["ready"])
        for request in ({"app_id": "hello", "kind": "shell"}, {**self.request, "command": "id"},
                        {**self.request, "namespace": "kube-system"}):
            with self.subTest(request=request), self.assertRaises(HarborError):
                self.tasks.submit(request, "bad")

    def test_restart_expires_stale_lease_without_replaying(self):
        task = self.tasks.submit(self.request, "crash")
        claimed = self.tasks.claim()
        with self.tasks.connect() as db:
            db.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time() - 100, task["task_id"]))
        self.assertEqual(TaskStore(self.cfg.state_dir).recover_stale(), 1)
        self.assertEqual(self.tasks.result(task["task_id"])["state"], "interrupted")
        self.tasks.finish(claimed, result={"late": True})
        self.assertEqual(self.tasks.result(task["task_id"])["state"], "interrupted")
        self.assertFalse(work_once(self.tasks, self.platform, FakeHarness())["worked"])

    def test_atomic_claim_only_one_worker(self):
        self.tasks.submit(self.request, "race")
        claims = []
        def claim():
            claims.append(self.tasks.claim())
        threads = [threading.Thread(target=claim) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(value is not None for value in claims), 1)

    def test_fake_harness_refused_on_real_backend(self):
        self.platform.backend.simulated = False
        with self.assertRaisesRegex(HarborError, "forbidden"):
            work_once(self.tasks, self.platform, FakeHarness())


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config(state_dir=Path(self.temp.name), require_digest=False)
        self.platform = Platform(cfg)
        self.server = MCPServer(self.platform, TaskStore(cfg.state_dir))

    def call(self, method, params=None, request_id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

    def initialize(self):
        response = self.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_protocol_schema_and_bounded_operations(self):
        self.assertIn("error", self.call("tools/list"))
        self.initialize()
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 12)
        self.assertTrue(all(not t["inputSchema"]["additionalProperties"] for t in tools))
        deployed = self.call("tools/call", {"name": "deploy", "arguments": {"spec": sample()}})
        self.assertFalse(deployed["result"]["isError"])
        task = self.call("tools/call", {"name": "submit", "arguments": {"request": {"app_id": "hello", "kind": "diagnose"}, "idempotency_key": "mcp"}})
        task_id = task["result"]["structuredContent"]["task_id"]
        response = self.call("tools/call", {"name": "cancel", "arguments": {"task_id": task_id}})
        self.assertEqual(response["result"]["structuredContent"]["state"], "cancelled")
        bad = self.call("tools/call", {"name": "status", "arguments": {"app_id": "hello", "shell": "whoami"}})
        self.assertIn("error", bad)
        unknown = self.call("tools/call", {"name": "shell", "arguments": {}})
        self.assertIn("error", unknown)

    def test_tool_errors_do_not_echo_secret_input(self):
        self.initialize()
        spec = {**sample(), "env": {"API_TOKEN": "DO-NOT-LEAK"}}
        response = self.call("tools/call", {"name": "deploy", "arguments": {"spec": spec}})
        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("DO-NOT-LEAK", json.dumps(response))

    def test_stdio_is_json_only_and_parse_errors(self):
        source = io.StringIO('not-json\n' + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + '\n')
        target = io.StringIO()
        self.server.serve(source, target)
        lines = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})

    def test_real_cli_stdio_subprocess(self):
        root = Path(__file__).resolve().parents[1]
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"state_dir": "state", "backend": "fake", "require_digest": False}))
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "deploy", "arguments": {"spec": sample()}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "inventory", "arguments": {}}},
        ]
        completed = subprocess.run([sys.executable, "-m", "imageharbor", "--config", str(config), "mcp"], cwd=root,
                                   input="".join(json.dumps(m) + "\n" for m in messages), text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        outputs = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(outputs), 3)
        self.assertEqual(outputs[-1]["result"]["structuredContent"]["applications"][0]["id"], "hello")
