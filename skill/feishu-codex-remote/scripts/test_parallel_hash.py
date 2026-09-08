"""Isolated branch transport tests; no credentials, network, or model calls."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import remote_gateway as gateway
from codex_thread_fork import ThreadForkError


def message(mid, text="# side task", *, status="pending", created=1000):
    return {
        "message_id": mid, "chat_id": "test-chat", "sender": {"id": "test-user"},
        "message_type": "text", "content": json.dumps({"text": text}),
        "create_time": created, "received_at": "2000-01-01T00:00:00+00:00",
        "status": status, "attempts": 0,
    }


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("isolated branch did not reach expected state")
        time.sleep(0.005)


class SimulatedExit(BaseException):
    """Stop the isolated perpetual entrypoint without exiting the test runner."""


class ParallelHashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = patch.object(gateway, "REMOTE_STATE", self.root)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        # Fail closed if a test forgets to supply a fake for an external operation.
        for name in ("fork_thread", "start_thread", "archive_thread", "run_model_process"):
            guard = patch.object(gateway, name, side_effect=AssertionError("unexpected external " + name))
            guard.start()
            self.addCleanup(guard.stop)
        self.process_guard = patch.object(gateway.subprocess, "run", side_effect=AssertionError("unexpected subprocess"))
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)
        self.cli_guard = patch.object(gateway, "codex_cli_path", return_value=Path("fake-codex.exe"))
        self.cli_guard.start()
        self.addCleanup(self.cli_guard.stop)
        self.client = MagicMock()
        self.client.add_reaction.return_value = {"data": {"reaction_id": "fake-reaction"}}
        self.project = {"working_directory": str(self.root), "chat_id": "test-chat",
                        "agent_model": "test-model", "agent_reasoning_effort": "high",
                        "agent_service_tier": "fast"}
        self.worker = gateway.ProjectWorker("test", self.project, self.client, self.root / "gateway.log")
        self.store = self.worker.store
        self.store.set_thread_id("main-A")
        self.release = threading.Event()
        self.addCleanup(self.finish_branches)

    def finish_branches(self):
        self.release.set()
        with self.store.lock:
            threads = list(self.worker.active_branches.values())
        for thread in threads:
            thread.join(timeout=3)

    def put(self, *rows):
        with self.store.lock:
            self.store.state["messages"].update({row["message_id"]: row for row in rows})
            self.store.save()

    def idle(self):
        with self.store.lock:
            return not self.worker.active_branches

    def test_hash_marker_is_single_use_and_keeps_response_routing(self):
        row = message("hash", " \n # /doc side task")
        self.assertTrue(gateway.parallel_message(row))
        self.assertFalse(gateway.forced_single_message(row))
        self.assertEqual("doc", gateway.explicit_routing_mode(row))
        self.assertEqual("side task", gateway.normalized_user_text(row))
        combined = gateway.combined_batch_item([row])
        self.assertEqual("/doc side task", gateway.message_text(combined["content"]))
        self.assertEqual(" \n # /doc side task", json.loads(row["content"])["text"])
        self.assertFalse(gateway.parallel_message(message("ordinary", "text # inline")))
        self.assertFalse(gateway.parallel_message(message("star", "* # literal")))
        self.assertEqual("direct", gateway.explicit_routing_mode(message("direct", "# /direct short")))

    def test_main_selection_ignores_pending_failed_and_processing_hashes(self):
        self.put(message("pending"), message("failed", status="failed"),
                 message("running", status="processing"), message("main", "ordinary"))
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual(["main"], [row["message_id"] for row in batch])
        for mid, status in (("pending", "pending"), ("failed", "failed"), ("running", "processing")):
            self.assertEqual(status, self.store.state["messages"][mid]["status"])

    def test_main_processing_still_blocks_second_main_claim(self):
        self.put(message("main-active", "ordinary", status="processing"), message("main-next", "next"))
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.assertEqual("pending", self.store.state["messages"]["main-next"]["status"])

    def test_two_hashes_overlap_main_and_are_claimed_once(self):
        self.put(message("main", "ordinary", status="processing"), message("one"), message("two"))
        started = []
        children = []

        def process(rows):
            self.assertEqual(1, len(rows))
            mid = rows[0]["message_id"]
            with self.store.lock:
                self.assertEqual("processing", self.store.state["messages"][mid]["status"])
                started.append(mid)
                children.append(rows[0]["branch_thread_id"])
            self.release.wait(3)
            self.store.finish(mid, "completed")

        with patch.object(gateway, "fork_thread", side_effect=["child-one", "child-two"]), \
                patch.object(self.worker, "_process_batch", side_effect=process):
            self.worker.dispatch_parallel()
            wait_until(lambda: len(started) == 2)
            self.assertEqual({"one", "two"}, set(started))
            self.assertEqual({"child-one", "child-two"}, set(children))
            self.assertEqual("processing", self.store.state["messages"]["main"]["status"])
            self.worker.dispatch_parallel()
            self.assertEqual(2, len(started))
            self.assertEqual([1, 1], [self.store.state["messages"][mid]["attempts"] for mid in ("one", "two")])
            self.release.set()
            wait_until(self.idle)
            self.worker.dispatch_parallel()
            self.assertEqual(2, len(started))

    def test_new_hash_uses_current_main_even_when_replying_to_old_branch(self):
        self.put(message("one"))
        parents = []

        def fork(parent, *_args, **_kwargs):
            parents.append(parent)
            return "child-" + str(len(parents))

        def process(rows):
            self.store.finish(rows[0]["message_id"], "completed")

        with patch.object(gateway, "fork_thread", side_effect=fork), patch.object(self.worker, "_process_batch", side_effect=process):
            self.worker.dispatch_parallel()
            wait_until(self.idle)
            self.store.set_thread_id("main-B")
            second = message("two")
            second["parent_id"] = "one"
            self.put(second)
            self.worker.dispatch_parallel()
            wait_until(self.idle)
        self.assertEqual(["main-A", "main-B"], parents)
        self.assertEqual("main-B", self.store.thread_id())

    def test_closed_admission_preserves_pending_in_both_lanes(self):
        self.put(message("hash"), message("ordinary", "ordinary"))
        before = json.loads(json.dumps(self.store.state["messages"]))
        with self.store.lock:
            self.store.admission_open = False
        with patch.object(self.worker, "_process_batch") as process:
            self.worker.dispatch_parallel()
            self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
            self.assertIsNone(self.store.pending_wait_seconds())
            process.assert_not_called()
        self.assertEqual(before, self.store.state["messages"])

    def test_first_hash_bootstraps_main_with_existing_profile(self):
        self.store.set_thread_id("")
        with patch.object(gateway, "start_thread", return_value="bootstrapped-main") as start, \
                patch.object(gateway, "codex_cli_path", return_value=Path("fake-codex.exe")):
            self.assertEqual("bootstrapped-main", self.worker._branch_parent())
            self.assertEqual("bootstrapped-main", self.worker._branch_parent())
        start.assert_called_once()
        self.assertEqual("test-model", start.call_args.kwargs["model"])
        self.assertEqual("high", start.call_args.kwargs["reasoning_effort"])
        self.assertEqual("bootstrapped-main", self.store.thread_id())

    def test_active_main_thread_event_is_available_before_saved_thread(self):
        self.store.set_thread_id("")
        event_log = self.root / "main-events.jsonl"
        event_log.write_text('{"type":"thread.started","thread_id":"active-main"}\n', encoding="utf-8")
        row = message("main", "ordinary", status="processing")
        row["run_event_log"] = str(event_log)
        self.put(row)
        self.assertEqual("active-main", self.worker._branch_parent())

    def test_failed_first_main_keeps_confirmed_thread_from_event_log(self):
        self.store.set_thread_id("")
        event_log = self.root / "main-events.jsonl"
        event_log.write_text('{"type":"thread.started","thread_id":"failed-main"}\n', encoding="utf-8")
        row = message("main", "ordinary", status="failed")
        row.update(run_event_log=str(event_log), attempts=1)
        self.put(row)
        self.assertEqual("failed-main", self.worker._branch_parent())

    def test_thread_start_failure_restores_original_pending_record(self):
        row = message("hash")
        self.put(row)
        before = dict(row)
        with patch.object(gateway.threading.Thread, "start", side_effect=RuntimeError("cannot start thread")):
            with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                self.worker.dispatch_parallel()
        self.assertEqual(before, self.store.state["messages"]["hash"])
        self.assertTrue(self.idle())

    def test_bootstrap_unknown_is_not_repeated_and_main_start_is_not_raced(self):
        self.store.set_thread_id("")
        self.put(message("main", "ordinary", status="processing"))
        self.assertEqual("", self.worker._branch_parent())
        self.store.finish("main", "failed")
        with patch.object(gateway, "start_thread", side_effect=TimeoutError("unknown bootstrap")) as start, \
                patch.object(gateway, "codex_cli_path", return_value=Path("fake-codex.exe")):
            with self.assertRaises(TimeoutError):
                self.worker._branch_parent()
            self.assertEqual("", self.worker._branch_parent())
            start.assert_called_once()
        self.assertEqual("", self.store.thread_id())

    def test_reload_waits_for_active_branch_but_retains_new_pending(self):
        self.put(message("branch", status="processing"), message("new-hash"), message("ordinary", "ordinary"))
        service = object.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        service.set_admission(False)
        self.worker.active_branches["branch"] = MagicMock()
        with patch.object(gateway.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(gateway.GatewayError):
                service.wait_for_quiescence(["test"], timeout=1, quiet_seconds=0)
        self.store.finish("branch", "completed")
        # Completion persisted before the thread deregisters is still active.
        with patch.object(gateway.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(gateway.GatewayError):
                service.wait_for_quiescence(["test"], timeout=1, quiet_seconds=0)
        self.worker.active_branches.clear()
        with patch.object(gateway.time, "monotonic", side_effect=[0, 0, 0, 0, 0]):
            snapshot = service.wait_for_quiescence(["test"], timeout=1, quiet_seconds=0)
        self.assertEqual(2, snapshot["counts"]["test"]["pending"])
        self.assertEqual("pending", self.store.state["messages"]["new-hash"]["status"])
        self.assertEqual("pending", self.store.state["messages"]["ordinary"]["status"])
        self.assertFalse(self.store.admission_open)
        service.set_admission(True)
        self.assertTrue(self.store.admission_open)

    def test_real_reload_entry_freezes_before_both_pulls_and_preserves_arrivals(self):
        service = object.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        service.log_path = self.root / "reload.log"
        requests = self.root / "reload-requests"
        responses = self.root / "reload-responses"
        gateway.atomic_write_json(requests / "reload-test.json", {"wait_timeout": 2})
        pulls = []

        def catch_up(keys):
            self.assertFalse(self.store.admission_open)
            self.assertEqual(["test"], keys)
            index = len(pulls)
            self.put(message("ordinary-" + str(index), "ordinary arrival"),
                     message("hash-" + str(index), "# independent arrival"))
            pulls.append(index)
            return {"test": 2}

        def wait(keys, timeout):
            return gateway.GatewayService.wait_for_quiescence(service, keys, timeout, quiet_seconds=0)

        with patch.object(gateway, "RELOAD_REQUESTS", requests), \
                patch.object(gateway, "RELOAD_RESPONSES", responses), \
                patch.object(service, "catch_up", side_effect=catch_up), \
                patch.object(service, "wait_for_quiescence", side_effect=wait), \
                patch.object(gateway.time, "sleep"), \
                patch.object(gateway.os, "_exit", side_effect=SimulatedExit) as exit_process:
            with self.assertRaises(SimulatedExit):
                service.process_reload_requests()
        exit_process.assert_called_once_with(75)
        self.assertEqual([0, 1], pulls)
        response = json.loads((responses / "reload-test.json").read_text(encoding="utf-8"))
        self.assertTrue(response["ok"])
        self.assertEqual({"test": 2}, response["added"])
        self.assertEqual({"test": 2}, response["final_added"])
        self.assertEqual(4, response["counts"]["test"]["pending"])
        self.assertFalse((requests / "reload-test.json").exists())
        self.assertFalse(self.store.admission_open)
        restarted = gateway.ProjectStore("test")
        self.assertEqual(4, len(restarted.state["messages"]))
        for row in restarted.state["messages"].values():
            self.assertEqual("pending", row["status"])
            self.assertEqual(0, row["attempts"])
        self.client.assert_not_called()
        self.client.reply_post.assert_not_called()

    def test_real_reload_entry_failure_reopens_admission_and_preserves_pending(self):
        self.put(message("retained-hash"), message("retained-main", "ordinary"))
        before = json.loads(json.dumps(self.store.state["messages"]))
        service = object.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        service.log_path = self.root / "reload.log"
        requests = self.root / "reload-requests"
        responses = self.root / "reload-responses"
        gateway.atomic_write_json(requests / "reload-failure.json", {"wait_timeout": 2})

        def catch_up(_keys):
            self.assertFalse(self.store.admission_open)
            raise RuntimeError("isolated history pull failure")

        with patch.object(gateway, "RELOAD_REQUESTS", requests), \
                patch.object(gateway, "RELOAD_RESPONSES", responses), \
                patch.object(service, "catch_up", side_effect=catch_up), \
                patch.object(gateway.time, "sleep", side_effect=SimulatedExit), \
                patch.object(gateway.os, "_exit") as exit_process:
            with self.assertRaises(SimulatedExit):
                service.process_reload_requests()
        exit_process.assert_not_called()
        response = json.loads((responses / "reload-failure.json").read_text(encoding="utf-8"))
        self.assertFalse(response["ok"])
        self.assertIn("isolated history pull failure", response["error"])
        self.assertTrue(self.store.admission_open)
        self.assertEqual(before, self.store.state["messages"])
        self.assertEqual(before, gateway.ProjectStore("test").state["messages"])
        self.assertTrue(self.worker.signal.is_set())
        self.assertTrue(self.worker.branch_signal.is_set())

    def test_unknown_root_prevents_new_main_claim_or_busy_retry(self):
        self.store.set_thread_id("")
        self.store.state["main_bootstrap_state"] = "outcome_unknown"
        self.put(message("main", "ordinary"))
        before = dict(self.store.state["messages"]["main"])
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.assertIsNone(self.store.pending_wait_seconds())
        self.assertEqual(before, self.store.state["messages"]["main"])

    def test_unknown_fork_or_root_is_terminal_for_sync_while_processing_stays_active(self):
        service = object.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        for root_unknown in (False, True):
            with self.subTest(root_unknown=root_unknown):
                self.store.state["messages"] = {}
                self.store.state["main_bootstrap_state"] = "outcome_unknown" if root_unknown else "ready"
                rows = [message("pending", "ordinary" if root_unknown else "# side", status="pending"),
                        message("failed", "ordinary" if root_unknown else "# side", status="failed"),
                        message("running", "ordinary" if root_unknown else "# side", status="processing")]
                if not root_unknown:
                    for row in rows:
                        row["branch_state"] = "fork_outcome_unknown"
                self.put(*rows)
                snapshot = service.queue_snapshot(["test"])
                self.assertEqual(["running"], [row["message_id"] for row in snapshot["active"]])
                self.assertEqual({"pending", "failed"},
                                 {row["message_id"] for row in snapshot["terminal_failures"]})
                self.assertTrue(all(row["error"] for row in snapshot["terminal_failures"]))
                self.assertEqual({"pending": 1, "failed": 1, "processing": 1}, snapshot["counts"]["test"])

    def test_unknown_fork_is_durable_and_not_blindly_retried(self):
        self.put(message("hash"))
        with patch.object(gateway, "fork_thread", side_effect=TimeoutError("fork outcome unknown")) as fork, \
                patch.object(self.worker, "_process_batch") as process:
            self.worker.dispatch_parallel()
            wait_until(self.idle)
            row = self.store.state["messages"]["hash"]
            self.assertEqual("fork_outcome_unknown", row["branch_state"])
            self.worker.dispatch_parallel()
            wait_until(self.idle)
            fork.assert_called_once()
            process.assert_not_called()
            restarted = gateway.ProjectStore("test")
            restarted.recover_interrupted()
            self.assertEqual("fork_outcome_unknown", restarted.state["messages"]["hash"]["branch_state"])

    def test_fork_not_submitted_can_retry_without_unknown_outcome_gate(self):
        self.put(message("hash"))
        error = ThreadForkError("app-server initialization failed")

        def process(rows):
            self.store.finish(rows[0]["message_id"], "completed")

        with patch.object(gateway, "fork_thread", side_effect=[error, "child"]) as fork, \
                patch.object(self.worker, "_process_batch", side_effect=process) as process_mock:
            self.worker.dispatch_parallel()
            wait_until(self.idle)
            row = self.store.state["messages"]["hash"]
            self.assertEqual("not_submitted", row["branch_state"])
            self.assertEqual("failed", row["status"])
            process_mock.assert_not_called()
            self.worker.dispatch_parallel()
            wait_until(self.idle)
            self.assertEqual(2, fork.call_count)
            process_mock.assert_called_once()
        self.assertEqual("completed", self.store.state["messages"]["hash"]["status"])
        self.assertEqual("child", self.store.state["messages"]["hash"]["branch_thread_id"])

    def test_bootstrap_not_submitted_can_retry_after_initialization_recovers(self):
        self.store.set_thread_id("")
        error = ThreadForkError("app-server initialization failed")
        with patch.object(gateway, "start_thread", side_effect=[error, "main-ready"]) as start:
            with self.assertRaises(ThreadForkError):
                self.worker._branch_parent()
            self.assertNotEqual("outcome_unknown", self.store.state.get("main_bootstrap_state"))
            self.assertEqual("main-ready", self.worker._branch_parent())
            self.assertEqual(2, start.call_count)
        self.assertEqual("main-ready", self.store.thread_id())

    def test_confirmed_child_is_reused_when_execution_retries(self):
        row = message("hash", status="failed")
        row.update(attempts=1, branch_thread_id="saved-child", branch_parent_thread_id="original-main")
        self.put(row)
        received = []

        def process(rows):
            received.append(dict(rows[0]))
            self.store.finish(rows[0]["message_id"], "completed")

        with patch.object(gateway, "fork_thread") as fork, patch.object(self.worker, "_process_batch", side_effect=process):
            self.worker.dispatch_parallel()
            wait_until(self.idle)
            fork.assert_not_called()
        self.assertEqual(1, len(received))
        self.assertEqual("saved-child", received[0]["branch_thread_id"])
        self.assertEqual("main-A", self.store.thread_id())

    def test_branch_run_resumes_child_preserving_model_flags_and_main(self):
        row = message("hash", status="processing")
        row["branch_thread_id"] = "child"
        self.put(row)
        captured = {}

        def run(command, **kwargs):
            captured.update(command=command, kwargs=kwargs)
            Path(command[command.index("-o") + 1]).write_text("answer", encoding="utf-8")
            kwargs["stdout"].write(b'{"type":"thread.started","thread_id":"child"}\n')
            return SimpleNamespace(returncode=0)

        with patch.object(gateway, "codex_cli_path", return_value=Path("fake-codex.exe")), \
                patch.object(gateway, "run_model_process", side_effect=run):
            answer, thread, _path = gateway.run_codex("test", self.project, self.store,
                                                   gateway.combined_batch_item([row]), [self.root / "image.png"])
        command = captured["command"]
        self.assertEqual(("answer", "child"), (answer, thread))
        self.assertEqual("main-A", self.store.thread_id())
        self.assertEqual(["resume", "child", "--image", str(self.root / "image.png"), "-"], command[-5:])
        self.assertLess(command.index("--model"), command.index("resume"))
        for option in ('model_reasoning_effort="high"', 'service_tier="fast"', "features.fast_mode=true"):
            self.assertIn(option, command[:command.index("resume")])
        self.assertEqual("test-model", command[command.index("--model") + 1])
        self.assertEqual(gateway.CREATE_NO_WINDOW, captured["kwargs"]["creationflags"])
        self.assertNotIn("timeout", captured["kwargs"])

    def test_branch_run_rejects_unexpected_thread_identity(self):
        row = message("hash", status="processing")
        row["branch_thread_id"] = "child"
        self.put(row)

        def run(_command, **kwargs):
            kwargs["stdout"].write(b'{"type":"thread.started","thread_id":"different-thread"}\n')
            return SimpleNamespace(returncode=0)

        with patch.object(gateway, "codex_cli_path", return_value=Path("fake-codex.exe")), \
                patch.object(gateway, "run_model_process", side_effect=run):
            with self.assertRaisesRegex(gateway.GatewayError, "different branch thread"):
                gateway.run_codex("test", self.project, self.store, row, [])
        self.assertEqual("main-A", self.store.thread_id())

    def test_concurrent_branches_get_distinct_run_paths_at_identical_time(self):
        rows = [message("hash-one", status="processing"), message("hash-two", status="processing")]
        for index, row in enumerate(rows):
            row["branch_thread_id"] = "child-" + str(index)
        self.put(*rows)
        paths = []
        outcomes = []
        failures = []
        fixed_time = gateway.datetime(2026, 1, 1, 12, 0, 0)

        def run(command, **kwargs):
            final = Path(command[command.index("-o") + 1])
            thread_id = command[command.index("resume") + 1]
            final.write_text(thread_id, encoding="utf-8")
            kwargs["stdout"].write((json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n").encode())
            paths.append(final)
            return SimpleNamespace(returncode=0)

        def invoke(row):
            try:
                outcomes.append(gateway.run_codex("test", self.project, self.store, row, []))
            except Exception as exc:
                failures.append(exc)

        with patch.object(gateway, "datetime") as clock, patch.object(gateway, "run_model_process", side_effect=run):
            clock.now.return_value = fixed_time
            threads = [threading.Thread(target=invoke, args=(row,)) for row in rows]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(2, len(set(paths)))
        self.assertEqual({("child-0", "child-0"), ("child-1", "child-1")},
                         {(answer, thread_id) for answer, thread_id, _path in outcomes})
        self.assertEqual("main-A", self.store.thread_id())

    def test_branch_completion_targets_only_original_message_and_keeps_main(self):
        branch = message("hash", status="processing")
        branch["branch_thread_id"] = "child"
        self.put(branch, message("ordinary", "ordinary"))
        final = self.root / "final.md"
        final.write_text("answer", encoding="utf-8")
        with patch.object(gateway, "run_codex", return_value=("answer", "child", final)) as run, \
                patch.object(gateway, "reply_complete", return_value="") as reply:
            self.worker._process_batch([dict(branch)])
        run.assert_called_once()
        reply.assert_called_once()
        self.assertEqual("hash", reply.call_args.args[2]["message_id"])
        self.assertEqual("completed", self.store.state["messages"]["hash"]["status"])
        self.assertEqual("pending", self.store.state["messages"]["ordinary"]["status"])
        self.assertEqual("main-A", self.store.thread_id())
        self.assertEqual("child", self.store.state["messages"]["hash"]["branch_thread_id"])


if __name__ == "__main__":
    unittest.main()
