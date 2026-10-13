"""Explicit capability policy for private remote MCP clients, not multi-tenancy."""
from dataclasses import dataclass

from .model import HarborError, name

READ_TOOLS = frozenset({"inventory", "status", "task_status", "result"})
OPERATE_TOOLS = READ_TOOLS | {"deploy", "update", "rollback", "logs", "remove", "submit", "cancel"}


@dataclass(frozen=True)
class AccessPolicy:
    apps: frozenset[str]
    mode: str = "read"
    allow_data_delete: bool = False

    def __post_init__(self):
        apps = frozenset(self.apps)
        if not apps or len(apps) > 32:
            raise HarborError("Remote access requires 1–32 explicit application identifiers")
        for app in apps:
            name(app)
        if self.mode not in ("read", "operate") or type(self.allow_data_delete) is not bool:
            raise HarborError("Invalid remote access policy")
        if self.allow_data_delete and self.mode != "operate":
            raise HarborError("Data deletion requires operate access")
        object.__setattr__(self, "apps", apps)

    @property
    def tools(self):
        allowed = READ_TOOLS if self.mode == "read" else OPERATE_TOOLS
        return allowed | {"volumes_delete"} if self.allow_data_delete else allowed

    def authorize(self, tool, args, tasks):
        if tool not in self.tools:
            raise HarborError("Operation is outside the authorized scope")
        if tool == "inventory":
            return
        if tool in ("task_status", "result", "cancel"):
            try:
                app = tasks.status(args["task_id"])["request"]["app_id"]
            except HarborError:
                raise HarborError("Task is outside the authorized scope") from None
        elif tool in ("deploy", "update"):
            app = args["spec"]["id"]
        elif tool == "submit":
            app = args["request"]["app_id"]
        else:
            app = args["app_id"]
        if app not in self.apps:
            raise HarborError("Application is outside the authorized scope")

    def inventory(self, platform):
        applications, errors = [], []
        # Do not return metadata/errors/orphan names from applications outside scope.
        for app in sorted(self.apps):
            try:
                applications.append(platform.status(app))
            except HarborError:
                errors.append({"id": app, "error": "Application state unavailable"})
        return {"applications": applications, "metadata_errors": errors,
                "unregistered_owners": [], "simulated": platform.backend.simulated}
