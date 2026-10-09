"""History reconciliation invariants without network, model or worker execution."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import remote_gateway as gateway


class CatchupHealthTests(unittest.TestCase):
    BASE = 1791530000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote_state = patch.object(gateway, "REMOTE_STATE", self.root)
        self.remote_state.start()
        self.addCleanup(self.remote_state.stop)
        self.service = gateway.GatewayService.__new__(gateway.GatewayService)
        self.service.log_path = self.root / "gateway.log"
        self.service.config = {"expected_tenant_key": "fixture-tenant"}
        self.service.reconnect_signal = threading.Event()
        self.service.client = SimpleNamespace(list_messages=Mock(return_value=[]))
        self.service.workers = {}
        self.service.chat_to_key = {}

    def worker(self, key="fixture"):
        project = {"chat_id": "chat-" + key, "registered_epoch": self.BASE,
                   "hourly_catch_up_enabled": False, "working_directory": str(self.root)}
        worker = gateway.ProjectWorker(key, project, self.service.client, self.service.log_path)
        worker.store.set_thread_id("original-thread-" + key)
        worker.store.state["cursor"] = self.BASE
        worker.store.save()
        self.service.workers[key] = worker
        self.service.chat_to_key[project["chat_id"]] = key
        return worker

    def message(self, mid, seconds, key="fixture", sender_type="user"):
        return {"message_id": mid, "chat_id": "chat-" + key, "msg_type": "text",
                "create_time": str((self.BASE + seconds) * 1000 + 427),
                "body": {"content": '{"text":"synthetic fixture"}'},
                "sender": {"sender_type": sender_type, "id": "fixture-sender"}}

    def test_failed_request_commit_wakes_existing_loop_and_recovers_without_poke(self):
        from test_network_flap_recovery import FixtureStopped, StoppableSignal
        import gateway_recovery
        worker = self.worker()
        self.service.ws_client = SimpleNamespace(_reconnect_interval=0.01)
        self.service.reconnect_signal = StoppableSignal()
        already_waiting = threading.Event()
        original_wait = self.service.reconnect_signal.wait
        def wait(timeout=None):
            if timeout is None:
                already_waiting.set()
            return original_wait(timeout)
        self.service.reconnect_signal.wait = wait
        history_online = threading.Event()
        finished = threading.Event()
        failures = []
        def history(chat, start):
            if not history_online.is_set():
                raise OSError("fixture unavailable")
            return [self.message("retained-desired-work", 10)]
        self.service.client.list_messages.side_effect = history
        original_complete = self.service.recovery().complete
        def complete(key, revision):
            original_complete(key, revision)
            finished.set()
        self.service.recovery().complete = complete
        def run():
            try:
                self.service.reconcile_reconnects()
            except FixtureStopped:
                pass
            except BaseException as exc:
                failures.append(type(exc).__name__)
        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        try:
            self.assertTrue(already_waiting.wait(2))
            with patch.object(gateway_recovery, "atomic_write_json", side_effect=OSError("fixture disk busy")):
                with self.assertRaises(OSError):
                    self.service.request_catch_up()
                self.assertIn("fixture", self.service.recovery().pending())
            # Restoring IO is the only recovery action; no manual wake or pull.
            history_online.set()
            self.assertTrue(finished.wait(2), "Original recovery loop never resumed")
            self.assertEqual(self.service.recovery().pending(), {})
            self.assertEqual(set(worker.store.state["messages"]), {"retained-desired-work"})
            self.assertEqual(worker.store.thread_id(), "original-thread-fixture")
            self.assertEqual(failures, [])
        finally:
            self.service.reconnect_signal.stopped = True
            self.service.reconnect_signal.set()
            runner.join(2)
        self.assertFalse(runner.is_alive())

    def test_new_realtime_before_backfill_does_not_skip_offline_messages(self):
        worker = self.worker()
        source = [self.message("offline-one", 10), self.message("offline-two", 20),
                  self.message("fresh-realtime", 100)]
        self.service.client.list_messages.side_effect = lambda chat, start: [
            copy.deepcopy(row) for row in source
            if int(row["create_time"]) // 1000 >= start]
        self.assertTrue(self.service.enqueue(gateway.normalize_history_message(source[-1]), "fixture-tenant"))
        # A new websocket event is not evidence that older history was read.
        self.assertEqual(worker.store.state["cursor"], self.BASE)
        self.assertEqual(self.service.catch_up(), {"fixture": 2})
        self.assertEqual(self.service.catch_up(), {"fixture": 0})
        self.assertEqual(set(worker.store.state["messages"]),
                         {"offline-one", "offline-two", "fresh-realtime"})
        self.assertEqual(worker.store.thread_id(), "original-thread-fixture")
        self.assertTrue(all(row["status"] == "pending"
                            for row in worker.store.state["messages"].values()))
        self.assertTrue(worker.signal.is_set())
        self.assertTrue(worker.branch_signal.is_set())

    def test_ignored_realtime_message_cannot_move_history_cursor(self):
        worker = self.worker()
        ignored = self.message("fresh-bot-event", 100, sender_type="app")
        self.assertFalse(self.service.enqueue(gateway.normalize_history_message(ignored), "fixture-tenant"))
        self.assertEqual(worker.store.state["cursor"], self.BASE)
        self.service.client.list_messages.return_value = [self.message("older-user", 10), ignored]
        self.assertEqual(self.service.catch_up(), {"fixture": 1})
        self.assertEqual(set(worker.store.state["messages"]), {"older-user"})
        # Successful history may advance across ignored bot rows too.
        self.assertEqual(worker.store.state["cursor"], self.BASE + 100)

    def test_one_failed_group_does_not_block_another_and_failed_cursor_is_retained(self):
        failed = self.worker("failed")
        successful = self.worker("successful")
        def history(chat, start):
            if chat == "chat-failed":
                raise OSError("fixture history unavailable")
            return [self.message("success-message", 30, "successful")]
        self.service.client.list_messages.side_effect = history
        with self.assertRaises(gateway.GatewayError):
            self.service.catch_up()
        self.assertEqual(failed.store.state["cursor"], self.BASE)
        self.assertEqual(successful.store.state["cursor"], self.BASE + 30)
        self.assertEqual(set(successful.store.state["messages"]), {"success-message"})
        self.assertTrue(successful.signal.is_set())
        self.assertEqual(self.service.client.list_messages.call_count, 2)
        self.assertEqual(successful.store.thread_id(), "original-thread-successful")

    def test_successful_history_commits_max_timestamp_only_after_all_rows(self):
        worker = self.worker()
        source = [self.message("newer", 30), self.message("older", 10)]
        self.service.client.list_messages.return_value = source
        original_enqueue = worker.enqueue
        observed_cursors = []
        def observe(message):
            observed_cursors.append(worker.store.state["cursor"])
            return original_enqueue(message)
        with patch.object(worker, "enqueue", side_effect=observe):
            self.assertEqual(self.service.catch_up(), {"fixture": 2})
        self.assertEqual(observed_cursors, [self.BASE, self.BASE])
        self.assertEqual(worker.store.state["cursor"], self.BASE + 30)
        # The commit must survive restart, rather than existing only in memory.
        self.assertEqual(gateway.ProjectStore("fixture").state["cursor"], self.BASE + 30)
        self.service.client.list_messages.return_value = []
        self.assertEqual(self.service.catch_up(), {"fixture": 0})
        self.assertEqual(worker.store.state["cursor"], self.BASE + 30)

    def test_partial_enqueue_failure_retries_without_skipping_or_duplication(self):
        worker = self.worker()
        source = [self.message("first", 10), self.message("second", 20)]
        self.service.client.list_messages.return_value = source
        original_enqueue = worker.enqueue
        def fault(message):
            if message["message_id"] == "second":
                raise OSError("fixture durable write failed")
            return original_enqueue(message)
        with patch.object(worker, "enqueue", side_effect=fault):
            with self.assertRaises(gateway.GatewayError):
                self.service.catch_up()
        self.assertEqual(worker.store.state["cursor"], self.BASE)
        self.assertEqual(gateway.ProjectStore("fixture").state["cursor"], self.BASE)
        self.assertEqual(set(worker.store.state["messages"]), {"first"})
        self.assertEqual(self.service.catch_up(), {"fixture": 1})
        self.assertEqual(self.service.catch_up(), {"fixture": 0})
        self.assertEqual(set(worker.store.state["messages"]), {"first", "second"})
        self.assertEqual(worker.store.state["cursor"], self.BASE + 20)
        self.assertEqual(worker.store.thread_id(), "original-thread-fixture")

    def test_failed_reconnect_intent_survives_service_restart_and_retries(self):
        worker = self.worker()
        self.service.on_ws_reconnected()
        self.service.client.list_messages.side_effect = OSError("fixture offline")
        with self.assertRaises(gateway.GatewayError):
            self.service.catch_up()

        # A replacement service has no in-memory request, yet must retain the
        # unsuccessful reconciliation and use the SDK's existing retry cadence.
        restored = gateway.GatewayService.__new__(gateway.GatewayService)
        restored.log_path = self.service.log_path
        restored.workers = self.service.workers
        restored.client = SimpleNamespace(list_messages=Mock(return_value=[self.message("recovered", 10)]))
        restored.reconnect_signal = Mock()
        restored.reconnect_signal.wait.side_effect = [False, False, StopIteration]
        restored.ws_client = SimpleNamespace(_reconnect_interval=7)
        with self.assertRaises(StopIteration):
            restored.reconcile_reconnects()
        restored.client.list_messages.assert_called_once()
        self.assertEqual(restored.reconnect_signal.wait.call_args_list[0].args, (7,))
        # Once reconciled, the next iteration waits for a new request, rather
        # than retrying the completed group indefinitely.
        self.assertEqual(restored.reconnect_signal.wait.call_args_list[1].args, ())
        self.assertEqual(set(worker.store.state["messages"]), {"recovered"})
        self.assertEqual(worker.store.thread_id(), "original-thread-fixture")

    def test_manual_catchup_failure_wakes_an_idle_recovery_waiter(self):
        self.worker()
        self.service.client.list_messages.side_effect = OSError("fixture offline")
        waiting = threading.Event()
        woke = threading.Event()
        def wait_for_recovery():
            waiting.set()
            if self.service.reconnect_signal.wait(2):
                woke.set()
        waiter = threading.Thread(target=wait_for_recovery, daemon=True)
        waiter.start()
        try:
            self.assertTrue(waiting.wait(2))
            with self.assertRaises(gateway.GatewayError):
                self.service.catch_up()
            self.assertTrue(woke.wait(2), "A new failure must wake idle recovery immediately")
            self.assertIn("fixture", self.service.recovery().pending())
        finally:
            self.service.reconnect_signal.set()
            waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())

    def test_reconnects_own_failure_keeps_retry_cadence_without_self_wakeup(self):
        self.worker()
        self.service.request_catch_up()
        self.service.client.list_messages.side_effect = OSError("fixture offline")
        self.service.ws_client = SimpleNamespace(_reconnect_interval=7)
        self.service.reconnect_signal = Mock()
        self.service.reconnect_signal.wait.side_effect = [False, StopIteration]
        unexpected = []
        def reconcile_once():
            try:
                self.service.reconcile_reconnects()
            except StopIteration:
                pass
            except BaseException as exc:
                unexpected.append(exc)
        owner = threading.Thread(target=reconcile_once, daemon=True)
        self.service._components = {"reconnect-catch-up": owner}
        owner.start()
        owner.join(timeout=2)
        self.assertFalse(owner.is_alive())
        self.assertEqual(unexpected, [])
        self.service.reconnect_signal.set.assert_not_called()
        self.assertEqual([call.args for call in self.service.reconnect_signal.wait.call_args_list],
                         [(7,), (7,)])
        self.service.client.list_messages.assert_called_once()
        self.assertIn("fixture", self.service.recovery().pending())

    def test_reconnect_requested_during_history_scan_survives_older_completion(self):
        worker = self.worker()
        self.service.request_catch_up()
        previous_revision = self.service.recovery().pending()["fixture"]
        def history_with_concurrent_reconnect(chat, start):
            self.service.request_catch_up()
            return [self.message("first-pass", 10)]
        self.service.client.list_messages.side_effect = history_with_concurrent_reconnect
        self.assertEqual(self.service.catch_up(), {"fixture": 1})
        self.assertGreater(self.service.recovery().pending()["fixture"], previous_revision)
        # Completion of the older scan must not consume a newer reconnect's
        # request; a subsequent full pass may safely acknowledge that revision.
        self.service.client.list_messages.side_effect = None
        self.service.client.list_messages.return_value = [self.message("first-pass", 10)]
        self.assertEqual(self.service.catch_up(), {"fixture": 0})
        self.assertNotIn("fixture", self.service.recovery().pending())
        self.assertEqual(set(worker.store.state["messages"]), {"first-pass"})
        self.assertEqual(worker.store.thread_id(), "original-thread-fixture")

    def test_history_does_not_replay_completed_or_terminal_failed_work(self):
        worker = self.worker()
        source = [self.message("completed", 10), self.message("failed", 20)]
        for row in source:
            worker.enqueue(gateway.normalize_history_message(row))
        worker.store.state["messages"]["completed"].update(status="completed", attempts=1,
                                                         answer_sha256="fixture-proof")
        worker.store.state["messages"]["failed"].update(status="failed", attempts=3,
                                                      error="fixture terminal failure")
        worker.store.save()
        before = copy.deepcopy(worker.store.state["messages"])
        self.service.client.list_messages.return_value = source
        self.assertEqual(self.service.catch_up(), {"fixture": 0})
        self.assertEqual(worker.store.state["messages"], before)
        self.assertEqual(worker.store.next_pending_batch(quiet_window_seconds=0), [])
        self.assertEqual(worker.store.thread_id(), "original-thread-fixture")


class ServiceComponentRecoveryTests(unittest.TestCase):
    def service(self, root):
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.log_path = root / "components.log"
        service.workers = {}
        service._component_targets = {}
        service._components = {}
        service._component_errors = {}
        service._component_lock = threading.RLock()
        return service

    def test_complete_component_failure_reenters_even_when_diagnostics_fail(self):
        for fault in (OSError("fixture iteration failure"), SystemExit("fixture worker exit")):
            with self.subTest(fault=type(fault).__name__), tempfile.TemporaryDirectory() as temp:
                service = self.service(Path(temp))
                target = Mock(side_effect=[fault, None])
                # The second target return is also abnormal for a permanent loop.
                # Stop outside the exception boundary after observing both entries.
                with patch.object(gateway, "append_log", side_effect=OSError("fixture log failure")), \
                     patch.object(gateway.time, "sleep", side_effect=[None, StopIteration]):
                    with self.assertRaises(StopIteration):
                        service._component_entry("fixture-component", target)
                self.assertEqual(target.call_count, 2)
                self.assertEqual(service._component_errors["fixture-component"], "RuntimeError")
                service.component_step("fixture-component")
                self.assertNotIn("fixture-component", service._component_errors)

    def test_ensure_components_keeps_live_owner_and_replaces_only_dead_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            release = threading.Event()
            live_started = threading.Event()
            replacement_started = threading.Event()
            def live_target():
                live_started.set()
                release.wait()
            def replacement_target():
                replacement_started.set()
                release.wait()
            live = threading.Thread(target=live_target, daemon=True)
            dead = threading.Thread(target=lambda: None, daemon=True)
            service._component_targets = {"live": live_target, "dead": replacement_target}
            service._components = {"live": live, "dead": dead}
            live.start()
            dead.start()
            dead.join(timeout=2)
            replacement = None
            try:
                self.assertTrue(live_started.wait(2))
                self.assertFalse(dead.is_alive())
                # Real Thread ownership is tested here; the permanent exception
                # boundary is independently exercised in the preceding test.
                with patch.object(service, "_component_entry", side_effect=lambda name, target: target()) as entry:
                    service.ensure_components()
                    replacement = service._components["dead"]
                    self.assertTrue(replacement_started.wait(2))
                    self.assertIs(service._components["live"], live)
                    self.assertIsNot(replacement, dead)
                    service.ensure_components()
                    self.assertIs(service._components["dead"], replacement)
                    self.assertIs(service._components["live"], live)
                    self.assertEqual(entry.call_count, 1)
            finally:
                release.set()
                live.join(timeout=2)
                if replacement is not None:
                    replacement.join(timeout=2)
            self.assertFalse(live.is_alive())
            self.assertFalse(replacement.is_alive())

    def test_worker_supervision_waits_for_existing_prepare_to_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            preparing = threading.Event()
            release_prepare = threading.Event()
            started = threading.Event()
            def reconcile():
                preparing.set()
                release_prepare.wait()
            worker = SimpleNamespace(key="fixture", reconcile_reactions=Mock(side_effect=reconcile),
                                     start=Mock(side_effect=started.set), ensure_worker_threads=Mock())
            service.workers = {"fixture": worker}
            service.start_workers()
            try:
                self.assertTrue(preparing.wait(2))
                service.ensure_components()
                service.ensure_components()
                worker.ensure_worker_threads.assert_not_called()
                worker.start.assert_not_called()
            finally:
                release_prepare.set()
            self.assertTrue(started.wait(2))
            worker.reconcile_reactions.assert_called_once()
            worker.start.assert_called_once()
            service.ensure_components()
            worker.ensure_worker_threads.assert_called_once()


if __name__ == "__main__":
    unittest.main()
