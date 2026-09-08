"""Synthetic lifecycle regression; never uses a real app, Codex, or user history."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gateway_lifecycle as lifecycle
import remote_gateway as gateway


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        guard = patch.object(gateway, "REMOTE_STATE", self.root)
        guard.start()
        self.addCleanup(guard.stop)
        self.client = MagicMock()
        self.client.reply_post.return_value = {"data": {"message_id": "reply-id"}}
        self.client.send_post.return_value = {"data": {"message_id": "notice-id"}}
        self.project = {"working_directory": str(self.root), "chat_id": "test-chat"}
        self.worker = gateway.ProjectWorker("test", self.project, self.client, self.root / "log")
        self.store = self.worker.store
        self.store.set_thread_id("main")
        self.addCleanup(self.worker.lifecycle_stop.set)
        for name in ("archive_thread", "start_thread", "fork_thread", "run_codex"):
            guard = patch.object(gateway, name, side_effect=AssertionError("Unexpected external " + name))
            guard.start()
            self.addCleanup(guard.stop)

    def enqueue(self, mid="one", text="hello"):
        row = {"message_id": mid, "chat_id": "test-chat", "create_time": "1000",
               "message_type": "text", "content": json.dumps({"text": text}),
               "sender": {"id": "user"}}
        self.worker.enqueue(row)
        return row

    def enter(self, request="upgrade"):
        return self.worker.maintenance_action("enter", request, reason="test repair")

    def child(self, status="completed", attempts=1, child="child"):
        self.enqueue(text="# branch")
        self.store.finish("one", status, attempts=attempts, branch_thread_id=child,
                          branch_parent_thread_id="main")

    def test_maintenance_preserves_payload_without_completing_work(self):
        original = self.enqueue()
        self.enter()
        row = self.store.state["messages"]["one"]
        self.assertEqual("deferred", row["status"])
        self.assertEqual(original["content"], row["content"])
        self.assertEqual(0, row["attempts"])
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.worker.deliver_lifecycle_notices()
        self.client.reply_post.assert_called_once()
        self.assertEqual("deferred", row["status"])
        self.assertNotIn("completed_at", row)
        self.assertFalse(self.client.add_reaction.called)

    def test_arrivals_and_duplicates_are_retained_during_upgrade(self):
        self.enter()
        original = self.enqueue(text="# includes attachment reference")
        self.worker.enqueue(original)
        self.assertEqual(1, len(self.store.state["messages"]))
        self.assertEqual([], self.worker.dispatch_parallel())
        self.assertEqual("deferred", self.store.state["messages"]["one"]["status"])
        self.worker.deliver_lifecycle_notices()
        self.worker.deliver_lifecycle_notices()
        self.client.reply_post.assert_called_once()

    def test_repeated_enter_preserves_identity_and_notification(self):
        self.enqueue()
        first = self.enter()
        self.enter("another-command")
        self.assertEqual(first, self.store.state["maintenance"])
        self.assertEqual(1, len(self.store.state["lifecycle_notices"]))

    def test_restart_does_not_replay_deferred_messages(self):
        self.enqueue()
        self.enter()
        restarted = gateway.ProjectWorker("test", self.project, self.client, self.root / "log")
        self.assertTrue(restarted.store.pause_requested())
        self.assertEqual("deferred", restarted.store.state["messages"]["one"]["status"])
        restarted.maintenance_action("exit", "exit")
        self.assertEqual([], restarted.store.next_pending_batch(quiet_window_seconds=0))
        self.assertFalse(restarted.store.pause_requested())

    def test_exit_resumes_new_messages_only(self):
        self.enqueue()
        self.enter()
        self.worker.maintenance_action("exit", "exit")
        self.enqueue("new")
        selected = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual(["new"], [row["message_id"] for row in selected])
        self.assertEqual("deferred", self.store.state["messages"]["one"]["status"])

    def test_active_work_must_confirm_stopping_before_exit(self):
        self.enqueue()
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.enter()
        with self.assertRaises(gateway.GatewayError):
            self.store.exit_maintenance()
        with patch.object(gateway, "run_codex") as run:
            self.worker._process_batch(batch)
        run.assert_not_called()
        self.assertEqual("deferred", self.store.state["messages"]["one"]["status"])
        self.store.exit_maintenance()

    def test_stop_failure_is_not_falsely_marked_deferred(self):
        self.enqueue()
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        def run(*args):
            self.enter()
            raise lifecycle.MaintenanceStopFailed("still alive")
        with patch.object(gateway, "run_codex", side_effect=run):
            self.worker._process_batch(batch)
        self.assertEqual("processing", self.store.state["messages"]["one"]["status"])
        self.assertIn("maintenance_stop_error", self.store.state["messages"]["one"])
        with self.assertRaises(gateway.GatewayError):
            self.store.exit_maintenance()
        self.client.reply_post.assert_not_called()

    def test_completion_requires_recovery_and_stable_payload(self):
        self.enter()
        with self.assertRaises(gateway.GatewayError):
            self.worker.maintenance_action("notify-complete", "req", release_id="release")
        self.store.exit_maintenance()
        self.worker.maintenance_action("notify-complete", "req", release_id="release")
        self.worker.deliver_lifecycle_notices()
        self.worker.maintenance_action("notify-complete", "again", release_id="release")
        self.worker.deliver_lifecycle_notices()
        self.client.send_post.assert_called_once()
        with self.assertRaises(gateway.GatewayError):
            self.worker.maintenance_action("notify-complete", "changed", release_id="release", text="changed")
        self.worker.maintenance_action("notify-complete", "next", release_id="next")
        self.worker.deliver_lifecycle_notices()
        self.assertEqual(2, self.client.send_post.call_count)

    def test_failed_send_recovers_using_same_uuid_after_restart(self):
        self.worker.maintenance_action("notify-complete", "req", release_id="release")
        self.client.send_post.side_effect = RuntimeError("offline")
        self.worker.deliver_lifecycle_notices()
        old_uuid = self.client.send_post.call_args.args[2]
        restarted = gateway.ProjectWorker("test", self.project, self.client, self.root / "log")
        self.client.send_post.side_effect = None
        with patch.object(lifecycle.time, "time", return_value=time.time() + 60):
            restarted.deliver_lifecycle_notices()
        self.assertEqual(old_uuid, self.client.send_post.call_args.args[2])
        self.assertEqual("delivered", restarted.store.state["lifecycle_notices"]["complete:release"]["status"])

    def test_new_maintenance_blocks_pending_completion_notification(self):
        self.worker.maintenance_action("notify-complete", "req", release_id="release")
        self.enter()
        self.worker.deliver_lifecycle_notices()
        self.client.send_post.assert_not_called()

    def test_notice_delivery_does_not_hold_message_store_lock(self):
        self.worker.maintenance_action("notify-complete", "req", release_id="release")
        def send(*args):
            done = threading.Event()
            writer = threading.Thread(target=lambda: (self.enqueue("concurrent"), done.set()))
            writer.start()
            self.assertTrue(done.wait(1))
            writer.join()
            return {"data": {"message_id": "notice"}}
        self.client.send_post.side_effect = send
        self.worker.deliver_lifecycle_notices()
        self.assertIn("concurrent", self.store.state["messages"])

    def test_completed_child_is_archived_once_and_history_is_retained(self):
        self.child()
        before = copy.deepcopy(self.store.state["messages"]["one"])
        with patch.object(self.worker, "_archive_temporary_thread") as archive:
            self.worker.reconcile_branch_archives()
            self.worker.reconcile_branch_archives()
        archive.assert_called_once_with("child")
        after = self.store.state["messages"]["one"]
        for key, value in before.items():
            if key != "updated_at":
                self.assertEqual(value, after[key])
        self.assertEqual("main", self.store.thread_id())
        self.assertEqual("archived", after["branch_archive_state"])

    def test_archive_error_retries_without_rerunning_codex_or_reply(self):
        self.child()
        with patch.object(self.worker, "_archive_temporary_thread", side_effect=RuntimeError("offline")):
            self.worker.reconcile_branch_archives()
        self.assertEqual("completed", self.store.state["messages"]["one"]["status"])
        with patch.object(lifecycle.time, "time", return_value=time.time() + 60), \
                patch.object(self.worker, "_archive_temporary_thread") as archive:
            self.worker.reconcile_branch_archives()
        archive.assert_called_once_with("child")
        gateway.run_codex.assert_not_called()
        self.client.reply_post.assert_not_called()

    def test_active_and_retryable_children_are_not_archived(self):
        self.child(status="processing")
        with patch.object(self.worker, "_archive_temporary_thread") as archive:
            self.worker.reconcile_branch_archives()
            self.store.finish("one", "failed", attempts=1)
            self.worker.reconcile_branch_archives()
            self.store.finish("one", "completed")
            self.worker.active_branches["one"] = object()
            self.worker.reconcile_branch_archives()
        archive.assert_not_called()

    def test_main_thread_cannot_be_archived_as_a_child(self):
        self.child(child="main")
        with patch.object(self.worker, "_archive_temporary_thread") as archive:
            self.worker.reconcile_branch_archives()
        archive.assert_not_called()
        self.assertIn("Refusing", self.store.state["messages"]["one"]["branch_archive_error"])

    def test_startup_cleanup_runs_without_a_new_message(self):
        self.child()
        called = threading.Event()
        with patch.object(self.worker, "_archive_temporary_thread", side_effect=lambda _: called.set()):
            thread = self.worker.start_lifecycle()
            self.assertTrue(called.wait(2))
            self.worker.lifecycle_stop.set()
            self.worker.lifecycle_signal.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_archive_rpc_does_not_hold_store_lock(self):
        self.child()
        def archive(_):
            done = threading.Event()
            writer = threading.Thread(target=lambda: (self.enqueue("normal"), done.set()))
            writer.start()
            self.assertTrue(done.wait(1))
            writer.join()
        with patch.object(self.worker, "_archive_temporary_thread", side_effect=archive):
            self.worker.reconcile_branch_archives()

    def test_english_upgrade_message(self):
        self.project["language"] = "en"
        self.enqueue()
        self.enter()
        self.worker.deliver_lifecycle_notices()
        self.assertIn("upgrading", self.client.reply_post.call_args.args[1])

    def test_interrupted_stop_cannot_silently_become_completed_after_restart(self):
        self.enqueue()
        self.store.next_pending_batch(quiet_window_seconds=0)
        self.enter()
        restarted = gateway.ProjectWorker("test", self.project, self.client, self.root / "log")
        self.assertEqual("processing", restarted.store.state["messages"]["one"]["status"])
        self.assertIn("maintenance_stop_error", restarted.store.state["messages"]["one"])
        with self.assertRaises(gateway.GatewayError):
            restarted.store.exit_maintenance()

    def test_maintenance_request_uses_existing_listener_and_no_second_store_writer(self):
        request_dir = self.root / "sync_requests"
        response_dir = self.root / "sync_responses"
        request_dir.mkdir()
        response_dir.mkdir()
        gateway.atomic_write_json(request_dir / "request.json", {
            "project_keys": ["test"], "maintenance_action": "enter", "reason": "repair",
        })
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        service.log_path = self.root / "log"
        service.catch_up = MagicMock()
        class EndLoop(BaseException):
            pass
        with patch.object(gateway, "SYNC_REQUESTS", request_dir), \
                patch.object(gateway, "SYNC_RESPONSES", response_dir), \
                patch.object(gateway.time, "sleep", side_effect=EndLoop):
            with self.assertRaises(EndLoop):
                service.process_sync_requests()
        response = gateway.load_json(response_dir / "request.json", {})
        self.assertTrue(response["ok"])
        self.assertTrue(self.store.pause_requested())
        self.assertFalse((request_dir / "request.json").exists())
        service.catch_up.assert_not_called()

    def test_cli_missing_listener_retains_request_and_does_not_claim_success(self):
        config = {"app_id": "synthetic", "expected_tenant_key": "synthetic", "projects": {"test": self.project}}
        args = SimpleNamespace(action="enter", project_key="test", reason="repair",
                               release_id=None, text_file=None, ack_timeout=0)
        request_dir = self.root / "requests"
        with patch.object(gateway, "load_config", return_value=config), \
                patch.object(gateway, "SYNC_REQUESTS", request_dir), \
                patch.object(gateway, "SYNC_RESPONSES", self.root / "responses"):
            with self.assertRaisesRegex(gateway.GatewayError, "not acknowledged"):
                gateway.request_maintenance(args)
        self.assertEqual(1, len(list(request_dir.glob("*.json"))))
        self.assertFalse(self.store.pause_requested())

    def test_completion_send_does_not_wait_for_slow_archive_recovery(self):
        request_dir = self.root / "requests"
        response_dir = self.root / "responses"
        gateway.atomic_write_json(request_dir / "notice.json", {
            "project_keys": ["test"], "maintenance_action": "notify-complete", "release_id": "release",
        })
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        service.log_path = self.root / "log"
        class EndLoop(BaseException):
            pass
        with self.worker.lifecycle_archive_lock, \
                patch.object(gateway, "SYNC_REQUESTS", request_dir), \
                patch.object(gateway, "SYNC_RESPONSES", response_dir), \
                patch.object(gateway.time, "sleep", side_effect=EndLoop):
            with self.assertRaises(EndLoop):
                service.process_sync_requests()
        self.client.send_post.assert_called_once()
        self.assertEqual("delivered", self.store.state["lifecycle_notices"]["complete:release"]["status"])

    def test_maintenance_cli_is_available_without_new_product_flags(self):
        args = gateway.parser().parse_args([
            "maintenance", "notify-complete", "--project-key", "test", "--release-id", "release"])
        self.assertEqual("notify-complete", args.action)
        self.assertEqual("release", args.release_id)

    def test_process_has_no_model_deadline(self):
        self.enqueue()
        process = MagicMock()
        process.returncode = 0
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 1)] * 3 + [None]
        with patch.object(lifecycle.subprocess, "Popen", return_value=process) as spawn:
            result = lifecycle.run_model_process(["fake"], input=b"request", store=self.store,
                    message_ids=["one"], creationflags=gateway.CREATE_NO_WINDOW, cwd=str(self.root))
        self.assertEqual(0, result.returncode)
        self.assertEqual(4, process.communicate.call_count)
        self.assertEqual(gateway.CREATE_NO_WINDOW, spawn.call_args.kwargs["creationflags"])
        self.assertIsNone(process.communicate.call_args.kwargs["input"])
        process.kill.assert_not_called()

    def test_process_paused_before_launch_starts_nothing(self):
        self.enter()
        with patch.object(lifecycle.subprocess, "Popen") as spawn, self.assertRaises(lifecycle.MaintenancePaused):
            lifecycle.run_model_process(["fake"], input=b"", store=self.store, message_ids=[])
        spawn.assert_not_called()

    def test_posix_stop_also_cleans_descendants_after_root_exit(self):
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        with patch.object(lifecycle.os, "name", "posix"), \
                patch.object(lifecycle.signal, "SIGKILL", 9, create=True), \
                patch.object(lifecycle.os, "killpg", create=True) as kill:
            lifecycle._stop_process_tree(process)
        self.assertEqual([lifecycle.signal.SIGTERM, 9],
                         [call.args[1] for call in kill.call_args_list])

    def test_windows_stop_targets_only_owned_tree_without_a_window(self):
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        with patch.object(lifecycle.os, "name", "nt"), \
                patch.object(lifecycle.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as kill:
            lifecycle._stop_process_tree(process)
        self.assertEqual(["taskkill.exe", "/PID", "12345", "/T", "/F"], kill.call_args.args[0])
        self.assertEqual(gateway.CREATE_NO_WINDOW, kill.call_args.kwargs["creationflags"])

    def test_real_owned_process_stops_for_explicit_maintenance(self):
        self.enqueue()
        marker = self.root / "started"
        script = "import pathlib,time;pathlib.Path(" + repr(str(marker)) + ").touch();time.sleep(60)"
        errors = []
        def run():
            try:
                lifecycle.run_model_process([sys.executable, "-c", script], input=b"", store=self.store,
                    message_ids=["one"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=gateway.CREATE_NO_WINDOW, cwd=str(self.root))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        self.enter()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], lifecycle.MaintenancePaused)


if __name__ == "__main__":
    unittest.main()
