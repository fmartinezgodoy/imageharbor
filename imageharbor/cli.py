from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .mcp import MCPServer
from .access import AccessPolicy
from .model import Config, HarborError, integer, validate_spec
from .manifests import render
from .platform import Platform
from .tasks import FakeHarness, TaskStore, UnconfiguredHarness, work_once


def read_spec(filename):
    try:
        if Path(filename).stat().st_size > 64_000:
            raise HarborError("Spec file exceeds 64 KB")
        return json.loads(Path(filename).read_text())
    except (OSError, ValueError):
        raise HarborError("Cannot read application JSON") from None


def parser():
    root = argparse.ArgumentParser(description="Private image-first personal platform; default config chooses fake backend")
    root.add_argument("--config", default="examples/instance.fake.json")
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("deploy", "update", "render"):
        sub.add_parser(command).add_argument("spec_file")
    for command in ("status", "remove", "logs", "rollback"):
        cmd = sub.add_parser(command)
        cmd.add_argument("app_id")
        if command == "logs":
            cmd.add_argument("--tail", type=int, default=100)
        if command == "rollback":
            cmd.add_argument("--revision", type=int, required=True)
    cmd = sub.add_parser("volumes-delete")
    cmd.add_argument("app_id")
    cmd.add_argument("--volumes", nargs="+", required=True)
    cmd.add_argument("--confirm", required=True)
    sub.add_parser("inventory")
    cmd = sub.add_parser("reconcile")
    cmd.add_argument("--watch", action="store_true")
    cmd.add_argument("--interval", type=int, default=5)
    cmd = sub.add_parser("mcp")
    cmd.add_argument("--allow-app", action="append")
    cmd.add_argument("--access", choices=("read", "operate"))
    cmd.add_argument("--allow-data-delete", action="store_true")
    cmd = sub.add_parser("mcp-http", help="Opt-in private loopback HTTP; not an OAuth/ChatGPT integration")
    cmd.add_argument("--allow-app", action="append", required=True)
    cmd.add_argument("--access", choices=("read", "operate"), default="read")
    cmd.add_argument("--allow-data-delete", action="store_true")
    cmd.add_argument("--token-fd", type=int, required=True)
    cmd.add_argument("--token-ttl", type=int, default=900)
    cmd.add_argument("--port", type=int, default=8765)
    cmd = sub.add_parser("ssh-plan", help="Render a fixed remote stdio plan; never connect")
    cmd.add_argument("--alias", required=True)
    cmd.add_argument("--remote-root", required=True)
    cmd.add_argument("--allow-app", action="append", required=True)
    cmd.add_argument("--access", choices=("read", "operate"), default="read")
    cmd.add_argument("--allow-data-delete", action="store_true")
    cmd = sub.add_parser("submit")
    cmd.add_argument("app_id")
    cmd.add_argument("--kind", choices=("diagnose", "reconcile"), default="diagnose")
    cmd.add_argument("--idempotency-key", required=True)
    for command in ("task-status", "result", "cancel"):
        sub.add_parser(command).add_argument("task_id")
    cmd = sub.add_parser("worker")
    cmd.add_argument("--adapter", choices=("unconfigured", "fake"), default="unconfigured")
    cmd.add_argument("--once", action="store_true")
    return root


def emit(value):
    print(json.dumps(value, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "ssh-plan":
            from .ssh_plan import plan
            emit(plan(args.alias, args.remote_root, args.allow_app, args.access, args.allow_data_delete))
            return 0
        policy = None
        token = None
        if args.command in ("mcp", "mcp-http"):
            if args.allow_app:
                policy = AccessPolicy(frozenset(args.allow_app), args.access or "read", args.allow_data_delete)
            elif args.allow_data_delete or args.access is not None:
                raise HarborError("Restricted stdio requires explicit allowed applications")
            if args.command == "mcp-http":
                from .remote import token_from_fd
                integer(args.port, 0, 65535)
                integer(args.token_ttl, 60, 3600)
                token = token_from_fd(args.token_fd)
        cfg = Config.load(args.config)
        if args.command == "render":
            emit({"apiVersion": "v1", "kind": "List", "items": render(cfg, validate_spec(read_spec(args.spec_file), cfg), 1)})
            return 0
        platform = Platform(cfg)
        if args.command in ("deploy", "update"):
            result = getattr(platform, args.command)(read_spec(args.spec_file))
        elif args.command in ("status", "remove"):
            result = getattr(platform, args.command)(args.app_id)
        elif args.command == "rollback":
            result = platform.rollback(args.app_id, args.revision)
        elif args.command == "logs":
            result = platform.logs(args.app_id, args.tail)
        elif args.command == "volumes-delete":
            result = platform.volumes_delete(args.app_id, args.volumes, args.confirm)
        elif args.command == "inventory":
            result = platform.inventory()
        elif args.command == "reconcile":
            integer(args.interval, 1, 30)
            emit(platform.reconcile())
            if not args.watch:
                return 0
            while True:
                try:
                    emit(platform.watch_round(args.interval))
                except HarborError as error:
                    print(str(error), file=sys.stderr)
                    time.sleep(min(args.interval, 5))
        else:
            tasks = TaskStore(cfg.state_dir, scope_provider=platform.task_scope)
            if args.command == "mcp":
                MCPServer(platform, tasks, policy).serve()
                return 0
            if args.command == "mcp-http":
                from .remote import PrivateMCPHTTPServer
                with PrivateMCPHTTPServer(platform, tasks, policy, token, args.port, args.token_ttl) as server:
                    token = None
                    print(f"Private MCP loopback listener: 127.0.0.1:{server.server_port}; ephemeral auth; {args.access} access", file=sys.stderr)
                    server.serve_forever(poll_interval=0.5)
                return 0
            if args.command == "submit":
                platform.status(args.app_id)
                result = tasks.submit({"app_id": args.app_id, "kind": args.kind}, args.idempotency_key)
            elif args.command in ("task-status", "result", "cancel"):
                result = getattr(tasks, "status" if args.command == "task-status" else args.command)(args.task_id)
            else:
                harness = FakeHarness() if args.adapter == "fake" else UnconfiguredHarness()
                while True:
                    result = work_once(tasks, platform, harness)
                    if args.once:
                        break
                    if result["worked"]:
                        emit(result)
                    else:
                        time.sleep(1)
        emit(result)
        return 0
    except HarborError as error:
        print(f"imageharbor: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
