"""Regression cases from the independent review; no Docker/cloud dependencies."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from imageharbor.backend import locked
from imageharbor.manifests import APP, ROLE, render
from imageharbor.mcp import MCPServer
from imageharbor.model import Config, HarborError, validate_spec
from imageharbor.platform import Platform, RECORD
from imageharbor.tasks import FakeHarness, TaskStore, work_once
from tests.test_platform import sample


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(state_dir=Path(self.temp.name), require_digest=False, log_safe_apps=["hello"])
        self.platform = Platform(self.cfg)
        self.spec = sample()
        self.platform.deploy(self.spec)
        self.ns = self.cfg.namespace("hello")
        self.tasks = TaskStore(self.cfg.state_dir, scope_provider=self.platform.task_scope)

    def get(self, kind, resource_name="app"):
        return self.platform.backend.get(kind, self.ns, resource_name)

    def replace(self, value):
        original = self.platform.backend.get(value["kind"], value["metadata"].get("namespace"), value["metadata"]["name"])
        return self.platform.backend.apply(value, original)


class OwnershipAndDataTests(ReviewCase):
    def test_namespace_relabel_blocks_remove_and_logs(self):
        ns = self.platform.backend.get("Namespace", None, self.ns)
        altered = copy.deepcopy(ns)
        altered["metadata"]["labels"][APP] = "foreign"
        self.replace(altered)
        before = self.get("Deployment")["metadata"]["uid"]
        for operation in (lambda: self.platform.remove("hello"), lambda: self.platform.logs("hello")):
            with self.assertRaisesRegex(HarborError, "another owner"):
                operation()
        self.assertEqual(self.get("Deployment")["metadata"]["uid"], before)
        cm = self.get("ConfigMap", RECORD)
        self.assertEqual(json.loads(cm["data"]["release.json"])["state"], "active")

    def test_namespace_uid_recreation_is_not_adopted(self):
        ns = self.platform.backend.get("Namespace", None, self.ns)
        self.platform.backend.delete(ns)
        self.platform.backend.apply(ns)
        with self.assertRaisesRegex(HarborError, "namespace identity"):
            self.platform.remove("hello")

    def test_foreign_deployment_logs_are_never_requested(self):
        deployment = self.get("Deployment")
        changed = copy.deepcopy(deployment)
        changed["metadata"]["labels"][APP] = "foreign"
        self.replace(changed)
        called = []
        self.platform.backend.logs = lambda *_: called.append(True)
        with self.assertRaisesRegex(HarborError, "another owner"):
            self.platform.logs("hello")
        self.assertFalse(called)

    def test_tracked_missing_pvc_is_not_recreated(self):
        volume = self.get("PersistentVolumeClaim", "data-state")
        self.assertEqual(self.platform._record("hello")["volumes"]["state"]["uid"], volume["metadata"]["uid"])
        self.platform.backend.delete(volume)
        report = self.platform.reconcile()[0]
        self.assertFalse(report["reconciled"])
        self.assertIn("empty replacement", report["error"])
        self.assertIsNone(self.get("PersistentVolumeClaim", "data-state"))
        self.assertFalse(self.platform.status("hello")["ready"])

    def test_replacement_pvc_uid_is_rejected_for_deploy_and_delete(self):
        volume = self.get("PersistentVolumeClaim", "data-state")
        self.platform.backend.delete(volume)
        replacement = self.platform.backend.apply(volume)
        self.assertNotEqual(volume["metadata"]["uid"], replacement["metadata"]["uid"])
        self.assertFalse(self.platform.reconcile()[0]["reconciled"])
        self.assertIn("PersistentVolumeClaim/data-state", self.platform.status("hello")["drift"])
        with self.assertRaisesRegex(HarborError, "identity changed"):
            self.platform.remove("hello")
        record = self.platform._record("hello")
        record["state"] = "removed"
        self.platform._save("hello", record)
        self.assertFalse(self.platform.reconcile()[0]["reconciled"])
        self.assertIsNotNone(self.get("Deployment"))
        # Simulate an independent runtime removal, then check the data guard itself.
        self.platform.backend.delete(self.get("Deployment"))
        self.cfg.allow_volume_delete = True
        with self.assertRaisesRegex(HarborError, "identity changed"):
            self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.assertEqual(self.get("PersistentVolumeClaim", "data-state")["metadata"]["uid"], replacement["metadata"]["uid"])

    def test_owner_reference_prevents_default_remove_gc(self):
        volume = self.get("PersistentVolumeClaim", "data-state")
        changed = copy.deepcopy(volume)
        changed["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "Deployment", "name": "app", "uid": self.get("Deployment")["metadata"]["uid"]}]
        self.replace(changed)
        with self.assertRaisesRegex(HarborError, "ownerReferences"):
            self.platform.remove("hello")
        self.assertIsNotNone(self.get("Deployment"))
        self.assertEqual(self.platform._record("hello")["state"], "active")

    def test_unlabelled_pods_and_other_deployments_block_data_deletion(self):
        self.platform.remove("hello")
        self.cfg.allow_volume_delete = True
        for kind in ("Pod", "Deployment"):
            with self.subTest(kind=kind):
                foreign = self.platform.backend.apply({"apiVersion": "v1" if kind == "Pod" else "apps/v1", "kind": kind,
                    "metadata": {"name": "foreign", "namespace": self.ns, "labels": {}}})
                with self.assertRaisesRegex(HarborError, "Pods and Deployment"):
                    self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
                self.platform.backend.delete(foreign)
        self.assertIsNotNone(self.get("PersistentVolumeClaim", "data-state"))

    def test_pending_pvc_delete_is_durable_retryable_and_prevents_redeploy(self):
        self.platform.remove("hello")
        self.cfg.allow_volume_delete = True
        original_delete = self.platform.backend.delete
        def pending(current):
            changed = copy.deepcopy(current)
            changed["metadata"]["deletionTimestamp"] = "2026-10-08T00:00:00Z"
            self.replace(changed)
        self.platform.backend.delete = pending
        result = self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.assertEqual(result["pending"], ["state"])
        self.assertEqual(self.platform._record("hello")["volumes"]["state"]["state"], "delete_requested")
        with self.assertRaises(HarborError):
            self.platform.deploy(self.spec)
        self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.platform.backend.delete = original_delete
        original_delete(self.get("PersistentVolumeClaim", "data-state"))
        self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.assertEqual(self.platform._record("hello")["volumes"]["state"]["state"], "deleted")
        self.assertTrue(self.platform.deploy(self.spec)["ready"])

    def test_partial_data_delete_is_retryable_without_erasing_ledger(self):
        spec = {**self.spec, "volumes": self.spec["volumes"] + [{"name": "second", "mount_path": "/second", "size_gi": 1}]}
        self.platform.update(spec)
        self.platform.remove("hello")
        self.cfg.allow_volume_delete = True
        original_delete = self.platform.backend.delete
        def fail_second(current):
            if current["metadata"]["name"] == "data-second":
                raise HarborError("injected failure")
            original_delete(current)
        self.platform.backend.delete = fail_second
        with self.assertRaises(HarborError):
            self.platform.volumes_delete("hello", ["state", "second"], "DELETE DATA hello")
        self.assertIsNone(self.get("PersistentVolumeClaim", "data-state"))
        self.assertIsNotNone(self.get("PersistentVolumeClaim", "data-second"))
        self.assertTrue(all(v["state"] == "delete_requested" for v in self.platform._record("hello")["volumes"].values()))
        self.platform.backend.delete = original_delete
        self.platform.volumes_delete("hello", ["state", "second"], "DELETE DATA hello")
        self.assertTrue(all(v["state"] == "deleted" for v in self.platform._record("hello")["volumes"].values()))

    def test_rollback_refuses_missing_historical_volume_before_journaling(self):
        self.platform.update({**self.spec, "volumes": []})
        self.platform.backend.delete(self.get("PersistentVolumeClaim", "data-state"))
        with self.assertRaisesRegex(HarborError, "missing"):
            self.platform.rollback("hello", 1)
        self.assertEqual(self.platform.status("hello")["revision"], 2)

    def test_metadata_uses_original_read_version(self):
        stale = self.platform._record("hello")
        self.platform.update({**self.spec, "image": "acme/demo:v2"})
        stale["state"] = "removed"
        with self.assertRaisesRegex(HarborError, "concurrently"):
            self.platform._save("hello", stale)
        self.assertEqual(self.platform.status("hello")["revision"], 2)
        self.assertEqual(self.platform.status("hello")["state"], "active")

    def test_corrupt_other_metadata_does_not_break_app_scoped_status(self):
        self.platform.deploy(sample("other"))
        cm = self.platform.backend.get("ConfigMap", self.cfg.namespace("other"), RECORD)
        cm["data"]["release.json"] = '{"unexpected": "DO-NOT-ECHO"}'
        self.replace(cm)
        self.assertEqual(self.platform.status("hello")["id"], "hello")
        inventory = self.platform.inventory()
        self.assertEqual(inventory["metadata_errors"][0]["id"], "other")
        self.assertNotIn("DO-NOT-ECHO", json.dumps(inventory))

    def test_disabling_routes_closes_them_even_when_volume_is_missing(self):
        self.cfg.routing_enabled, self.cfg.allow_http = True, True
        self.cfg.route_cidrs = ["10.0.0.0/8"]
        self.platform.update({**self.spec, "canonical_url": "http://hello.apps.example.test/"})
        self.assertIsNotNone(self.get("IngressRoute"))
        self.platform.backend.delete(self.get("PersistentVolumeClaim", "data-state"))
        self.cfg.routing_enabled = False
        self.assertFalse(self.platform.reconcile()[0]["reconciled"])
        self.assertIsNone(self.get("IngressRoute"))

    def test_unknown_runtime_is_not_deleted_by_label_alone(self):
        value = copy.deepcopy(self.get("Deployment"))
        value["metadata"]["name"] = "unregistered"
        self.platform.backend.apply(value)
        with self.assertRaisesRegex(HarborError, "Unregistered runtime"):
            self.platform.remove("hello")
        self.assertIsNotNone(self.get("Deployment", "unregistered"))

    def test_state_directory_rejects_another_instance_or_context(self):
        with self.assertRaisesRegex(HarborError, "State directory belongs"):
            Platform(Config(state_dir=self.cfg.state_dir, instance="another", require_digest=False))
        with self.assertRaisesRegex(HarborError, "State directory belongs"):
            Platform(Config(state_dir=self.cfg.state_dir, kube_context="another", require_digest=False))


class ScopedTaskReviewTests(ReviewCase):
    def submit(self, kind="reconcile", key="review"):
        return self.tasks.submit({"app_id": "hello", "kind": kind}, key)

    def test_capability_identity_is_read_only(self):
        self.submit("diagnose")
        test = self
        class Harness:
            def execute(self, request, capabilities, cancelled):
                with test.assertRaises(AttributeError):
                    capabilities.app_id = "other"
                request["app_id"] = "other"
                return {"id": capabilities.status()["id"]}
        outcome = work_once(self.tasks, self.platform, Harness())
        self.assertEqual(outcome["result"]["id"], "hello")

    def test_diagnose_cannot_mutate(self):
        self.platform.backend.delete(self.get("Deployment"))
        self.submit("diagnose")
        class Harness:
            def execute(self, request, capabilities, cancelled):
                return capabilities.reconcile()
        outcome = work_once(self.tasks, self.platform, Harness())
        self.assertEqual(outcome["state"], "failed")
        self.assertIsNone(self.get("Deployment"))

    def test_cancel_while_waiting_for_writer_lock_prevents_mutation(self):
        self.platform.backend.delete(self.get("Deployment"))
        task = self.submit()
        entered = threading.Event()
        original_cancelled = self.tasks.cancelled
        def observed(task):
            result = original_cancelled(task)
            entered.set()
            return result
        self.tasks.cancelled = observed
        outcomes = []
        with locked(self.platform.writer_lock):
            worker = threading.Thread(target=lambda: outcomes.append(work_once(self.tasks, self.platform, FakeHarness())))
            worker.start()
            self.assertTrue(entered.wait(2))
            self.tasks.cancel(task["task_id"])
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0]["state"], "cancelled")
        self.assertIsNone(self.get("Deployment"))

    def test_cancel_after_one_resource_stops_next_resource(self):
        self.platform.backend.delete(self.get("Service"))
        self.platform.backend.delete(self.get("Deployment"))
        task = self.submit()
        original_apply = self.platform.backend.apply
        def cancel_after_service(desired, expected=None):
            value = original_apply(desired, expected)
            if desired["kind"] == "Service":
                self.tasks.cancel(task["task_id"])
            return value
        self.platform.backend.apply = cancel_after_service
        result = work_once(self.tasks, self.platform, FakeHarness())
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNotNone(self.get("Service"))  # Already applied action is not undone.
        self.assertIsNone(self.get("Deployment"))

    def test_scope_uid_replacement_is_rejected_before_harness_call(self):
        self.submit("diagnose")
        cm = self.get("ConfigMap", RECORD)
        self.platform.backend.delete(cm)
        self.platform.backend.apply(cm)
        called = []
        class Harness:
            def execute(self, *args):
                called.append(True)
                return {}
        self.assertEqual(work_once(self.tasks, self.platform, Harness())["state"], "failed")
        self.assertFalse(called)

    def test_expired_lease_cannot_be_revived_or_finish_successfully(self):
        self.submit("diagnose")
        task = self.tasks.claim()
        with self.tasks.connect() as db:
            db.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time() - 100, task["task_id"]))
        self.assertTrue(self.tasks.cancelled(task))
        self.assertFalse(self.tasks.heartbeat(task))
        self.tasks.finish(task, result={"late": True})
        self.assertEqual(self.tasks.result(task["task_id"])["state"], "interrupted")

    def test_legacy_unscoped_task_fails_closed(self):
        self.submit("diagnose")
        with self.tasks.connect() as db:
            db.execute("UPDATE tasks SET scope=NULL")
        self.assertEqual(work_once(self.tasks, self.platform, FakeHarness())["state"], "failed")


class MCPAndInputReviewTests(ReviewCase):
    def server(self):
        server = MCPServer(self.platform, self.tasks)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def test_invalid_request_id_prevents_side_effects(self):
        server = self.server()
        response = server.handle({"jsonrpc": "2.0", "id": {"secret": "DO-NOT-ECHO"}, "method": "tools/call", "params": {
            "name": "remove", "arguments": {"app_id": "hello"}}})
        self.assertEqual(response["error"]["code"], -32600)
        self.assertNotIn("DO-NOT-ECHO", json.dumps(response))
        self.assertEqual(self.platform.status("hello")["state"], "active")

    def test_nested_schema_blocks_unknown_fields_and_wrong_types(self):
        server = self.server()
        for params in (
            {"name": "update", "arguments": {"spec": {**self.spec, "command": ["sh"]}}},
            {"name": "update", "arguments": {"spec": {**self.spec, "env": {"MODE": 123}}}},
            {"name": "volumes_delete", "arguments": {"app_id": "hello", "volumes": [{}], "confirm": "DELETE DATA hello"}},
            {"name": "logs", "arguments": {"app_id": "hello", "tail": True}},
        ):
            with self.subTest(params=params):
                response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params})
                self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(self.platform.status("hello")["revision"], 1)

    def test_malformed_initialize_does_not_unlock_tools(self):
        server = MCPServer(self.platform, self.tasks)
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": [], "clientInfo": {"name": "test", "version": "1"}}})
        self.assertIn("error", response)
        self.assertFalse(server.initialized)

    def test_default_config_without_state_dir_is_valid(self):
        file = Path(self.temp.name) / "minimal.json"
        file.write_text("{}")
        self.assertEqual(Config.load(file).state_dir, (file.parent / ".local").resolve())

    def test_canonical_scheme_and_malformed_url_are_rejected_safely(self):
        cfg = Config(require_digest=False, allow_http=True, routing_enabled=True)
        with self.assertRaisesRegex(HarborError, "scheme"):
            validate_spec({**self.spec, "canonical_url": "https://hello.apps.example.test/"}, cfg)
        with self.assertRaises(HarborError):
            validate_spec({**self.spec, "repo": "https://[invalid"}, self.cfg)
