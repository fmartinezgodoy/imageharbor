"""Kubernetes adapter and a small persistent simulation (not a real cluster)."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from .model import HarborError

API = {
    "Namespace": ("v1", "namespaces"),
    "ConfigMap": ("v1", "configmaps"),
    "Service": ("v1", "services"),
    "PersistentVolumeClaim": ("v1", "persistentvolumeclaims"),
    "Pod": ("v1", "pods"),
    "Deployment": ("apps/v1", "deployments"),
    "NetworkPolicy": ("networking.k8s.io/v1", "networkpolicies"),
    "IngressRoute": ("traefik.io/v1alpha1", "ingressroutes"),
    "Middleware": ("traefik.io/v1alpha1", "middlewares"),
}
WATCH_KINDS = tuple(API)


class Expired(HarborError):
    pass


def api_path(kind, namespace=None, resource_name=None):
    group, plural = API[kind]
    root = "/api/v1" if group == "v1" else "/apis/" + group
    if namespace:
        root += "/namespaces/" + namespace
    return root + "/" + plural + ("/" + resource_name if resource_name else "")


@contextmanager
def locked(filename):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(filename, os.O_CREAT | os.O_RDWR, 0o600), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_json(filename, data):
    temp = filename.with_suffix(".tmp")
    with os.fdopen(os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as file:
        json.dump(data, file, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, filename)


def bind_state(cfg):
    """Prevent an existing queue/state directory from being pointed at a new scope.

    This guards accidental config reuse, not an administrator retargeting the
    contents of a trusted kubeconfig. Cluster identity is additionally checked
    by namespace/release UIDs on scoped operations.
    """
    identity = {"instance": cfg.instance, "backend": cfg.backend,
                "kube_context": cfg.kube_context, "kubeconfig": cfg.kubeconfig}
    file = cfg.state_dir / "instance-binding.json"
    with locked(cfg.state_dir / "instance-binding.lock"):
        if file.exists():
            try:
                existing = json.loads(file.read_text())
            except (OSError, ValueError):
                raise HarborError("Invalid local instance binding; operator recovery required") from None
            if existing != identity:
                raise HarborError("State directory belongs to another instance/backend/context; use an isolated state directory")
        else:
            atomic_json(file, identity)


def merged(current, desired):
    result = copy.deepcopy(current)
    for key, value in desired.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def satisfies(current, desired):
    """Compare owned fields, ignoring API defaults, status and unrelated labels."""
    if isinstance(desired, dict):
        return isinstance(current, dict) and all(k in current and satisfies(current[k], v) for k, v in desired.items())
    if isinstance(desired, list):
        return isinstance(current, list) and len(current) == len(desired) and all(satisfies(a, b) for a, b in zip(current, desired))
    return current == desired


def resource_matches(current, desired):
    if not current or not satisfies(current, desired):
        return False
    if desired["kind"] == "Deployment":
        pod = current["spec"]["template"]["spec"]
        if any(pod.get(key, False) for key in ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace")):
            return False
        if pod.get("initContainers") or pod.get("ephemeralContainers"):
            return False
        if any(container.get("command") or container.get("args") for container in pod["containers"]):
            return False
        for container in pod["containers"]:
            security = container.get("securityContext", {})
            if security.get("privileged") or security.get("capabilities", {}).get("add"):
                return False
            if security.get("runAsNonRoot") is False or security.get("runAsUser", pod["securityContext"]["runAsUser"]) != pod["securityContext"]["runAsUser"]:
                return False
            if security.get("seccompProfile", {}).get("type", "RuntimeDefault") != "RuntimeDefault":
                return False
    if desired["kind"] == "Service":
        if current["spec"].get("externalIPs") or any(p.get("nodePort") for p in current["spec"]["ports"]):
            return False
    if desired["kind"] == "IngressRoute" and not desired["spec"].get("tls") and current["spec"].get("tls"):
        return False
    return True


def replacement(current, desired):
    value = merged(current or {}, desired)
    if "spec" in desired:
        value["spec"] = copy.deepcopy(desired["spec"])
        if current and desired["kind"] == "Service":
            for key in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy"):
                if key in current["spec"]:
                    value["spec"][key] = current["spec"][key]
    return value


class FakeBackend:
    simulated = True

    def __init__(self, state_dir):
        self.file = Path(state_dir) / "fake-cluster.json"
        self.lock = Path(state_dir) / "fake-cluster.lock"

    def _read(self):
        if not self.file.exists():
            return {"rv": 0, "floor": 0, "objects": {}, "events": []}
        return json.loads(self.file.read_text())

    @staticmethod
    def _key(kind, namespace, resource_name):
        return f"{kind}/{namespace or ''}/{resource_name}"

    def get(self, kind, namespace, resource_name):
        with locked(self.lock):
            return copy.deepcopy(self._read()["objects"].get(self._key(kind, namespace, resource_name)))

    def list(self, kind, selector):
        with locked(self.lock):
            data = self._read()
            return {"metadata": {"resourceVersion": str(data["rv"])}, "items": [
                copy.deepcopy(o) for o in data["objects"].values()
                if o["kind"] == kind and self._selected(o, selector)]}

    @staticmethod
    def _selected(value, selector):
        return all(value["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())

    def apply(self, desired, expected=None):
        with locked(self.lock):
            data = self._read()
            meta = desired["metadata"]
            key = self._key(desired["kind"], meta.get("namespace"), meta["name"])
            current = data["objects"].get(key)
            if (expected is None and current) or (expected and (not current or current["metadata"]["resourceVersion"] != expected["metadata"]["resourceVersion"])):
                raise HarborError("Concurrent resource change; retry after refreshing state")
            value = replacement(current, desired)
            data["rv"] += 1
            value["metadata"].update(resourceVersion=str(data["rv"]), uid=(current or {}).get("metadata", {}).get("uid", str(uuid.uuid4())))
            if desired["kind"] == "Deployment":
                generation = (current or {}).get("metadata", {}).get("generation", 0) + 1
                value["metadata"]["generation"] = generation
                value["status"] = {"observedGeneration": generation, "readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1}
            if desired["kind"] == "PersistentVolumeClaim":
                value["status"] = {"phase": "Bound"}
            data["objects"][key] = value
            self._event(data, "MODIFIED" if current else "ADDED", value)
            atomic_json(self.file, data)
            return copy.deepcopy(value)

    @staticmethod
    def _event(data, event_type, value):
        data["events"].append({"type": event_type, "object": copy.deepcopy(value)})
        if len(data["events"]) > 1000:
            data["floor"] = int(data["events"].pop(0)["object"]["metadata"]["resourceVersion"])

    def delete(self, current):
        with locked(self.lock):
            data = self._read()
            meta = current["metadata"]
            key = self._key(current["kind"], meta.get("namespace"), meta["name"])
            found = data["objects"].get(key)
            if not found:
                return
            if any(found["metadata"][k] != meta[k] for k in ("uid", "resourceVersion")):
                raise HarborError("Concurrent resource change; deletion refused")
            del data["objects"][key]
            data["rv"] += 1
            found["metadata"]["resourceVersion"] = str(data["rv"])
            self._event(data, "DELETED", found)
            atomic_json(self.file, data)

    def watch(self, kind, selector, resource_version, timeout=5):
        deadline = time.monotonic() + timeout
        cursor = int(resource_version)
        while time.monotonic() < deadline:
            with locked(self.lock):
                data = self._read()
                if cursor < data["floor"]:
                    raise Expired("Watch resource version expired; relist required")
                events = [e for e in data["events"] if int(e["object"]["metadata"]["resourceVersion"]) > cursor]
            for event in events:
                cursor = int(event["object"]["metadata"]["resourceVersion"])
                if event["object"]["kind"] == kind and self._selected(event["object"], selector):
                    yield event
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def logs(self, namespace, tail):
        return "[FAKE] simulated application log\n"


class KubectlBackend:
    simulated = False

    def __init__(self, cfg, runner=subprocess.run):
        self.base = ["kubectl", "--kubeconfig", cfg.kubeconfig, "--context", cfg.kube_context]
        self.runner = runner

    def _run(self, args, body=None, allow_missing=False):
        try:
            result = self.runner(self.base + ["--request-timeout=30s"] + args,
                                 input=json.dumps(body) if body is not None else None,
                                 text=True, capture_output=True, timeout=35, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HarborError("kubectl unavailable or timed out; verify explicit context and connectivity") from None
        if result.returncode and allow_missing and "(NotFound)" in result.stderr:
            return ""
        if result.returncode:
            # stderr can echo manifests, config or API credentials; do not surface it.
            raise HarborError("Kubernetes request failed; check connectivity, RBAC, CRDs and resource conflicts")
        return result.stdout

    def get(self, kind, namespace, resource_name):
        result = self._run(["get", "--raw", api_path(kind, namespace, resource_name)], allow_missing=True)
        return json.loads(result) if result.strip() else None

    @staticmethod
    def _selector(selector):
        return ",".join(f"{k}={v}" for k, v in sorted(selector.items()))

    def list(self, kind, selector):
        url = api_path(kind) + "?" + urlencode({"labelSelector": self._selector(selector), "limit": 200})
        items = []
        while True:
            page = json.loads(self._run(["get", "--raw", url]))
            items.extend(page.get("items", []))
            continuation = page["metadata"].get("continue")
            if not continuation:
                return {"metadata": {"resourceVersion": page["metadata"]["resourceVersion"]}, "items": items}
            url = api_path(kind) + "?" + urlencode({"labelSelector": self._selector(selector), "limit": 200, "continue": continuation})

    def apply(self, desired, expected=None):
        value = replacement(expected, desired)
        value.pop("status", None)
        value["metadata"].pop("managedFields", None)
        # replace sends resourceVersion; create fails if someone won the race.
        command = "replace" if expected else "create"
        meta = desired["metadata"]
        url = api_path(desired["kind"], meta.get("namespace"), meta["name"] if expected else None)
        return json.loads(self._run([command, "--raw", url + "?fieldValidation=Strict", "-f", "-"], value))

    def delete(self, current):
        meta = current["metadata"]
        body = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background",
                "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}}
        self._run(["delete", "--raw", api_path(current["kind"], meta.get("namespace"), meta["name"]), "-f", "-"], body)

    def watch(self, kind, selector, resource_version, timeout=5):
        url = api_path(kind) + "?" + urlencode({"labelSelector": self._selector(selector), "resourceVersion": resource_version,
                                               "watch": "true", "allowWatchBookmarks": "true", "timeoutSeconds": timeout})
        try:
            process = subprocess.Popen(self.base + [f"--request-timeout={timeout + 5}s", "get", "--raw", url],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                for line in process.stdout:
                    event = json.loads(line)
                    if event.get("type") == "ERROR":
                        if event.get("object", {}).get("code") == 410:
                            raise Expired("Watch expired; relist required")
                        raise HarborError("Kubernetes watch failed")
                    yield event
                if process.wait(timeout=5):
                    raise HarborError("Kubernetes watch disconnected; retry with relist")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise HarborError("Kubernetes watch transport failed") from None

    def logs(self, namespace, tail):
        return self._run(["logs", "deployment/app", "--namespace", namespace, "--container", "app", "--tail", str(tail), "--limit-bytes", "32768"])
