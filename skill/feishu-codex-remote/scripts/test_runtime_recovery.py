"""Fault replay through real dispatch/store code; synthetic history and transport."""
import json
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import codex_thread_fork as fork
import remote_gateway as gateway
import test_parallel_hash as parallel
from history_fork_snapshot import codex_home, snapshot_rollout
from provider_failure import terminal_capacity_failure


CAPACITY = "Selected model is at capacity. Please try again later."


class RuntimeRecoveryTests(unittest.TestCase):
    # Reuse the isolated fixture, not its inherited test methods.
    setUp = parallel.ParallelHashTests.setUp
    finish_branches = parallel.ParallelHashTests.finish_branches
    put = parallel.ParallelHashTests.put
    idle = parallel.ParallelHashTests.idle

    def native(self, outcome="capacity"):
        def execute(command, **kwargs):
            events = [{"type": "thread.started", "thread_id": "main-A"},
                      {"type": "error", "message": CAPACITY}]
            if "child" in command:
                events[0]["thread_id"] = "child"
            if outcome == "capacity":
                events.append({"type": "turn.failed", "error": {"message": CAPACITY}})
            else:
                events.append({"type": "turn.completed"})
                Path(command[command.index("-o") + 1]).write_text("finished", encoding="utf-8")
            kwargs["stdout"].write(("\n".join(map(json.dumps, events)) + "\n").encode())
            return SimpleNamespace(returncode=1 if outcome == "capacity" else 0)
        return execute

    def test_native_retry_success_is_not_a_gateway_failure(self):
        self.put(parallel.message("main", "ordinary"))
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        with patch.object(gateway, "run_model_process", side_effect=self.native("success")) as model, \
                patch.object(gateway, "reply_complete") as reply:
            reply.return_value = None
            self.worker._process_batch(batch)
        model.assert_called_once()
        self.assertNotIn("timeout", model.call_args.kwargs)
        reply.assert_called_once()
        self.assertEqual("completed", self.store.state["messages"]["main"]["status"])
        self.assertFalse(self.store.state.get("lifecycle_notices"))

    def test_terminal_capacity_preserves_batch_retries_only_notice_and_unblocks_main(self):
        self.put(parallel.message("one", "first"), parallel.message("two", "second"))
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.put(parallel.message("next", "later"))
        with patch.object(gateway, "run_model_process", side_effect=self.native()) as model:
            self.worker._process_batch(batch)
        model.assert_called_once()
        for mid in ("one", "two"):
            row = self.store.state["messages"][mid]
            self.assertEqual("provider_failed", row["status"])
            self.assertEqual(1, row["attempts"])
            self.assertTrue(row["content"])
            self.assertTrue(Path(row["run_event_log"]).exists())
            self.assertFalse(row.get("working_reaction_active"))
        self.assertFalse(any(call.args[1] == gateway.COMPLETION_REACTION
                             for call in self.client.add_reaction.call_args_list))
        self.client.reply_post.side_effect = OSError("offline")
        self.worker.deliver_lifecycle_notices()
        notices = self.store.state["lifecycle_notices"]
        before = {key: value["uuid"] for key, value in notices.items()}
        self.assertEqual(2, len(before))
        self.store.recover_interrupted()
        restored = gateway.ProjectStore("test")
        self.assertEqual(before, {key: value["uuid"] for key, value in restored.state["lifecycle_notices"].items()})
        self.client.reply_post.side_effect = None
        self.client.reply_post.return_value = {"data": {"message_id": "synthetic-reply"}}
        for row in notices.values():
            row["retry_at"] = 0
        self.worker.deliver_lifecycle_notices()
        self.worker.deliver_lifecycle_notices()
        self.assertEqual(4, self.client.reply_post.call_count)  # two failures, two successes
        self.assertTrue(all(row["status"] == "delivered" for row in notices.values()))
        self.assertEqual(["next"], [row["message_id"] for row in self.store.next_pending_batch(quiet_window_seconds=0)])
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        snapshot = service.queue_snapshot(["test"])
        self.assertEqual(2, len(snapshot["terminal_failures"]))
        self.assertEqual(["next"], [row["message_id"] for row in snapshot["active"]])

    def test_capacity_branch_finishes_without_retry_or_parent_archive(self):
        self.put(parallel.message("main", "ordinary", status="processing"), parallel.message("side"))
        with patch.object(gateway, "fork_thread", return_value="child") as create, \
                patch.object(gateway, "run_model_process", side_effect=self.native()) as model:
            self.worker.dispatch_parallel()
            parallel.wait_until(self.idle)
            self.worker.dispatch_parallel()
        create.assert_called_once()
        model.assert_called_once()
        self.assertEqual("processing", self.store.state["messages"]["main"]["status"])
        self.assertEqual("provider_failed", self.store.state["messages"]["side"]["status"])
        self.assertEqual("main-A", self.store.thread_id())
        with patch.object(gateway, "archive_thread") as archive:
            self.worker.reconcile_branch_archives()
        self.assertEqual("child", archive.call_args.args[0])

    def test_capacity_cannot_overwrite_newer_or_foreign_run(self):
        row = parallel.message("one", "ordinary", status="processing")
        row["run_event_log"] = "newer-log"
        self.put(row)
        with self.assertRaises(gateway.GatewayError):
            self.store.fail_provider_capacity(["one"], Path("older-log"), "notice")
        self.assertEqual("processing", self.store.state["messages"]["one"]["status"])
        self.assertFalse(self.store.state.get("lifecycle_notices"))

    def test_first_main_capacity_keeps_native_thread_receipt(self):
        self.store.set_thread_id("")
        self.put(parallel.message("one", "ordinary"))
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        with patch.object(gateway, "run_model_process", side_effect=self.native()):
            self.worker._process_batch(batch)
        self.assertEqual("main-A", self.store.thread_id())

    def test_terminal_capacity_is_not_hidden_by_sync_or_inspection(self):
        row = parallel.message("one", "ordinary", status="provider_failed")
        row["error"] = "Selected model is at capacity"
        self.put(row)
        config = {"app_id": "synthetic-app", "expected_tenant_key": "synthetic-tenant",
                  "projects": {"test": self.project}}
        responses = self.root / "responses"
        responses.mkdir()
        (responses / "fixed.json").write_text('{"ok":true}', encoding="utf-8")
        args = SimpleNamespace(project_key="test", working_directory=None, ack_timeout=0.1,
                               wait_timeout=1, request_only=False, report_usage=False)
        with patch.object(gateway, "load_config", return_value=config), \
                patch.object(gateway, "SYNC_REQUESTS", self.root / "requests"), \
                patch.object(gateway, "SYNC_RESPONSES", responses), \
                patch.object(gateway.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")), \
                patch.object(gateway, "send_due_reminders", create=True, return_value={}), \
                self.assertRaisesRegex(gateway.GatewayError, "terminal failures.*capacity"):
            gateway.request_sync(args)
        if hasattr(gateway, "routine_inspection_text"):
            with patch.object(gateway, "codex_rate_limits", side_effect=OSError("offline")):
                self.assertIn("failed 1", gateway.routine_inspection_text("test", "en"))
                self.assertIn("失败 1", gateway.routine_inspection_text("test"))

    def test_known_history_rejection_recovers_and_delivers_while_main_busy(self):
        source = self.root / "source.jsonl"
        original = b'{"type":"session_meta","payload":{"id":"main-A","history_mode":"paginated"}}\n'
        original += b'{"type":"response_item","payload":{"text":"synthetic saved context"}}\n'
        source.write_bytes(original)
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES(?,?)", ("main-A", str(source)))
            db.commit()
        self.put(parallel.message("main", "ordinary"))
        main_batch = self.store.next_pending_batch(quiet_window_seconds=0)
        main_started = threading.Event()
        native = self.native("success")
        def model(command, **kwargs):
            if kwargs["message_ids"] == ["main"]:
                main_started.set()
                if not self.release.wait(5):
                    raise AssertionError("main test was not released")
            return native(command, **kwargs)
        imports = []
        def rpc(method, params, *_args, **_kwargs):
            if "path" not in params:
                raise fork.ThreadForkRPCError("thread/fork", -32603, projection_ordinals=(7, 6))
            imports.append(Path(params["path"]))
            self.assertEqual(original.split(b"\n", 1)[1], imports[-1].read_bytes().split(b"\n", 1)[1])
            return "child"
        with patch.object(fork, "codex_home", return_value=self.root), \
                patch.object(fork, "snapshot_rollout", side_effect=lambda parent, dest: snapshot_rollout(parent, dest, home=self.root)), \
                patch.object(fork, "_thread_rpc", side_effect=rpc) as requests, \
                patch.object(gateway, "fork_thread", side_effect=fork.fork_thread), \
                patch.object(gateway, "run_model_process", side_effect=model), \
                patch.object(gateway, "reply_complete", return_value=None) as reply:
            main = threading.Thread(target=self.worker._process_batch, args=(main_batch,))
            main.start()
            try:
                self.assertTrue(main_started.wait(2))
                self.put(parallel.message("side"))
                self.worker.dispatch_parallel()
                parallel.wait_until(self.idle)
                self.assertTrue(main.is_alive())
                self.assertEqual("processing", self.store.state["messages"]["main"]["status"])
                reply.assert_called_once()
                self.assertEqual("side", reply.call_args.args[2]["message_id"])
            finally:
                self.release.set()
                main.join(3)
            self.assertFalse(main.is_alive())
        self.assertEqual(2, requests.call_count)
        self.assertEqual(2, reply.call_count)
        self.assertEqual("completed", self.store.state["messages"]["side"]["status"])
        self.assertEqual("completed", self.store.state["messages"]["main"]["status"])
        self.assertEqual("main-A", self.store.thread_id())
        self.assertFalse(imports[0].exists())
        self.assertEqual(original, source.read_bytes())
        with patch.object(gateway, "archive_thread") as archive:
            self.worker.reconcile_branch_archives()
        self.assertEqual("child", archive.call_args.args[0])


class FailureSignatureTests(unittest.TestCase):
    def test_only_latest_terminal_event_classifies_capacity(self):
        failed = {"type": "turn.failed", "error": {"message": CAPACITY}}
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "events.jsonl"
            for records, expected in [
                ([{"type": "error", "message": CAPACITY}], False),
                ([{"type": "item.completed", "item": {"type": "agent_message", "text": CAPACITY}}], False),
                ([failed], True),
                ([failed, {"type": "turn.completed"}], False),
                ([failed, {"type": "turn.failed", "error": {"message": "other error"}}], False),
                ([None, [], failed], True),
            ]:
                with self.subTest(records=records):
                    log.write_text("invalid\n" + "\n".join(map(json.dumps, records)), encoding="utf-8")
                    self.assertEqual(expected, terminal_capacity_failure(log))

    def test_rpc_preserves_only_exact_pre_creation_failure_signature(self):
        signature = ("failed to prepare paginated fork: thread history projection for "
                     "11111111-1111-1111-1111-111111111111 expected ordinal 7, got 6")
        for text, expected in [(signature, (7, 6)), ("secret " + signature, None), (signature + " secret", None)]:
            with self.subTest(text=text):
                messages = queue.Queue()
                messages.put(("message", {"id": 1, "error": {"code": -32603, "message": text, "data": "private"}}))
                with self.assertRaises(fork.ThreadForkRPCError) as caught:
                    fork._response(messages, 1, "thread/fork", time.monotonic() + 1)
                self.assertEqual(expected, caught.exception.projection_ordinals)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("11111111", str(caught.exception))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / "source.jsonl"
        self.meta = {"type": "session_meta", "payload": {"id": "parent", "history_mode": "paginated"}}
        self.original = (json.dumps(self.meta) + '\n{"type":"response_item","payload":{"text":"synthetic"}}\n').encode()
        self.parent.write_bytes(self.original)
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES(?,?)", ("parent", str(self.parent)))
            db.commit()

    def snapshot(self, *_):
        return snapshot_rollout("parent", self.root / "imports", home=self.root)

    def test_partial_tail_excluded_and_concurrent_copies_independent(self):
        self.parent.write_bytes(self.original + b'{"partial":')
        one, two = self.snapshot(), self.snapshot()
        self.assertNotEqual(one, two)
        self.assertEqual(one.read_bytes(), two.read_bytes())
        self.assertEqual("legacy", json.loads(one.read_bytes().split(b"\n")[0])["payload"]["history_mode"])
        self.assertEqual(self.original.split(b"\n", 1)[1], one.read_bytes().split(b"\n", 1)[1])
        one.unlink()
        self.assertTrue(two.exists())
        self.assertEqual(self.original + b'{"partial":', self.parent.read_bytes())

    def test_invalid_identity_corrupt_or_referenced_history_refused(self):
        for value in [b'[]\n', b'null\n', b'{"type":"session_meta","payload":null}\n',
                      self.original + b'invalid\n', self.original.replace(b'parent', b'wrong'),
                      self.original.replace(b'"paginated"', b'"paginated", "history_base": "base"')]:
            with self.subTest(value=value):
                self.parent.write_bytes(value)
                with self.assertRaises(ValueError):
                    self.snapshot()
                self.assertFalse((self.root / "imports").exists())

    def test_unknown_transport_or_unrelated_rpc_never_creates_second_child(self):
        for error in [fork.ThreadForkEOFError("unknown"), fork.ThreadForkTimeoutError("unknown"),
                      fork.ThreadForkRPCError("thread/fork", -32603),
                      fork.ThreadForkRPCError("thread/start", -32603, projection_ordinals=(7, 6))]:
            with self.subTest(error=error), patch.object(fork, "_thread_rpc", side_effect=error) as rpc, \
                    patch.object(fork, "snapshot_rollout") as copy:
                with self.assertRaises(fork.ThreadForkError):
                    fork.fork_thread("parent", "fake-codex", self.root)
                self.assertEqual(1, rpc.call_count)
                copy.assert_not_called()

    def test_ambiguous_import_kept_and_never_repeated(self):
        failure = fork.ThreadForkTimeoutError("unknown")
        failure.mutation_submitted = True
        with patch.object(fork, "_thread_rpc", side_effect=[
                fork.ThreadForkRPCError("thread/fork", -32603, projection_ordinals=(7, 6)), failure]) as rpc, \
                patch.object(fork, "snapshot_rollout", side_effect=self.snapshot):
            with self.assertRaises(fork.ThreadForkTimeoutError) as caught:
                fork.fork_thread("parent", "fake-codex", self.root)
        self.assertTrue(caught.exception.mutation_submitted)
        self.assertEqual(2, rpc.call_count)
        self.assertEqual(1, len(list((self.root / "imports").glob("*.jsonl"))))

    def test_custom_codex_home_is_respected(self):
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            self.assertEqual(self.root.resolve(), codex_home())


if __name__ == "__main__":
    unittest.main()
