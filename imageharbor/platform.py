"""Desired metadata + live LIST/WATCH + idempotent reconciliation."""
from __future__ import annotations

import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .backend import Expired, FakeBackend, KubectlBackend, WATCH_KINDS, locked, satisfies, resource_matches, bind_state
from .manifests import APP, INSTANCE, MANAGED, ROLE, labels, render, resource
from .model import Config, HarborError, integer, name, obj, overlaps, path, sequence, string, validate_spec

RECORD = "imageharbor-release"


class Platform:
    def __init__(self, cfg, backend=None):
        self.cfg = cfg
        bind_state(cfg)
        self.backend = backend or (FakeBackend(cfg.state_dir) if cfg.backend == "fake" else KubectlBackend(cfg))
        self.selector = {MANAGED: "imageharbor", INSTANCE: cfg.instance}
        self.writer_lock = cfg.state_dir / "writer.lock"

    def _owned(self, current, app, allow_deleting=False):
        if current and not all(current["metadata"].get("labels", {}).get(k) == v for k, v in labels(self.cfg, app).items()):
            raise HarborError("Resource belongs to another owner; refusing adoption or mutation")
        roles = {"Namespace": "identity", "ConfigMap": "identity", "PersistentVolumeClaim": "data", "NetworkPolicy": "policy"}
        if current and current["kind"] != "Pod" and current["metadata"].get("labels", {}).get(ROLE) != roles.get(current["kind"], "runtime"):
            raise HarborError("Resource ownership role does not match; refusing mutation")
        if current and current["metadata"].get("deletionTimestamp") and not allow_deleting:
            raise HarborError("Resource is being deleted; wait before retrying")
        return current

    def _get(self, kind, app, resource_name, allow_deleting=False):
        return self._owned(self.backend.get(kind, None if kind == "Namespace" else self.cfg.namespace(app), resource_name), app, allow_deleting)

    def _apply(self, desired, app):
        current = self._get(desired["kind"], app, desired["metadata"]["name"])
        if current and resource_matches(current, desired):
            return current
        return self.backend.apply(desired, current)

    @staticmethod
    def _decode(cm):
        try:
            record = json.loads(cm["data"]["release.json"])
            obj(record, ("schema", "state", "desired", "revision", "history", "volumes", "namespace_uid"),
                ("schema", "state", "desired", "revision", "history", "volumes"))
            if type(record["schema"]) is not int or record["schema"] != 1 or record["state"] not in ("active", "removed") or not record["history"]:
                raise ValueError()
            integer(record["revision"], 1, 2**31 - 1)
            neutral = Config(require_digest=False, allow_http=True)
            validate_spec(record["desired"], neutral)
            app = name(record["desired"]["id"])
            if not isinstance(record["volumes"], dict):
                raise ValueError()
            if record.get("namespace_uid") is not None:
                string(record["namespace_uid"], 100)
            revisions = []
            for entry in sequence(record["history"], 20):
                obj(entry, ("revision", "spec", "created_at", "rollback_of"), ("revision", "spec", "created_at"))
                integer(entry["revision"], 1, record["revision"])
                integer(entry["created_at"], 0, 2**63 - 1)
                validate_spec(entry["spec"], neutral)
                if "rollback_of" in entry:
                    integer(entry["rollback_of"], 1, record["revision"])
                if entry["spec"]["id"] != app:
                    raise ValueError()
                revisions.append(entry["revision"])
            if revisions != sorted(set(revisions)) or revisions[-1] != record["revision"] or record["history"][-1]["spec"] != record["desired"]:
                raise ValueError()
            for key, volume in record["volumes"].items():
                name(key)
                obj(volume, ("name", "mount_path", "size_gi", "uid", "state"), ("name", "mount_path", "size_gi"))
                if volume["name"] != key:
                    raise ValueError()
                path(volume["mount_path"])
                integer(volume["size_gi"], 1, 20)
                if volume.get("uid") is not None:
                    string(volume["uid"], 100)
                if volume.get("state", "active") not in ("active", "delete_requested", "deleted"):
                    raise ValueError()
            record["_cm"] = cm
            return record
        except (HarborError, KeyError, IndexError, TypeError, ValueError):
            raise HarborError("Invalid deployment metadata; manual recovery required") from None

    def _record(self, app):
        name(app)
        cm = self._get("ConfigMap", app, RECORD)
        if not cm:
            return None
        record = self._decode(cm)
        if record.get("desired", {}).get("id") != app:
            raise HarborError("Deployment metadata identity does not match its owner")
        self._namespace_identity(app, record)
        return record

    def _namespace_identity(self, app, record):
        namespace = self._get("Namespace", app, self.cfg.namespace(app))
        if not namespace or (record.get("namespace_uid") and record["namespace_uid"] != namespace["metadata"]["uid"]):
            raise HarborError("Application namespace identity changed or disappeared; refusing operation")
        return namespace

    def _save(self, app, record):
        namespace = self._namespace_identity(app, record)
        record["namespace_uid"] = namespace["metadata"]["uid"]
        text = json.dumps({k: v for k, v in record.items() if not k.startswith("_")}, separators=(",", ":"))
        if len(text.encode()) > 800_000:
            raise HarborError("Release history exceeds safe ConfigMap size")
        current = self._get("ConfigMap", app, RECORD)
        expected = record.get("_cm")
        if (current is None) != (expected is None) or (current and any(current["metadata"][key] != expected["metadata"][key] for key in ("uid", "resourceVersion"))):
            raise HarborError("Deployment metadata changed concurrently; refresh before retrying")
        desired = resource(self.cfg, app, "ConfigMap", RECORD, {"data": {"release.json": text}}, role="identity")
        record["_cm"] = current if resource_matches(current, desired) else self.backend.apply(desired, expected)

    def _records(self):
        results = []
        for cm in self.backend.list("ConfigMap", self.selector)["items"]:
            if cm["metadata"]["name"] != RECORD:
                continue
            app = "unknown"
            try:
                app = name(cm["metadata"].get("labels", {}).get(APP))
                self._owned(cm, app)
                if cm["metadata"].get("namespace") != self.cfg.namespace(app):
                    raise HarborError("Deployment metadata lies outside its owner's namespace")
                record = self._decode(cm)
                if record.get("desired", {}).get("id") != app:
                    raise HarborError("Deployment metadata identity does not match its owner")
                self._namespace_identity(app, record)
            except HarborError as error:
                record = error
            results.append((app, record))
        return results

    def _conflicts(self, spec):
        if not spec["routes"]:
            return
        # Include other ImageHarbor instances: hostname ownership is cluster-wide.
        for cm in self.backend.list("ConfigMap", {MANAGED: "imageharbor"})["items"]:
            if cm["metadata"]["name"] != RECORD:
                continue
            same = cm["metadata"].get("namespace") == self.cfg.namespace(spec["id"])
            record = self._decode(cm)
            if same or record["state"] != "active":
                continue
            for old in record["desired"]["routes"]:
                for new in spec["routes"]:
                    if old["host"] == new["host"] and overlaps(old["path"], new["path"]):
                        raise HarborError("Hostname/path conflicts with another application")
        # Never silently take traffic from manually installed CRD routers. We can
        # only prove disjointness for simple exact Host rules; fail closed otherwise.
        for ingress in self.backend.list("IngressRoute", {})["items"]:
            if ingress["metadata"].get("namespace") == self.cfg.namespace(spec["id"]) and all(
                    ingress["metadata"].get("labels", {}).get(k) == v for k, v in labels(self.cfg, spec["id"]).items()):
                continue
            for route in ingress.get("spec", {}).get("routes", []):
                rule = route.get("match", "")
                exact = re.fullmatch(r"Host\(`([a-z0-9.-]+)`\)(?: && \(Path\(`([^`]+)`\) \|\| PathPrefix\(`\2/`\)\))?", rule)
                if spec["routes"] and not exact:
                    raise HarborError("Cannot prove route is disjoint from an unmanaged router")
                if exact:
                    live_host, live_path = exact[1], exact[2] or "/"
                    if any(r["host"] == live_host and overlaps(r["path"], live_path) for r in spec["routes"]):
                        raise HarborError("Hostname/path conflicts with a live router; reconcile its owner first")

    def _preflight(self, spec, record, revision):
        self._conflicts(spec)
        ledger = (record or {}).get("volumes", {})
        for vol in spec["volumes"]:
            old = ledger.get(vol["name"])
            if old and old["size_gi"] != vol["size_gi"]:
                raise HarborError("Volume resizing is outside this first version; preserve the existing size")
        for desired in render(self.cfg, spec, revision):
            self._get(desired["kind"], spec["id"], desired["metadata"]["name"])
        self._get("ConfigMap", spec["id"], RECORD)
        if record:
            for vol in spec["volumes"]:
                self._volume(spec["id"], record, vol["name"], require_present=True)

    def _volume(self, app, record, volume, require_present=False, allow_deleting=False):
        entry = record["volumes"].get(volume, {})
        live = self._get("PersistentVolumeClaim", app, "data-" + volume, allow_deleting)
        if live and live["metadata"].get("ownerReferences"):
            raise HarborError("PVC has ownerReferences; retention cannot be guaranteed")
        if live and entry.get("uid") and entry["uid"] != live["metadata"]["uid"]:
            raise HarborError("PVC identity changed; refusing adoption or deletion of a replacement")
        if require_present and not live and entry.get("uid"):
            raise HarborError("Tracked PVC is missing; refusing to create an empty replacement")
        if require_present and entry.get("state") == "delete_requested":
            raise HarborError("PVC deletion is pending; finish explicit data deletion before deploying")
        return live

    def deploy(self, spec):
        return self._release(spec, update=False)

    def update(self, spec):
        return self._release(spec, update=True)

    def _release(self, raw, update):
        spec = validate_spec(raw, self.cfg)
        app = spec["id"]
        with locked(self.writer_lock):
            record = self._record(app)
            active = record is not None and record["state"] == "active"
            if update and not active:
                raise HarborError("Update requires an active application")
            if not update and active:
                if record["desired"] == spec:
                    self._reconcile_one(app, record)
                    return self.status(app)
                raise HarborError("Application already exists; use update")
            revision = (record or {}).get("revision", 0) + 1
            self._preflight(spec, record, revision)
            if record and active and record["desired"] == spec:
                self._reconcile_one(app, record)
                return self.status(app)
            record = record or {"schema": 1, "revision": 0, "history": [], "volumes": {}}
            record.update(state="active", desired=spec, revision=revision)
            record["history"] = (record["history"] + [{"revision": revision, "spec": spec, "created_at": int(time.time())}])[-20:]
            for volume in spec["volumes"]:
                record["volumes"][volume["name"]] = {**record["volumes"].get(volume["name"], {}), **copy.deepcopy(volume)}
            namespace = self._apply(render(self.cfg, spec, revision)[0], app)
            record["namespace_uid"] = namespace["metadata"]["uid"]
            # Journal intent before workload mutation. Failure leaves repairable,
            # visible desired state, never a falsely successful deployment.
            self._save(app, record)
            self._reconcile_one(app, record)
            return self.status(app)

    def rollback(self, app_id, revision):
        name(app_id)
        integer(revision, 1, 2**31 - 1)
        with locked(self.writer_lock):
            record = self._record(app_id)
            if not record or record["state"] != "active":
                raise HarborError("Rollback requires an active application")
            target = next((entry for entry in record["history"] if entry["revision"] == revision), None)
            if not target:
                raise HarborError("Revision is outside retained history")
            spec = validate_spec(target["spec"], self.cfg)
            next_revision = record["revision"] + 1
            self._preflight(spec, record, next_revision)
            record.update(desired=spec, revision=next_revision)
            record["history"] = (record["history"] + [{"revision": next_revision, "spec": spec,
                                                      "created_at": int(time.time()), "rollback_of": revision}])[-20:]
            self._save(app_id, record)
            self._reconcile_one(app_id, record)
            return self.status(app_id)

    def _runtime(self, app):
        result = []
        for kind in ("IngressRoute", "Middleware", "Deployment", "Service"):
            for value in self.backend.list(kind, {**self.selector, APP: app})["items"]:
                self._owned(value, app, allow_deleting=True)
                if value["metadata"].get("namespace") != self.cfg.namespace(app):
                    raise HarborError("Ownership labels found outside the application's namespace")
                if value["metadata"].get("labels", {}).get(ROLE) == "runtime":
                    allowed = value["metadata"]["name"] == "app" if kind != "Middleware" else bool(re.fullmatch(r"(?:source-allowlist|strip-(?:[0-9]|1[0-5]))", value["metadata"]["name"]))
                    if not allowed:
                        raise HarborError("Unregistered runtime resource carries this application's labels; refusing cleanup")
                    result.append(value)
        return result

    def _reconcile_one(self, app, record, check=None):
        check = check or (lambda: None)
        check()
        self._namespace_identity(app, record)
        if record["state"] == "removed":
            check()
            self._apply(resource(self.cfg, app, "NetworkPolicy", "app", {"spec": {
                "podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}}, role="policy"), app)
            for volume in record["volumes"]:
                check()
                self._volume(app, record, volume, allow_deleting=True)
            for live in self._runtime(app):
                check()
                if not live["metadata"].get("deletionTimestamp"):
                    self.backend.delete(live)
            return
        spec = validate_spec(record["desired"], self.cfg)
        if spec["id"] != app:
            raise HarborError("Deployment metadata identity does not match its owner")
        # Closing routes must not be blocked by an unrelated router or bad PVC.
        if not self.cfg.routing_enabled or not spec["routes"]:
            for live in self._runtime(app):
                if live["kind"] in ("IngressRoute", "Middleware") and not live["metadata"].get("deletionTimestamp"):
                    check()
                    self.backend.delete(live)
        self._conflicts(spec)
        desired = render(self.cfg, spec, record["revision"])
        keep = {(o["kind"], o["metadata"]["name"]) for o in desired}
        # PVCs removed from the spec stay in the ledger and the cluster.
        for value in desired:
            check()
            if value["kind"] == "PersistentVolumeClaim":
                volume = value["metadata"]["name"][5:]
                current = self._volume(app, record, volume, require_present=True)
                if current:
                    if not satisfies(current.get("spec", {}), value["spec"]):
                        raise HarborError("Live volume specification differs; manual data-safe recovery required")
                else:
                    check()
                    current = self._apply(value, app)
                if record["volumes"][volume].get("uid") != current["metadata"]["uid"] or record["volumes"][volume].get("state") != "active":
                    record["volumes"][volume].update(uid=current["metadata"]["uid"], state="active")
                    check()
                    self._save(app, record)
                continue
            check()
            self._apply(value, app)
        for live in self._runtime(app):
            if (live["kind"], live["metadata"]["name"]) not in keep:
                check()
                if not live["metadata"].get("deletionTimestamp"):
                    self.backend.delete(live)

    def reconcile(self):
        with locked(self.writer_lock):
            results = []
            for app, record in self._records():
                try:
                    if isinstance(record, HarborError):
                        raise record
                    self._reconcile_one(app, record)
                    results.append({"app_id": app, "reconciled": True})
                except HarborError as error:
                    results.append({"app_id": app, "reconciled": False, "error": str(error)})
            return results

    def watch_round(self, timeout=5):
        """LIST each kind, WATCH from its RV, then reconcile. 410 always relists.

        Independent kind snapshots are eventually consistent. Reconciliation
        rereads metadata under the writer lock. Bounded streams avoid leaked watchers.
        """
        snapshots = {kind: self.backend.list(kind, self.selector) for kind in WATCH_KINDS}

        def consume(kind):
            try:
                count = sum(1 for _ in self.backend.watch(kind, self.selector, snapshots[kind]["metadata"]["resourceVersion"], timeout))
                return {"kind": kind, "events": count, "relisted": False}
            except Expired:
                self.backend.list(kind, self.selector)
                return {"kind": kind, "events": 0, "relisted": True}

        with ThreadPoolExecutor(max_workers=len(WATCH_KINDS)) as pool:
            events = list(pool.map(consume, WATCH_KINDS))
        return {"watches": events, "reconcile": self.reconcile()}

    def remove(self, app_id):
        name(app_id)
        with locked(self.writer_lock):
            record = self._record(app_id)
            if not record:
                raise HarborError("Unknown application")
            for volume in record["volumes"]:
                self._volume(app_id, record, volume, allow_deleting=True)
            record["state"] = "removed"
            self._save(app_id, record)
            self._reconcile_one(app_id, record)
            return self.status(app_id)

    def volumes_delete(self, app_id, volumes, confirm):
        name(app_id)
        requested = sequence(volumes, 8)
        for volume in requested:
            name(volume)
        if not self.cfg.allow_volume_delete or confirm != f"DELETE DATA {app_id}" or not requested or len(set(requested)) != len(requested):
            raise HarborError("Data deletion requires instance opt-in, explicit volume names and exact confirmation")
        with locked(self.writer_lock):
            record = self._record(app_id)
            if not record or record["state"] != "removed":
                raise HarborError("Remove the application before deleting its data")
            ns = self.cfg.namespace(app_id)
            if any(o["metadata"].get("namespace") == ns for kind in ("Deployment", "Pod") for o in self.backend.list(kind, {})["items"]):
                raise HarborError("Wait for all application Pods and Deployment to disappear before deleting data")
            current = []
            for volume in requested:
                if volume not in record["volumes"]:
                    raise HarborError("Volume is outside the application's ownership ledger")
                live = self._volume(app_id, record, volume, allow_deleting=True)
                if live:
                    current.append(live)
                record["volumes"][volume]["state"] = "delete_requested"
            self._save(app_id, record)
            for live in current:
                if not live["metadata"].get("deletionTimestamp"):
                    self.backend.delete(live)
            pending = []
            for volume in requested:
                live = self._volume(app_id, record, volume, allow_deleting=True)
                if live:
                    pending.append(volume)
                else:
                    record["volumes"][volume].update(uid=None, state="deleted")
            self._save(app_id, record)
            # Keep the ledger: partial deletes are retryable, storage is not restored
            # for tombstones, and ownership history survives recreation.
            return {"app_id": app_id, "requested": requested, "pending": pending, "deletion_requested": True,
                    "note": "PVC deletion follows storage-class reclaim policy; verify PV/backend cleanup separately"}

    def inventory(self, app_id=None):
        result = []
        errors = []
        if app_id is not None:
            name(app_id)
            record = self._record(app_id)
            if not record:
                raise HarborError("Unknown application")
            records = [(app_id, record)]
        else:
            records = self._records()
        selected = self.selector if app_id is None else {**self.selector, APP: app_id}
        snapshots = {kind: self.backend.list(kind, selected)["items"] for kind in WATCH_KINDS}
        for app, record in records:
            if isinstance(record, HarborError):
                errors.append({"id": app, "error": str(record)})
                continue
            spec = record["desired"]
            live = [o for values in snapshots.values() for o in values if o["metadata"].get("labels", {}).get(APP) == app
                    and (o["metadata"].get("namespace") == self.cfg.namespace(app) or
                         (o["kind"] == "Namespace" and o["metadata"]["name"] == self.cfg.namespace(app)))]
            deployment = next((o for o in live if o["kind"] == "Deployment" and o["metadata"]["name"] == "app"), None)
            drift = []
            if record["state"] == "active":
                for desired in render(self.cfg, spec, record["revision"]):
                    current = next((o for o in live if o["kind"] == desired["kind"] and o["metadata"]["name"] == desired["metadata"]["name"]), None)
                    data_identity_bad = False
                    if desired["kind"] == "PersistentVolumeClaim" and current:
                        entry = record["volumes"].get(desired["metadata"]["name"][5:], {})
                        data_identity_bad = bool(current["metadata"].get("ownerReferences") or (entry.get("uid") and entry["uid"] != current["metadata"]["uid"]))
                    if not resource_matches(current, desired) or data_identity_bad:
                        drift.append(desired["kind"] + "/" + desired["metadata"]["name"])
            else:
                drift = [o["kind"] + "/" + o["metadata"]["name"] for o in live if o["kind"] in ("Deployment", "Service", "IngressRoute", "Middleware")]
            observed = deployment.get("status", {}) if deployment else {}
            ready = bool(record["state"] == "active" and deployment and observed.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 1)
                         and observed.get("updatedReplicas", 0) == 1 and observed.get("availableReplicas", 0) == 1 and not drift)
            result.append({"id": app, "namespace": self.cfg.namespace(app), "state": record["state"],
                           "revision": record["revision"], "purpose": spec["purpose"], "image": spec["image"],
                           "repo": spec["repo"], "routes": spec["routes"], "canonical_url": spec["canonical_url"],
                           "routing_enabled": self.cfg.routing_enabled, "resources": spec["resources"],
                           "backend": "fake" if self.backend.simulated else "kubernetes", "ready": ready,
                           "observed": observed, "drift": drift,
                           "observed_image": deployment["spec"]["template"]["spec"]["containers"][0]["image"] if deployment else None,
                           "live_resources": [{"kind": o["kind"], "name": o["metadata"]["name"],
                                               "uid": o["metadata"].get("uid")} for o in live],
                           "volumes": [{"name": o["metadata"]["name"], "uid": o["metadata"].get("uid"),
                                        "phase": o.get("status", {}).get("phase", "Unknown")} for o in live if o["kind"] == "PersistentVolumeClaim"],
                           "tracked_volumes": [{"name": volume, "expected_uid": entry.get("uid"), "state": entry.get("state", "active")}
                                               for volume, entry in record["volumes"].items()],
                           "history": [{k: v for k, v in h.items() if k != "spec"} for h in record["history"]]})
        known = {app for app, _ in records}
        orphans = sorted({o["metadata"].get("labels", {}).get(APP, "unknown") for values in snapshots.values() for o in values} - known)
        return {"applications": result, "metadata_errors": errors, "unregistered_owners": orphans, "simulated": self.backend.simulated}

    def status(self, app_id):
        name(app_id)
        for app in self.inventory(app_id)["applications"]:
            if app["id"] == app_id:
                return app
        raise HarborError("Unknown application")

    def task_scope(self, app_id):
        record = self._record(app_id)
        if not record:
            raise HarborError("Unknown application")
        namespace = self._namespace_identity(app_id, record)
        return {"instance": self.cfg.instance, "namespace_uid": namespace["metadata"]["uid"],
                "release_uid": record["_cm"]["metadata"]["uid"]}

    def check_task_scope(self, app_id, scope):
        if self.task_scope(app_id) != scope:
            raise HarborError("Task application identity changed; refusing stale capabilities")

    def logs(self, app_id, tail=100):
        name(app_id)
        integer(tail, 1, 500)
        if app_id not in self.cfg.log_safe_apps:
            raise HarborError("Logs disabled: operator must attest upstream log scrubbing via log_safe_apps")
        record = self._record(app_id)
        if not record or record["state"] != "active":
            raise HarborError("Logs require an active application")
        if not self._get("Deployment", app_id, "app"):
            raise HarborError("No owned Deployment is available for logs")
        # Defense in depth. Arbitrary secret values cannot be inferred from refs.
        text = self.backend.logs(self.cfg.namespace(app_id), tail)[:32768]
        text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"(://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
        return {"app_id": app_id, "simulated": self.backend.simulated, "logs": text}
