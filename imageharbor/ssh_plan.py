"""Render a fixed SSH stdio command for later review. Never execute or connect."""
import re
import shlex
from pathlib import PurePosixPath

from .access import AccessPolicy
from .model import HarborError


def plan(alias, remote_root, apps, mode="read", allow_data_delete=False):
    if not isinstance(alias, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", alias):
        raise HarborError("SSH plan requires a literal conventional host alias")
    if not isinstance(remote_root, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,199}", remote_root):
        raise HarborError("Remote project root must be a safe absolute POSIX path")
    root = PurePosixPath(remote_root)
    if ".." in root.parts or str(root) == "/":
        raise HarborError("Remote project root cannot traverse directories")
    policy = AccessPolicy(frozenset(apps), mode, allow_data_delete)
    command = ["python3", "-m", "imageharbor", "--config", "personal/instance.json", "mcp", "--access", mode]
    for app in sorted(policy.apps):
        command += ["--allow-app", app]
    if allow_data_delete:
        command += ["--allow-data-delete"]
    fixed = f"cd {shlex.quote(str(root))} && exec {shlex.join(command)}"
    argv = ["ssh", "-T", "-a"]
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
                   "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes",
                   "PermitLocalCommand=no", "RemoteCommand=none", "RequestTTY=no",
                   "ControlMaster=no", "ControlPath=none", "ProxyCommand=none", "ProxyJump=none",
                   "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                   "ConnectTimeout=10", "ConnectionAttempts=1", "ServerAliveInterval=15", "ServerAliveCountMax=2"):
        argv += ["-o", option]
    argv += ["--", alias, fixed]
    return {"executed": False, "transport": "ssh-stdio", "argv": argv,
            "access": mode, "apps": sorted(policy.apps), "allow_data_delete": allow_data_delete,
            "requires": ["Explicit authorization to connect and start this fixed remote process",
                         "Reviewed SSH configuration, trusted host key and existing authentication",
                         "Isolated remote checkout and personal config under a restricted operator account"],
            "chatgpt_connected": False}
