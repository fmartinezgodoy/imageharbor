"""Bounded MCP dispatch shared by stdio and the optional private HTTP adapter."""
from __future__ import annotations

import json
import re
import sys

from .model import HarborError, obj

PROTOCOLS = ("2025-11-25", "2025-06-18", "2024-11-05")


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def validate(value, definition):
    """Validate the small JSON-schema vocabulary used by this server, locally."""
    expected = definition.get("type")
    kinds = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "integer": lambda v: type(v) is int,
             "boolean": lambda v: type(v) is bool, "null": lambda v: v is None}
    if expected and not any(kinds[t](value) for t in (expected if isinstance(expected, list) else [expected])):
        raise HarborError("Input does not match the operation schema")
    if "enum" in definition and value not in definition["enum"]:
        raise HarborError("Input does not match the operation schema")
    if isinstance(value, dict):
        properties = definition.get("properties", {})
        if set(definition.get("required", [])) - set(value):
            raise HarborError("Required operation parameter is missing")
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key])
            elif definition.get("additionalProperties") is False:
                raise HarborError("Unexpected operation parameter")
            elif isinstance(definition.get("additionalProperties"), dict):
                validate(item, definition["additionalProperties"])
    if isinstance(value, list):
        if len(value) < definition.get("minItems", 0) or len(value) > definition.get("maxItems", 256):
            raise HarborError("Operation list length is outside its bounds")
        for item in value:
            validate(item, definition.get("items", {}))
    if isinstance(value, str):
        if len(value) > definition.get("maxLength", 4096) or (definition.get("pattern") and not re.fullmatch(definition["pattern"], value)):
            raise HarborError("Invalid operation text parameter")
    if type(value) is int and not definition.get("minimum", -2**63) <= value <= definition.get("maximum", 2**63 - 1):
        raise HarborError("Operation integer is outside its bounds")


TEXT = {"type": "string"}
APP = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"}
SPEC = schema({
    "id": APP, "purpose": {"type": "string", "maxLength": 500}, "image": TEXT,
    "port": {"type": "integer", "minimum": 1024, "maximum": 65535}, "repo": {"type": ["string", "null"]},
    "env": {"type": "object", "additionalProperties": {"type": "string"}},
    "secrets": {"type": "array", "maxItems": 64, "items": schema({"env": TEXT, "name": TEXT, "key": TEXT}, ("env", "name", "key"))},
    "volumes": {"type": "array", "maxItems": 8, "items": schema({"name": TEXT, "mount_path": TEXT, "size_gi": {"type": "integer", "minimum": 1, "maximum": 20}}, ("name", "mount_path", "size_gi"))},
    "routes": {"type": "array", "maxItems": 16, "items": schema({"host": TEXT, "subdomain": TEXT, "path": TEXT, "prefix": {"enum": ["strip", "preserve"]}}, ("path", "prefix"))},
    "path_support": {"type": "boolean"}, "canonical_url": {"type": ["string", "null"]},
    "tls_secret": {"type": ["string", "null"]},
    "resources": schema({"cpu_m": {"type": "integer"}, "memory_mi": {"type": "integer"}}),
    "uid": {"type": "integer"}, "egress_cidrs": {"type": "array", "items": TEXT},
    "image_pull_secrets": {"type": "array", "items": TEXT},
}, ("id", "purpose", "image", "port"))


class MCPServer:
    def __init__(self, platform, tasks, policy=None):
        if tasks.path.parent.resolve() != platform.cfg.state_dir.resolve():
            raise HarborError("MCP task store belongs to another platform state directory")
        tasks.scope_provider = platform.task_scope
        self.platform, self.tasks = platform, tasks
        self.policy = policy
        self.initialized = False
        self.ready = False
        self.operations = {
            "inventory": (schema({}), lambda: platform.inventory(), "List desired metadata and observed cluster state", True),
            "status": (schema({"app_id": APP}, ("app_id",)), platform.status, "Inspect one application", True),
            "deploy": (schema({"spec": SPEC}, ("spec",)), platform.deploy, "Deploy a prebuilt Docker Hub image", False),
            "update": (schema({"spec": SPEC}, ("spec",)), platform.update, "Update a complete application specification", False),
            "rollback": (schema({"app_id": APP, "revision": {"type": "integer", "minimum": 1}}, ("app_id", "revision")), platform.rollback, "Restore a retained release as a new revision", False),
            "logs": (schema({"app_id": APP, "tail": {"type": "integer", "minimum": 1, "maximum": 500}}, ("app_id",)), platform.logs, "Bounded logs, only for operator-approved scrubbed applications", True),
            "remove": (schema({"app_id": APP}, ("app_id",)), platform.remove, "Remove runtime; retain volumes and identity", False),
            "volumes_delete": (schema({"app_id": APP, "volumes": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 8}, "confirm": TEXT}, ("app_id", "volumes", "confirm")), platform.volumes_delete, "Request owned PVC deletion after remove, opt-in and exact confirmation", False),
            "submit": (schema({"request": schema({"app_id": APP, "kind": {"enum": ["diagnose", "reconcile"]}}, ("app_id", "kind")), "idempotency_key": TEXT}, ("request", "idempotency_key")), self.submit, "Queue one scoped agent task; a worker and configured harness are required", False),
            "task_status": (schema({"task_id": TEXT}, ("task_id",)), tasks.status, "Read persisted task state", True),
            "result": (schema({"task_id": TEXT}, ("task_id",)), tasks.result, "Read terminal task result or explicit failure", True),
            "cancel": (schema({"task_id": TEXT}, ("task_id",)), tasks.cancel, "Request cooperative cancellation; already applied changes are not undone", False),
        }

    def submit(self, request, idempotency_key):
        obj(request, ("app_id", "kind"), ("app_id", "kind"))
        self.platform.status(request["app_id"])
        return self.tasks.submit(request, idempotency_key)

    @staticmethod
    def error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self.error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
        request_id = message.get("id")
        if "id" in message and not ((isinstance(request_id, str) and len(request_id) <= 100) or type(request_id) is int):
            return self.error(None, -32600, "Invalid request identifier")
        method = message["method"]
        if "id" not in message:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        try:
            params = message.get("params", {})
            if method == "initialize":
                if self.initialized:
                    raise HarborError("MCP session is already initialized")
                obj(params, ("protocolVersion", "capabilities", "clientInfo", "_meta"), ("protocolVersion", "capabilities", "clientInfo"))
                validate(params["protocolVersion"], {"type": "string", "maxLength": 32})
                validate(params["capabilities"], {"type": "object"})
                validate(params["clientInfo"], schema({"name": TEXT, "version": TEXT, "title": TEXT, "description": TEXT, "websiteUrl": TEXT, "icons": {"type": "array"}}, ("name", "version")))
                self.initialized = True
                result = {"protocolVersion": params["protocolVersion"] if params["protocolVersion"] in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "imageharbor", "version": "0.1.0"},
                          "instructions": "Private bounded control plane. Fake results are marked. Oracle agent harness is not integrated."}
            elif method == "ping":
                result = {}
            elif not self.ready:
                raise HarborError("Complete MCP initialization first")
            elif method == "tools/list":
                result = {"tools": [{"name": tool, "description": info[2], "inputSchema": info[0],
                                     "annotations": {"readOnlyHint": info[3], "destructiveHint": tool in ("remove", "volumes_delete", "update", "rollback"), "openWorldHint": not info[3]}}
                                    for tool, info in self.operations.items() if self.policy is None or tool in self.policy.tools]}
            elif method == "tools/call":
                obj(params, ("name", "arguments", "_meta"), ("name",))
                if params["name"] not in self.operations:
                    raise HarborError("Unknown bounded operation")
                input_schema, operation, _, _ = self.operations[params["name"]]
                args = params.get("arguments", {})
                validate(args, input_schema)
                try:
                    if self.policy is not None:
                        self.policy.authorize(params["name"], args, self.tasks)
                    output = self.policy.inventory(self.platform) if self.policy is not None and params["name"] == "inventory" else operation(**args)
                    result = {"content": [{"type": "text", "text": json.dumps(output)}], "structuredContent": output, "isError": False}
                except HarborError as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                return self.error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (HarborError, TypeError, KeyError):
            return self.error(request_id, -32602, "Invalid parameters or initialization state")

    def serve(self, source=None, destination=None):
        source, destination = source or sys.stdin, destination or sys.stdout
        while True:
            try:
                line = source.readline(262_145)
                encoded_size = len(line.encode())
            except UnicodeError:
                destination.write(json.dumps(self.error(None, -32700, "Invalid UTF-8 message")) + "\n")
                destination.flush()
                return
            if not line:
                return
            if encoded_size > 262_144 or not line.endswith("\n"):
                # Do not accept a partial oversized message as a subsequent request.
                return
            try:
                response = self.handle(json.loads(line))
            except (ValueError, RecursionError):
                response = self.error(None, -32700, "Parse error")
            except Exception:
                response = self.error(None, -32603, "Internal operation failure")
            if response is not None:
                destination.write(json.dumps(response, separators=(",", ":")) + "\n")
                destination.flush()
