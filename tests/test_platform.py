import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from imageharbor.backend import Expired, FakeBackend
from imageharbor.manifests import APP, MANAGED, render
from imageharbor.model import Config, HarborError, validate_spec
from imageharbor.platform import Platform, RECORD


def sample(app="hello"):
    return {"id": app, "purpose": "Test app", "image": "acme/demo:v1", "port": 8080,
            "env": {"MODE": "test"}, "routes": [{"subdomain": app, "path": "/", "prefix": "preserve"}],
            "volumes": [{"name": "state", "mount_path": "/data", "size_gi": 1}]}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(state_dir=Path(self.temp.name), require_digest=False, log_safe_apps=["hello"])
        self.platform = Platform(self.cfg)
        self.spec = sample()

    def test_vertical_release_rollback_remove_and_restore(self):
        deployed = self.platform.deploy(self.spec)
        self.assertTrue(deployed["ready"])
        self.assertEqual(deployed["backend"], "fake")
        volume_uid = deployed["volumes"][0]["uid"]
        v2 = {**self.spec, "image": "acme/demo:v2", "env": {"MODE": "new"}}
        self.assertEqual(self.platform.update(v2)["revision"], 2)
        restored = self.platform.rollback("hello", 1)
        self.assertEqual(restored["image"], "docker.io/acme/demo:v1")
        self.assertEqual(restored["revision"], 3)
        self.assertIn("simulated", self.platform.logs("hello"))
        removed = self.platform.remove("hello")
        self.assertEqual(removed["state"], "removed")
        self.assertFalse(removed["ready"])
        self.assertEqual(removed["volumes"][0]["uid"], volume_uid)
        self.assertIsNone(self.platform.backend.get("Deployment", self.cfg.namespace("hello"), "app"))
        self.assertIsNotNone(self.platform.backend.get("NetworkPolicy", self.cfg.namespace("hello"), "app"))
        self.assertEqual(self.platform.deploy(self.spec)["volumes"][0]["uid"], volume_uid)

    def test_persistence_and_idempotent_retry(self):
        self.platform.deploy(self.spec)
        platform = Platform(self.cfg)
        self.assertEqual(platform.deploy(self.spec)["revision"], 1)
        self.assertEqual(platform.update(self.spec)["revision"], 1)
        self.assertEqual(platform.inventory()["applications"][0]["purpose"], "Test app")

    def test_default_closed_and_no_plaintext_secret(self):
        self.spec["secrets"] = [{"env": "API_TOKEN", "name": "runtime", "key": "token"}]
        result = self.platform.deploy(self.spec)
        self.assertFalse(result["routing_enabled"])
        self.assertEqual(self.platform.backend.list("IngressRoute", {})["items"], [])
        deployment = self.platform.backend.get("Deployment", self.cfg.namespace("hello"), "app")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(pod["containers"][0]["env"][1]["valueFrom"]["secretKeyRef"]["name"], "runtime")
        self.assertNotIn("ownerReferences", json.dumps(result))

    def test_prefix_rules_multiple_hosts_same_service_and_cleanup(self):
        self.cfg.routing_enabled = True
        self.cfg.allow_http = True
        self.cfg.route_cidrs = ["10.0.0.0/8"]
        self.spec.update(path_support=True, routes=[
            {"host": "tools.example.test", "path": "/foo", "prefix": "strip"},
            {"host": "alias.example.test", "path": "/bar", "prefix": "preserve"}])
        self.platform.deploy(self.spec)
        route = self.platform.backend.get("IngressRoute", self.cfg.namespace("hello"), "app")
        self.assertIn("Path(`/foo`) || PathPrefix(`/foo/`)", route["spec"]["routes"][0]["match"])
        self.assertEqual(route["spec"]["routes"][0]["services"], route["spec"]["routes"][1]["services"])
        self.assertEqual(route["spec"]["routes"][1]["middlewares"], [{"name": "source-allowlist"}])
        self.spec["routes"] = [self.spec["routes"][1]]
        self.platform.update(self.spec)
        self.assertIsNone(self.platform.backend.get("Middleware", self.cfg.namespace("hello"), "strip-0"))
        self.cfg.routing_enabled = False
        self.platform.reconcile()
        self.assertIsNone(self.platform.backend.get("IngressRoute", self.cfg.namespace("hello"), "app"))

    def test_route_conflicts_segment_boundary_and_other_instance(self):
        self.spec.update(path_support=True, routes=[{"host": "shared.example.test", "path": "/foo", "prefix": "strip"}])
        self.platform.deploy(self.spec)
        other = sample("other")
        other.update(path_support=True, routes=[{"host": "shared.example.test", "path": "/foobar", "prefix": "preserve"}])
        self.platform.deploy(other)
        other["routes"][0]["path"] = "/foo/child"
        with self.assertRaisesRegex(HarborError, "conflicts"):
            self.platform.update(other)
        cfg2 = Config(state_dir=self.cfg.state_dir / "other-instance", instance="other", require_digest=False)
        with self.assertRaisesRegex(HarborError, "conflicts"):
            Platform(cfg2, backend=self.platform.backend).deploy(other)
        self.platform.remove("hello")
        self.platform.update(other)

    def test_unmanaged_router_conflict_fails_closed(self):
        self.platform.backend.apply({"apiVersion": "traefik.io/v1alpha1", "kind": "IngressRoute",
            "metadata": {"namespace": "existing", "name": "foreign", "labels": {}},
            "spec": {"routes": [{"match": "HostRegexp(`.*`)"}]}})
        with self.assertRaisesRegex(HarborError, "unmanaged"):
            self.platform.deploy(self.spec)
        self.assertIsNone(self.platform.backend.get("Namespace", None, self.cfg.namespace("hello")))

    def test_stale_live_route_still_reserves_hostname(self):
        self.cfg.routing_enabled = True
        self.cfg.allow_http = True
        self.cfg.route_cidrs = ["10.0.0.0/8"]
        self.platform.deploy(self.spec)
        record = self.platform._record("hello")
        record["state"] = "removed"  # Simulate crash after tombstone, before route deletion.
        self.platform._save("hello", record)
        other = sample("other")
        other["routes"] = self.spec["routes"]
        with self.assertRaisesRegex(HarborError, "live router"):
            self.platform.deploy(other)
        self.platform.reconcile()
        self.platform.deploy(other)

    def test_ownership_never_adopts_namespace_or_resource(self):
        namespace = self.cfg.namespace("hello")
        self.platform.backend.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {}}})
        with self.assertRaisesRegex(HarborError, "another owner"):
            self.platform.deploy(self.spec)

    def test_drift_repaired_without_new_revision(self):
        self.platform.deploy(self.spec)
        current = self.platform.backend.get("Deployment", self.cfg.namespace("hello"), "app")
        drifted = copy.deepcopy(current)
        drifted["spec"]["template"]["spec"]["containers"][0]["image"] = "docker.io/acme/demo:tampered"
        self.platform.backend.apply(drifted, current)
        self.assertIn("Deployment/app", self.platform.status("hello")["drift"])
        self.assertTrue(self.platform.reconcile()[0]["reconciled"])
        self.assertEqual(self.platform.status("hello")["drift"], [])
        self.assertEqual(self.platform.status("hello")["revision"], 1)

    def test_reconciliation_removes_unsafe_extra_fields(self):
        self.platform.deploy(self.spec)
        for kind in ("Deployment", "Service"):
            current = self.platform.backend.get(kind, self.cfg.namespace("hello"), "app")
            drifted = copy.deepcopy(current)
            if kind == "Deployment":
                drifted["spec"]["template"]["spec"]["hostNetwork"] = True
                drifted["spec"]["template"]["spec"]["containers"][0]["securityContext"]["capabilities"]["add"] = ["SYS_ADMIN"]
            else:
                drifted["spec"]["externalIPs"] = ["192.0.2.10"]
            self.platform.backend.apply(drifted, current)
        self.assertEqual(len(self.platform.status("hello")["drift"]), 2)
        self.platform.reconcile()
        self.assertEqual(self.platform.status("hello")["drift"], [])
        pod = self.platform.backend.get("Deployment", self.cfg.namespace("hello"), "app")["spec"]["template"]["spec"]
        self.assertNotIn("hostNetwork", pod)
        self.assertNotIn("add", pod["containers"][0]["securityContext"]["capabilities"])

    def test_metadata_identity_cannot_escape_app_scope(self):
        self.platform.deploy(self.spec)
        current = self.platform.backend.get("ConfigMap", self.cfg.namespace("hello"), RECORD)
        broken = copy.deepcopy(current)
        record = json.loads(broken["data"]["release.json"])
        record["desired"]["id"] = "other"
        broken["data"]["release.json"] = json.dumps(record)
        self.platform.backend.apply(broken, current)
        self.assertFalse(self.platform.reconcile()[0]["reconciled"])

    def test_partial_failure_leaves_durable_intent(self):
        original = self.platform.backend.apply
        def fail(desired, expected=None):
            if desired["kind"] == "Deployment":
                raise HarborError("injected failure")
            return original(desired, expected)
        self.platform.backend.apply = fail
        with self.assertRaises(HarborError):
            self.platform.deploy(self.spec)
        self.assertIn("Deployment/app", self.platform.status("hello")["drift"])
        self.platform.backend.apply = original
        self.platform.reconcile()
        self.assertTrue(self.platform.status("hello")["ready"])

    def test_volume_deletion_optin_confirmation_and_pod_guard(self):
        self.platform.deploy(self.spec)
        self.platform.remove("hello")
        with self.assertRaises(HarborError):
            self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.cfg.allow_volume_delete = True
        with self.assertRaises(HarborError):
            self.platform.volumes_delete("hello", ["state"], "yes")
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": self.cfg.namespace("hello"),
               "name": "terminating", "labels": {**self.platform.selector, APP: "hello"}}}
        live = self.platform.backend.apply(pod)
        with self.assertRaisesRegex(HarborError, "Pods"):
            self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.platform.backend.delete(live)
        self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.assertEqual(self.platform.status("hello")["volumes"], [])
        self.platform.volumes_delete("hello", ["state"], "DELETE DATA hello")
        self.assertIsNotNone(self.platform.backend.get("ConfigMap", self.cfg.namespace("hello"), RECORD))

    def test_volume_removed_from_spec_is_retained_and_resizing_rejected(self):
        self.platform.deploy(self.spec)
        self.platform.update({**self.spec, "volumes": []})
        self.assertEqual(len(self.platform.status("hello")["volumes"]), 1)
        larger = copy.deepcopy(self.spec)
        larger["volumes"][0]["size_gi"] = 2
        with self.assertRaisesRegex(HarborError, "resizing"):
            self.platform.update(larger)

    def test_log_policy_and_redaction(self):
        self.platform.deploy(self.spec)
        self.platform.backend.logs = lambda *_: "authorization: Bearer supersecret\ntoken=abcdef\nhttps://user:password@example.test\n"
        text = self.platform.logs("hello")["logs"]
        for secret in ("supersecret", "abcdef", "user:password"):
            self.assertNotIn(secret, text)
        self.cfg.log_safe_apps = []
        with self.assertRaisesRegex(HarborError, "Logs disabled"):
            self.platform.logs("hello")

    def test_list_watch_observes_changes_and_reconciles(self):
        self.platform.deploy(self.spec)
        snapshot = self.platform.backend.list("Service", self.platform.selector)
        live = self.platform.backend.get("Service", self.cfg.namespace("hello"), "app")
        self.platform.backend.delete(live)
        events = list(self.platform.backend.watch("Service", self.platform.selector, snapshot["metadata"]["resourceVersion"], .1))
        self.assertEqual(events[0]["type"], "DELETED")
        result = self.platform.watch_round(timeout=1)
        self.assertTrue(result["reconcile"][0]["reconciled"])
        self.assertEqual(self.platform.status("hello")["drift"], [])

    def test_expired_watch_relists(self):
        self.platform.deploy(self.spec)
        original = self.platform.backend.watch
        def expired(*args, **kwargs):
            raise Expired("expired")
            yield
        self.platform.backend.watch = expired
        self.assertTrue(all(w["relisted"] for w in self.platform.watch_round(timeout=1)["watches"]))
        self.platform.backend.watch = original

    def test_tombstone_reconciles_reintroduced_runtime(self):
        self.platform.deploy(self.spec)
        self.platform.remove("hello")
        value = next(v for v in render(self.cfg, validate_spec(self.spec, self.cfg), 1) if v["kind"] == "Service")
        self.platform.backend.apply(value)
        self.assertTrue(self.platform.status("hello")["drift"])
        self.platform.reconcile()
        self.assertFalse(self.platform.status("hello")["drift"])


class InputTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(require_digest=False)

    def test_digest_required_and_registry_restriction(self):
        cfg = Config()
        with self.assertRaisesRegex(HarborError, "digest"):
            validate_spec(sample(), cfg)
        raw = sample()
        raw["image"] = "acme/demo@sha256:" + "a" * 64
        self.assertIn("@sha256:", validate_spec(raw, cfg)["image"])
        for image in ("acme/demo", "acme/demo:latest", "ghcr.io/acme/demo:v1", "demo;echo bad:v1"):
            with self.subTest(image=image), self.assertRaises(HarborError):
                validate_spec({**raw, "image": image}, self.cfg)

    def test_paths_hosts_and_canonical_url(self):
        for route in ({"host": "a.example.test", "path": "/foo", "prefix": "strip"},
                      {"host": "*.example.test", "path": "/", "prefix": "strip"},
                      {"host": "a.example.test", "path": "/%2e%2e", "prefix": "strip"},
                      {"host": "a.example.test", "path": "/foo/", "prefix": "strip"}):
            with self.subTest(route=route), self.assertRaises(HarborError):
                validate_spec({**sample(), "routes": [route]}, self.cfg)
        with self.assertRaises(HarborError):
            validate_spec({**sample(), "canonical_url": "https://other.example.test/"}, self.cfg)

    def test_secrets_and_arbitrary_fields_rejected(self):
        for mutation in ({"env": {"API_TOKEN": "secret"}}, {"env": {"DATABASE_URL": "postgres://u:pw@db/test"}},
                         {"command": ["sh"]}, {"privileged": True}, {"port": True}, {"id": "../hello"},
                         {"repo": "https://user:pass@example.test/project"}):
            with self.subTest(mutation=mutation), self.assertRaises(HarborError):
                validate_spec({**sample(), **mutation}, self.cfg)

    def test_https_and_source_allowlist_required_when_enabled(self):
        cfg = Config(routing_enabled=True, require_digest=False)
        with self.assertRaisesRegex(HarborError, "TLS"):
            validate_spec(sample(), cfg)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            for data in ({"routing_enabled": True}, {"route_cidrs": ["0.0.0.0/0"]}, {"backend": "kubectl"}):
                config.write_text(json.dumps(data))
                with self.subTest(data=data), self.assertRaises(HarborError):
                    Config.load(config)
