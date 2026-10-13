"""Run a complete disposable fake flow: python3 -m examples.demo."""
import json
import tempfile
from pathlib import Path

from imageharbor.model import Config
from imageharbor.platform import Platform
from imageharbor.tasks import FakeHarness, TaskStore, work_once


def main():
    examples = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="imageharbor-demo-") as temp:
        cfg = Config(state_dir=Path(temp), require_digest=False, log_safe_apps=["hello"])
        platform = Platform(cfg)
        first = json.loads((examples / "hello.json").read_text())
        second = json.loads((examples / "hello-v2.json").read_text())
        deployed = platform.deploy(first)
        volume_uid = deployed["volumes"][0]["uid"]
        updated = platform.update(second)
        rolled = platform.rollback("hello", 1)
        tasks = TaskStore(cfg.state_dir, scope_provider=platform.task_scope)
        submitted = tasks.submit({"app_id": "hello", "kind": "diagnose"}, "demo-diagnose")
        task = work_once(tasks, platform, FakeHarness())
        cancelled = tasks.submit({"app_id": "hello", "kind": "reconcile"}, "demo-cancel")
        tasks.cancel(cancelled["task_id"])
        logs = platform.logs("hello")
        removed = platform.remove("hello")
        retained = removed["volumes"][0]["uid"] == volume_uid
        restarted = Platform(cfg).status("hello")
        assert retained and restarted["state"] == "removed"
        assert task["state"] == "succeeded"
        assert tasks.status(cancelled["task_id"])["state"] == "cancelled"
        print(json.dumps({
            "simulated": True, "oracle_contacted": False,
            "release_revisions": [deployed["revision"], updated["revision"], rolled["revision"]],
            "rollback_image": rolled["image"], "async_state": task["state"],
            "async_adapter": task["result"]["adapter"], "cancel_state": "cancelled",
            "logs": logs["logs"].strip(), "removed_state": removed["state"],
            "data_retained_same_pvc_uid": retained, "restart_state": restarted["state"],
            "cleanup": "Only this demo's temporary fake state is removed; no real volumes or images are touched"
        }, indent=2))


if __name__ == "__main__":
    main()
