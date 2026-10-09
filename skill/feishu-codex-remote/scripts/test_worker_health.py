"""Real hidden-process worker recovery, with isolated state and no external calls."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import remote_gateway as gateway


def wait_for(predicate):
    # Only an observation budget for this synthetic child; never a task timeout.
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Worker fixture did not reach its expected state")
        time.sleep(0.01)


def fixture(case, root):
    root = Path(root)
    with ExitStack() as stack:
        stack.enter_context(patch.object(gateway, "REMOTE_STATE", root))
        stack.enter_context(patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
        stack.enter_context(patch.object(gateway.subprocess, "Popen", side_effect=AssertionError("model/process forbidden")))
        stack.enter_context(patch.object(gateway, "codex_cli_path", return_value=Path("fixture-only-codex")))
        stack.enter_context(patch.object(gateway, "fork_thread", side_effect=lambda *a: "branch-context"))
        stack.enter_context(patch.object(gateway, "start_thread", side_effect=AssertionError("unexpected bootstrap")))
        stack.enter_context(patch.object(gateway, "archive_thread", return_value=None))
        client = MagicMock()
        client.list_reactions.return_value = []
        client.add_reaction.return_value = {"data": {"reaction_id": "fixture-reaction"}}
        worker = gateway.ProjectWorker("fixture", {"working_directory": str(root), "chat_id": "fixture-chat"},
                                       client, root / "worker.log")
        store = worker.store
        store.set_thread_id("main-context")
        stack.enter_context(patch.object(store, "recover_interrupted",
                                        side_effect=AssertionError("live claims must never be reset")))
        original_next = store.next_pending_batch
        stack.enter_context(patch.object(store, "next_pending_batch",
                                        side_effect=lambda: original_next(quiet_window_seconds=0)))
        calls, deliveries = [], []
        entered = {lane: threading.Event() for lane in ("main", "branch")}
        releases = {lane: threading.Event() for lane in ("main", "branch")}
        hold = set()

        def run_model(key, project, runtime, item, attachments):
            mid = item["message_id"]
            lane = "branch" if item.get("branch_parent_thread_id") else "main"
            calls.append(mid)
            entered[lane].set()
            if mid == "unknown":
                raise SystemExit("injected exit after execution began")
            if lane in hold:
                releases[lane].wait()
            output = root / (mid + ".txt")
            output.write_text("Synthetic completed result", encoding="utf-8")
            return "Synthetic completed result", ("branch-context" if lane == "branch" else "main-context"), output

        stack.enter_context(patch.object(gateway, "run_codex", side_effect=run_model))
        stack.enter_context(patch.object(gateway, "run_codex_batch", side_effect=AssertionError("unexpected batching")))
        stack.enter_context(patch.object(gateway, "reply_complete",
                                        side_effect=lambda client, project, item, *args, **kwargs: deliveries.append(item["message_id"]) or ""))

        def enqueue(mid, branch=False):
            worker.enqueue({"message_id": mid, "chat_id": "fixture-chat", "create_time": "1000",
                            "message_type": "text", "content": json.dumps({"text": "# task" if branch else "task"}),
                            "sender": {"id": "fixture-user"}})

        def completed(mid):
            with store.lock:
                return store.state["messages"][mid]["status"] == "completed"

        def concurrent_ensure():
            results = []
            threads = [threading.Thread(target=lambda: results.append(worker.ensure_worker_threads())) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                assert not thread.is_alive(), "Concurrent ensure deadlocked"
            assert len(results) == len(threads)

        if case == "idle-death":
            original = store.next_pending_batch
            failed = threading.Event()

            def next_with_exit():
                if not failed.is_set():
                    failed.set()
                    raise SystemExit("injected idle thread exit")
                return original()

            stack.enter_context(patch.object(store, "next_pending_batch", side_effect=next_with_exit))
            worker.start()
            dead = worker.thread
            wait_for(lambda: failed.is_set() and not dead.is_alive())
            assert not worker.worker_health()["healthy"]
            assert "injected idle thread exit" in worker.worker_health()["errors"]["main"]["traceback"]
            concurrent_ensure()
            assert worker.thread is not dead and worker.store is store
            enqueue("once")
            wait_for(lambda: completed("once"))
            for _ in range(4):
                worker.ensure_worker_threads()
                worker.signal.set()
            wait_for(lambda: worker.worker_health()["healthy"])
            assert calls == ["once"] and deliveries == ["once"]

        elif case == "busy-preserved":
            hold.update(("main", "branch"))
            worker.start()
            enqueue("busy-main")
            assert entered["main"].wait(8)
            enqueue("busy-branch", branch=True)
            assert entered["branch"].wait(8)
            main = worker.thread
            branch = worker.active_branches["busy-branch"]
            dispatcher = worker.branch_thread
            original_dispatch = worker.dispatch_parallel
            injected = threading.Event()

            def dispatch_with_exit():
                if not injected.is_set():
                    injected.set()
                    raise SystemExit("injected dispatcher exit while branch busy")
                return original_dispatch()

            stack.enter_context(patch.object(worker, "dispatch_parallel", side_effect=dispatch_with_exit))
            worker.branch_signal.set()
            wait_for(lambda: injected.is_set() and not dispatcher.is_alive())
            concurrent_ensure()
            assert worker.branch_thread is not dispatcher
            assert worker.thread is main and worker.active_branches["busy-branch"] is branch
            assert store.thread_id() == "main-context" and worker.store is store
            assert calls.count("busy-main") == calls.count("busy-branch") == 1
            wait_for(lambda: worker.worker_health()["healthy"])
            releases["main"].set()
            releases["branch"].set()
            wait_for(lambda: completed("busy-main") and completed("busy-branch"))
            assert sorted(deliveries) == ["busy-branch", "busy-main"]

        elif case == "lifecycle-death":
            original_deliver = worker.deliver_lifecycle_notices
            injected = threading.Event()

            def deliver_with_exit():
                if not injected.is_set():
                    injected.set()
                    raise SystemExit("injected lifecycle exit")
                return original_deliver()

            stack.enter_context(patch.object(worker, "deliver_lifecycle_notices", side_effect=deliver_with_exit))
            worker.start()
            dead = worker.lifecycle_thread
            wait_for(lambda: injected.is_set() and not dead.is_alive())
            concurrent_ensure()
            live = worker.lifecycle_thread
            assert live is not dead
            for _ in range(8):
                assert worker.start_lifecycle() is live
            wait_for(lambda: worker.worker_health()["healthy"])
            assert not calls and not deliveries

        elif case in {"unknown-outcome", "unknown-branch"}:
            worker.start()
            original_thread = worker.thread
            branch = case == "unknown-branch"
            enqueue("unknown", branch=branch)
            if branch:
                wait_for(lambda: entered["branch"].is_set() and "unknown" not in worker.active_branches)
            else:
                wait_for(lambda: entered["main"].is_set() and not original_thread.is_alive())
            concurrent_ensure()
            for _ in range(5):
                worker.signal.set()
                worker.ensure_worker_threads()
                time.sleep(0.02)
            health = worker.worker_health()
            assert not health["healthy"] and health["outcome_unknown"] == ["unknown"]
            assert store.state["messages"]["unknown"]["status"] == "processing"
            assert store.state["messages"]["unknown"]["attempts"] == 1
            assert calls == ["unknown"] and not deliveries
            assert store.thread_id() == "main-context"

        elif case == "lifecycle-stopped":
            worker.start()
            wait_for(lambda: worker.worker_health()["healthy"])
            original_thread = worker.lifecycle_thread
            worker.lifecycle_stop.set()
            worker.lifecycle_signal.set()
            wait_for(lambda: not original_thread.is_alive())
            concurrent_ensure()
            assert worker.lifecycle_thread is original_thread
            assert worker.start_lifecycle() is original_thread
            assert not worker.worker_health()["healthy"]
            assert worker.worker_health()["threads"]["lifecycle"]["state"] == "stopped"

        elif case == "diagnostic-write-failure":
            bad = threading.Event()
            recovered = threading.Event()
            original_dispatch = worker.dispatch_parallel

            def dispatch_with_error():
                if not recovered.is_set():
                    bad.set()
                    raise PermissionError("injected iteration failure")
                return original_dispatch()

            stack.enter_context(patch.object(worker, "dispatch_parallel", side_effect=dispatch_with_error))
            log_patch = stack.enter_context(patch.object(gateway, "append_log", side_effect=OSError("injected log failure")))
            worker.start()
            wait_for(lambda: bad.is_set() and "diagnostic_write_error" in
                     worker.worker_health()["errors"].get("branch_dispatch", {}))
            first = worker.branch_thread
            assert first.is_alive() and not worker.worker_health()["healthy"]
            recovered.set()
            worker.branch_signal.set()
            wait_for(lambda: worker.worker_health()["healthy"])
            assert worker.branch_thread is first
        else:
            raise AssertionError("Unknown fixture case")

        # Daemon loops belong to this child only. It exits before the parent
        # removes its isolated directory; no production state is imported.
        return {"ok": True, "case": case, "calls": calls, "deliveries": deliveries}


class WorkerHealthProcessTests(unittest.TestCase):
    def child(self, case):
        with tempfile.TemporaryDirectory(prefix="worker-health-") as directory:
            result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                                     "--fixture", case, directory], capture_output=True, text=True,
                                    encoding="utf-8", timeout=30,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])

    def test_idle_main_thread_death_recovers_without_duplicate_work(self):
        self.child("idle-death")

    def test_busy_main_and_branch_survive_dispatcher_replacement(self):
        self.child("busy-preserved")

    def test_lifecycle_thread_recovers_and_start_is_idempotent(self):
        self.child("lifecycle-death")

    def test_unknown_execution_outcome_is_unhealthy_and_never_replayed(self):
        self.child("unknown-outcome")

    def test_unknown_branch_outcome_is_unhealthy_and_never_replayed(self):
        self.child("unknown-branch")

    def test_explicit_lifecycle_stop_is_not_undone_by_thread_recovery(self):
        self.child("lifecycle-stopped")

    def test_iteration_and_diagnostic_errors_do_not_silently_kill_dispatcher(self):
        self.child("diagnostic-write-failure")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture":
        result = fixture(sys.argv[2], sys.argv[3])
        print(json.dumps(result), flush=True)
        # Avoid daemon teardown racing the interpreter's output locks.
        os._exit(0)
    unittest.main()
