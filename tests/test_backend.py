import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from imageharbor.backend import Expired, FakeBackend, KubectlBackend, api_path
from imageharbor.model import Config, HarborError


class BackendTests(unittest.TestCase):
    def test_api_paths_are_fixed(self):
        self.assertEqual(api_path("Deployment", "ih-lab-hello", "app"), "/apis/apps/v1/namespaces/ih-lab-hello/deployments/app")
        self.assertEqual(api_path("Namespace", None, "ih-lab-hello"), "/api/v1/namespaces/ih-lab-hello")

    def test_pagination_and_explicit_context(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            token = "continue=" in args[-1]
            body = {"metadata": {"resourceVersion": "42", "continue": "" if token else "opaque"}, "items": [{"id": 2 if token else 1}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        backend = KubectlBackend(Config(kubeconfig="/tmp/explicit", kube_context="test-only"), runner)
        self.assertEqual(len(backend.list("Deployment", {"imageharbor.io/instance": "lab"})["items"]), 2)
        self.assertEqual(calls[0][0][:5], ["kubectl", "--kubeconfig", "/tmp/explicit", "--context", "test-only"])
        self.assertIn("labelSelector=", calls[0][0][-1])
        self.assertFalse(calls[0][1].get("shell", False))

    def test_errors_do_not_expose_subprocess_output(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "Forbidden sensitive-token")
        backend = KubectlBackend(Config(kubeconfig="/tmp/explicit", kube_context="test-only"), runner)
        with self.assertRaises(HarborError) as error:
            backend.list("Pod", {})
        self.assertNotIn("sensitive-token", str(error.exception))

    def test_fake_compare_and_swap_and_delete_preconditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend(Path(tmp))
            spec = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "app", "namespace": "ns", "labels": {}}}
            first = backend.apply(spec)
            second = backend.apply(spec, first)
            with self.assertRaises(HarborError):
                backend.apply(spec, first)
            with self.assertRaises(HarborError):
                backend.delete(first)
            backend.delete(second)


class LocalAPIServer:
    """A localhost-only Kubernetes transport fixture, not a Kubernetes runtime."""
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeBackend(Path(self.temp.name))
        self.requests = []
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, value):
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def parse(self):
                url = urlsplit(self.path)
                bits = url.path.split("/")
                root = 3 if bits[1] == "api" else 4
                remaining = bits[root:]
                namespace = None
                if len(remaining) >= 3 and remaining[0] == "namespaces":
                    namespace, remaining = remaining[1], remaining[2:]
                mapping = {"namespaces": "Namespace", "configmaps": "ConfigMap", "deployments": "Deployment", "services": "Service"}
                kind = mapping[remaining[0]]
                resource_name = remaining[1] if len(remaining) > 1 else None
                return kind, namespace, resource_name, parse_qs(url.query)

            def do_GET(self):
                fixture.requests.append(("GET", self.path, None))
                kind, ns, resource_name, query = self.parse()
                if resource_name:
                    value = fixture.backend.get(kind, ns, resource_name)
                    if value:
                        return self.send(200, value)
                    return self.send(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "NotFound", "message": "fixture resource not found", "code": 404})
                selector = dict(part.split("=", 1) for part in query.get("labelSelector", [""])[0].split(",") if part)
                if query.get("watch") == ["true"]:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if query.get("resourceVersion") == ["expired"]:
                        self.wfile.write(json.dumps({"type": "ERROR", "object": {"kind": "Status", "code": 410}}).encode() + b"\n")
                    else:
                        for event in fixture.backend.watch(kind, selector, query["resourceVersion"][0], .1):
                            self.wfile.write(json.dumps(event).encode() + b"\n")
                    return
                page = fixture.backend.list(kind, selector)
                page.update(apiVersion="v1", kind="List")
                self.send(200, page)

            def read_body(self):
                if self.headers.get("Transfer-Encoding") == "chunked":
                    chunks = []
                    while True:
                        size = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.read(2)
                    return json.loads(b"".join(chunks))
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def do_POST(self):
                body = self.read_body()
                fixture.requests.append(("POST", self.path, body))
                self.send(201, fixture.backend.apply(body))

            def do_PUT(self):
                body = self.read_body()
                fixture.requests.append(("PUT", self.path, body))
                kind, ns, resource_name, _ = self.parse()
                expected = fixture.backend.get(kind, ns, resource_name)
                if expected["metadata"]["resourceVersion"] != body["metadata"]["resourceVersion"]:
                    return self.send(409, {"kind": "Status", "apiVersion": "v1", "reason": "Conflict", "status": "Failure", "code": 409})
                self.send(200, fixture.backend.apply(body, expected))

            def do_DELETE(self):
                body = self.read_body()
                fixture.requests.append(("DELETE", self.path, body))
                kind, ns, resource_name, _ = self.parse()
                current = fixture.backend.get(kind, ns, resource_name)
                self.assert_preconditions(current, body)
                fixture.backend.delete(current)
                self.send(200, {"kind": "Status", "apiVersion": "v1", "status": "Success"})

            @staticmethod
            def assert_preconditions(current, body):
                assert body["preconditions"] == {k: current["metadata"][k] for k in ("uid", "resourceVersion")}

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError:
            self.temp.cleanup()
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = Path(self.temp.name) / "kubeconfig.json"
        self.config.write_text(json.dumps({"apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "fixture", "cluster": {"server": f"http://127.0.0.1:{self.server.server_port}"}}],
            "users": [{"name": "fixture", "user": {}}], "contexts": [{"name": "fixture", "context": {"cluster": "fixture", "user": "fixture"}}],
            "current-context": "fixture"}))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()


@unittest.skipUnless(shutil.which("kubectl"), "kubectl not installed; localhost transport contract skipped")
class KubectlTransportTests(unittest.TestCase):
    def setUp(self):
        try:
            self.fixture = LocalAPIServer()
        except PermissionError:
            self.skipTest("Sandbox denies localhost bind; rerun transport tests with localhost permission")
        self.addCleanup(self.fixture.close)
        self.backend = KubectlBackend(Config(kubeconfig=str(self.fixture.config), kube_context="fixture"))

    def test_real_kubectl_localhost_get_create_list_watch_replace_delete(self):
        self.assertIsNone(self.backend.get("ConfigMap", "ns", "release"))
        desired = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "release", "namespace": "ns", "labels": {"imageharbor.io/instance": "lab"}}, "data": {"value": "v1"}}
        created = self.backend.apply(desired)
        self.assertEqual(self.backend.get("ConfigMap", "ns", "release")["data"], {"value": "v1"})
        snapshot = self.backend.list("ConfigMap", {"imageharbor.io/instance": "lab"})
        updated = self.backend.apply({**desired, "data": {"value": "v2"}}, created)
        events = list(self.backend.watch("ConfigMap", {"imageharbor.io/instance": "lab"}, snapshot["metadata"]["resourceVersion"], 1))
        self.assertEqual(events[0]["type"], "MODIFIED")
        self.backend.delete(updated)
        self.assertIsNone(self.backend.get("ConfigMap", "ns", "release"))
        methods = [method for method, _, _ in self.fixture.requests]
        self.assertIn("POST", methods)
        self.assertIn("PUT", methods)
        self.assertIn("DELETE", methods)

    def test_real_kubectl_watch_410(self):
        with self.assertRaises(Expired):
            list(self.backend.watch("ConfigMap", {}, "expired", 1))
