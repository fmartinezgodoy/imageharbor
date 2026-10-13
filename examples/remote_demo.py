"""Temporary loopback MCP + real CLI subprocesses, fake platform, no SSH/Oracle.

Run explicitly: python3 -m examples.remote_demo. No token or personal config is
saved. The temporary listener is terminated even when a check fails.
"""
import http.client
import json
import os
import re
import secrets
import select
import subprocess
import sys
import tempfile
from pathlib import Path


def run():
    root = Path(__file__).resolve().parents[1]
    token = secrets.token_urlsafe(32).encode()
    with tempfile.TemporaryDirectory(prefix="imageharbor-http-demo-") as temp:
        config = Path(temp) / "instance.json"
        config.write_text(json.dumps({"backend": "fake", "state_dir": "state", "require_digest": False}))
        read_fd, write_fd = os.pipe()
        child = None
        try:
            child = subprocess.Popen([sys.executable, "-m", "imageharbor", "--config", str(config), "mcp-http",
                                      "--allow-app", "hello", "--access", "operate", "--port", "0", "--token-fd", str(read_fd)],
                                     cwd=root, pass_fds=(read_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.close(read_fd)
            read_fd = None
            os.write(write_fd, token)
            os.close(write_fd)
            write_fd = None
            if not select.select([child.stderr], [], [], 8)[0]:
                raise RuntimeError("Temporary MCP listener startup timed out")
            line = child.stderr.readline()
            match = re.fullmatch(r"Private MCP loopback listener: 127\.0\.0\.1:(\d+); ephemeral auth; operate access\n", line)
            if not match:
                raise RuntimeError("Temporary loopback listener unavailable; no remote connection attempted")
            port = int(match[1])
            sid = None
            request_id = 0

            def request(method, params=None, notification=False):
                nonlocal sid, request_id
                request_id += 1
                message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
                if not notification:
                    message["id"] = request_id
                headers = {"Authorization": "Bearer " + token.decode(), "Content-Type": "application/json",
                           "Accept": "application/json, text/event-stream"}
                if sid:
                    headers.update({"MCP-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25"})
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    connection.request("POST", "/mcp", json.dumps(message).encode(), headers)
                    response = connection.getresponse()
                    body = response.read()
                    if response.status not in (200, 202):
                        raise RuntimeError("Private MCP request failed")
                    if method == "initialize":
                        sid = response.getheader("MCP-Session-Id")
                    output = json.loads(body) if body else None
                    if output and "error" in output:
                        raise RuntimeError("Private MCP protocol rejected request")
                    return output["result"] if output else None
                finally:
                    connection.close()

            def tool(name, arguments=None):
                result = request("tools/call", {"name": name, "arguments": arguments or {}})
                if result["isError"]:
                    raise RuntimeError("Private MCP bounded operation failed")
                return result["structuredContent"]

            request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "imageharbor-demo", "version": "1"}})
            request("notifications/initialized", notification=True)
            catalog = request("tools/list")["tools"]
            spec = json.loads((root / "examples/hello.json").read_text())
            initial = tool("deploy", {"spec": spec})
            pvc = initial["volumes"][0]["uid"]
            update = json.loads((root / "examples/hello-v2.json").read_text())
            updated = tool("update", {"spec": update})
            rollback = tool("rollback", {"app_id": "hello", "revision": 1})
            task = tool("submit", {"request": {"app_id": "hello", "kind": "diagnose"}, "idempotency_key": "private-demo"})
            completed = subprocess.run([sys.executable, "-m", "imageharbor", "--config", str(config), "worker", "--adapter", "fake", "--once"],
                                       cwd=root, capture_output=True, text=True, timeout=10)
            if completed.returncode:
                raise RuntimeError("Fake worker demo failed")
            result = tool("result", {"task_id": task["task_id"]})
            removed = tool("remove", {"app_id": "hello"})
            return {"transport": "loopback-http", "http_cli_subprocess": True, "authenticated": True,
                    "access": "operate", "data_delete_advertised": any(t["name"] == "volumes_delete" for t in catalog),
                    "simulated_platform": initial["backend"] == "fake", "oracle_contacted": False,
                    "chatgpt_connected": False, "release_revisions": [initial["revision"], updated["revision"], rollback["revision"]],
                    "async_state": result["state"], "removed_state": removed["state"],
                    "data_retained_same_pvc_uid": removed["volumes"][0]["uid"] == pvc,
                    "cleanup": "Temporary listener terminated and temporary fake state removed"}
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
            if child is not None:
                child.terminate()
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=2)


if __name__ == "__main__":
    try:
        print(json.dumps(run(), indent=2))
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        print("Private demo failed; check local loopback/Python availability. No Oracle connection was made.", file=sys.stderr)
        sys.exit(2)
