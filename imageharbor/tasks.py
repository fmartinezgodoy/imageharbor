"""Durable scoped jobs. The real agent harness is deliberately NOT integrated."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Protocol

from .backend import locked
from .model import HarborError, name, obj, string

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
LEASE_SECONDS = 90


class TaskStore:
    def __init__(self, state_dir, scope_provider=None):
        self.scope_provider = scope_provider
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = state_dir / "tasks.sqlite"
        with locked(state_dir / "tasks-init.lock"):
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            with self.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                    request TEXT NOT NULL, scope TEXT, state TEXT NOT NULL,
                    result TEXT, error TEXT, worker TEXT, heartbeat REAL,
                    created REAL NOT NULL, updated REAL NOT NULL)""")
                if "scope" not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}:
                    db.execute("ALTER TABLE tasks ADD COLUMN scope TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, request, idempotency_key):
        obj(request, ("app_id", "kind"), ("app_id", "kind"))
        name(request["app_id"])
        if request["kind"] not in ("diagnose", "reconcile"):
            raise HarborError("Agent tasks support only diagnose and reconcile for one application")
        string(idempotency_key, 100)
        if not idempotency_key:
            raise HarborError("Idempotency key is required")
        if self.scope_provider is None:
            raise HarborError("Task submission requires a verified application scope provider")
        scope = json.dumps(self.scope_provider(request["app_id"]), sort_keys=True)
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["request"] != encoded or existing["scope"] != scope:
                    raise HarborError("Idempotency key already used for different task parameters")
                return self._public(existing)
            if db.execute("SELECT count(*) FROM tasks").fetchone()[0] >= 1000:
                raise HarborError("Task storage cap reached; archive task state before accepting more jobs")
            if db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','running','cancel_requested')").fetchone()[0] >= 50:
                raise HarborError("Task queue is full")
            now = time.time()
            task_id = str(uuid.uuid4())
            db.execute("INSERT INTO tasks(id,idempotency_key,request,scope,state,created,updated) VALUES(?,?,?,?,'queued',?,?)",
                       (task_id, idempotency_key, encoded, scope, now, now))
            return self._public(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def _public(row, include_result=False):
        result = {"task_id": row["id"], "request": json.loads(row["request"]), "state": row["state"],
                  "created_at": row["created"], "updated_at": row["updated"]}
        if include_result and row["state"] in TERMINAL:
            result["result"] = json.loads(row["result"]) if row["result"] else None
            result["error"] = row["error"]
        return result

    def _row(self, db, task_id):
        try:
            uuid.UUID(task_id)
        except (ValueError, TypeError, AttributeError):
            raise HarborError("Invalid task identifier") from None
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HarborError("Unknown task")
        return row

    def status(self, task_id):
        with self.connect() as db:
            return self._public(self._row(db, task_id))

    def result(self, task_id):
        with self.connect() as db:
            return self._public(self._row(db, task_id), True)

    def cancel(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            if row["state"] == "queued":
                db.execute("UPDATE tasks SET state='cancelled',updated=? WHERE id=?", (time.time(), task_id))
            elif row["state"] == "running":
                db.execute("UPDATE tasks SET state='cancel_requested',updated=? WHERE id=?", (time.time(), task_id))
            return self._public(self._row(db, task_id))

    def recover_stale(self):
        with self.connect() as db:
            changed = db.execute("""UPDATE tasks SET state='interrupted',error='worker_lease_expired',updated=?
                WHERE state IN ('running','cancel_requested') AND (heartbeat IS NULL OR heartbeat < ?)""", (time.time(), time.time() - LEASE_SECONDS)).rowcount
            return changed

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            worker = str(uuid.uuid4())
            now = time.time()
            db.execute("UPDATE tasks SET state='running',worker=?,heartbeat=?,updated=? WHERE id=?", (worker, now, now, row["id"]))
            return {"task_id": row["id"], "request": json.loads(row["request"]), "scope": json.loads(row["scope"]) if row["scope"] else None, "worker": worker}

    def heartbeat(self, task):
        with self.connect() as db:
            now = time.time()
            return db.execute("UPDATE tasks SET heartbeat=? WHERE id=? AND worker=? AND state IN ('running','cancel_requested') AND heartbeat >= ?",
                              (now, task["task_id"], task["worker"], now - LEASE_SECONDS)).rowcount == 1

    def cancelled(self, task):
        with self.connect() as db:
            row = self._row(db, task["task_id"])
            return row["worker"] != task["worker"] or row["state"] != "running" or row["heartbeat"] is None or row["heartbeat"] < time.time() - LEASE_SECONDS

    def finish(self, task, result=None, error=None):
        encoded = json.dumps(result, allow_nan=False) if result is not None else None
        if encoded and len(encoded.encode()) > 32_768:
            encoded, error = None, "result_too_large"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task["task_id"])
            if row["worker"] != task["worker"] or row["state"] not in ("running", "cancel_requested"):
                return
            if row["heartbeat"] is None or row["heartbeat"] < time.time() - LEASE_SECONDS:
                db.execute("UPDATE tasks SET state='interrupted',result=NULL,error='worker_lease_expired',updated=? WHERE id=?", (time.time(), task["task_id"]))
                return
            cancelled = row["state"] == "cancel_requested"
            state = "cancelled" if cancelled else ("failed" if error else "succeeded")
            db.execute("UPDATE tasks SET state=?,result=?,error=?,updated=? WHERE id=?",
                       (state, None if cancelled else encoded, None if cancelled else error, time.time(), task["task_id"]))


class Capabilities:
    """Only these methods are passed to a future harness; no backend, shell or root.

    This is an interface boundary, not a Python sandbox. A real untrusted harness
    must run in another process with authenticated capability RPC and OS isolation.
    """
    def __init__(self, platform, app_id, cancelled, kind, scope):
        self.__platform = platform
        self.__app_id = name(app_id)
        self.__kind = kind
        self.__scope = dict(scope)
        self.__cancelled = cancelled

    @property
    def app_id(self):
        return self.__app_id

    def _check(self):
        if self.__cancelled():
            raise HarborError("Task cancellation requested")
        self.__platform.check_task_scope(self.__app_id, self.__scope)

    def status(self):
        self._check()
        return self.__platform.status(self.app_id)

    def reconcile(self):
        if self.__kind != "reconcile":
            raise HarborError("Diagnose tasks have read-only capabilities")
        self._check()
        with locked(self.__platform.writer_lock):
            self._check()
            record = self.__platform._record(self.app_id)
            if not record:
                raise HarborError("Unknown application")
            self.__platform._reconcile_one(self.app_id, record, check=self._check)
            self._check()
        return self.status()


class Harness(Protocol):
    def execute(self, request: dict, capabilities: Capabilities, cancelled) -> dict: ...


class HarnessUnavailable(HarborError):
    pass


class UnconfiguredHarness:
    def execute(self, request, capabilities, cancelled):
        raise HarnessUnavailable("harness_not_configured")


class FakeHarness:
    """Deterministic simulator for tests. No LLM, no Oracle agent connection."""
    def execute(self, request, capabilities, cancelled):
        status = capabilities.reconcile() if request["kind"] == "reconcile" else capabilities.status()
        return {"adapter": "fake", "simulated": True, "app_id": capabilities.app_id,
                "state": status["state"], "ready": status["ready"], "drift": status["drift"],
                "summary": "Deterministic local simulation; no agent harness was called"}


def work_once(store: TaskStore, platform, harness: Harness):
    if store.path.parent.resolve() != platform.cfg.state_dir.resolve():
        raise HarborError("Worker task store does not belong to this platform's state directory")
    if isinstance(harness, FakeHarness) and not platform.backend.simulated:
        raise HarborError("Fake harness is forbidden against a real cluster")
    store.recover_stale()
    task = store.claim()
    if not task:
        return {"worked": False}
    stop = threading.Event()
    lease_lost = threading.Event()

    def beat():
        while not stop.wait(5):
            try:
                if not store.heartbeat(task):
                    lease_lost.set()
                    return
            except sqlite3.Error:
                lease_lost.set()
                return

    heartbeat = threading.Thread(target=beat, daemon=True)
    heartbeat.start()
    try:
        if not task["scope"]:
            raise HarborError("Legacy task has no verified scope")
        platform.check_task_scope(task["request"]["app_id"], task["scope"])
        cancelled = lambda: lease_lost.is_set() or store.cancelled(task)
        result = harness.execute(dict(task["request"]), Capabilities(platform, task["request"]["app_id"], cancelled, task["request"]["kind"], task["scope"]), cancelled)
        if lease_lost.is_set():
            store.finish(task, error="worker_lease_lost")
        else:
            store.finish(task, result=result)
    except HarnessUnavailable:
        store.finish(task, error="harness_not_configured")
    except HarborError:
        store.finish(task, error="scoped_operation_failed")
    except Exception:
        store.finish(task, error="harness_failed")
    finally:
        stop.set()
        heartbeat.join(timeout=1)
    return {"worked": True, **store.result(task["task_id"])}
