"""Provider outage recovery through the real worker/store; no model or network."""
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import remote_gateway as gateway
import test_parallel_hash as parallel


class ProviderWorkerRecoveryTests(unittest.TestCase):
    setUp = parallel.ParallelHashTests.setUp
    finish_branches = parallel.ParallelHashTests.finish_branches
    put = parallel.ParallelHashTests.put
    idle = parallel.ParallelHashTests.idle

    def service(self):
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.workers = {"test": self.worker}
        service.client = self.client
        service.log_path = self.root / "service.log"
        service.reconnect_signal = threading.Event()
        self.client.list_messages.return_value = []
        return service

    def native(self, failure=None, saved=None):
        """Emit the exact native JSONL shape while keeping partial local output."""
        def execute(command, **kwargs):
            final_path = Path(command[command.index("-o") + 1])
            thread_id = "child" if "child" in command else "main-A"
            final_path.write_text("saved partial result" if failure else "completed result", encoding="utf-8")
            events = [{"type": "thread.started", "thread_id": thread_id},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": "saved output"}}]
            if failure:
                events.append({"type": "turn.failed", "error": {"message": failure}})
            else:
                events.append({"type": "turn.completed"})
            kwargs["stdout"].write(("\n".join(map(json.dumps, events)) + "\n").encode("utf-8"))
            if saved is not None:
                saved.append((final_path, kwargs["input"], list(kwargs["message_ids"])))
            return SimpleNamespace(returncode=1 if failure else 0)
        return execute

    def test_transport_and_routing_main_wait_preserves_context_and_partial_then_normal_catchup_recovers(self):
        attachment = self.root / "source.pdf"
        attachment.write_bytes(b"synthetic original attachment")
        digest = hashlib.sha256(attachment.read_bytes()).digest()
        for index, (message, kind) in enumerate((
            ("stream disconnected before completion", "transport"),
            ("workspace routing discovery timed out", "routing"),
        )):
            with self.subTest(kind=kind):
                mid = "source-" + str(index)
                row = parallel.message(mid, "original request with an attachment")
                source_content = row["content"]
                self.put(row)
                batch = self.store.next_pending_batch(quiet_window_seconds=0)
                saved = []
                with patch.object(self.worker, "_download_attachments", return_value=[attachment]), \
                        patch.object(gateway, "run_model_process", side_effect=self.native(message, saved)) as model:
                    self.worker._process_batch(batch)
                model.assert_called_once()
                row = self.store.state["messages"][mid]
                self.assertEqual(("pending", kind, 0), (row["status"], row["provider_wait"], row["attempts"]))
                self.assertEqual("main-A", self.store.thread_id())
                self.assertEqual(source_content, row["content"])
                self.assertEqual(digest, hashlib.sha256(attachment.read_bytes()).digest())
                partial, prompt, ids = saved[0]
                self.assertEqual([mid], ids)
                self.assertIn(str(attachment), prompt.decode("utf-8"))
                self.assertEqual("saved partial result", partial.read_text(encoding="utf-8"))
                self.assertTrue(Path(row["run_event_log"]).is_file())
                self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))

                service = self.service()
                self.assertEqual({"test": 0}, service.catch_up(["test"]))
                self.assertNotIn("provider_wait", row)
                retry = self.store.next_pending_batch(quiet_window_seconds=0)
                self.assertEqual([mid], [item["message_id"] for item in retry])
                with patch.object(self.worker, "_download_attachments", return_value=[attachment]), \
                        patch.object(gateway, "run_model_process", side_effect=self.native()) as resumed, \
                        patch.object(gateway, "reply_complete", return_value=""):
                    self.worker._process_batch(retry)
                resumed.assert_called_once()
                self.assertEqual("completed", row["status"])
                self.assertEqual(1, row["attempts"])
                self.assertEqual("saved partial result", partial.read_text(encoding="utf-8"))

    def test_existing_capacity_notice_survives_upgrade_without_payload_conflict(self):
        for language in ("zh-CN", "en"):
            for kind, native_error in (("at_capacity", "selected model is at capacity"),
                                       ("rate_limit", "rate limit exceeded")):
                with self.subTest(language=language, kind=kind):
                    self.worker.project["language"] = language
                    mid = language + kind
                    self.put(parallel.message(mid, "original request"))
                    detail = "rate limit" if kind == "rate_limit" else "at capacity"
                    old_text = (
                        f"Codex is unavailable ({detail}). Your request is retained; the next normal catch-up will retry it. No resend is needed."
                        if language == "en" else
                        f"Codex 暂不可用（{detail}），消息仍待处理，下次正常巡查会再尝试，无需重发。"
                    )
                    key = "provider:" + kind + ":" + mid
                    old = self.store._queue_lifecycle_notice(key, old_text, mid)
                    old["status"] = "delivered"
                    old_uuid = old["uuid"]
                    self.store.save()
                    batch = self.store.next_pending_batch(quiet_window_seconds=0)
                    with patch.object(gateway, "run_model_process", side_effect=self.native(native_error)):
                        self.worker._process_batch(batch)
                    row = self.store.state["messages"][mid]
                    self.assertEqual(("pending", kind, 0), (row["status"], row["provider_wait"], row["attempts"]))
                    notice = self.store.state["lifecycle_notices"][key]
                    self.assertEqual((old_uuid, old_text, "delivered"),
                                     (notice["uuid"], notice["text"], notice["status"]))
                    # End this synthetic scenario so the next outage owns its own queue head.
                    self.store.finish(mid, "ignored")

    def test_network_failure_preserves_whole_batch_and_existing_failure_budget(self):
        first = parallel.message("first", "first source")
        second = parallel.message("second", "second source")
        first["attempts"] = second["attempts"] = 2
        self.put(first, second)
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        with patch.object(gateway, "run_model_process", side_effect=self.native("connection reset by peer")) as model:
            self.worker._process_batch(batch)
        model.assert_called_once()
        rows = [self.store.state["messages"][mid] for mid in ("first", "second")]
        self.assertEqual({"pending"}, {row["status"] for row in rows})
        self.assertEqual({"transport"}, {row["provider_wait"] for row in rows})
        self.assertEqual({2}, {row["attempts"] for row in rows})
        self.assertEqual(1, len({row["run_event_log"] for row in rows}))
        self.assertTrue(all(row["content"] for row in rows))
        self.assertFalse(any(key.startswith("task-failed:") for key in self.store.state.get("lifecycle_notices", {})))
        self.assertFalse(any(call.args[1] == gateway.COMPLETION_REACTION
                             for call in self.client.add_reaction.call_args_list))

    def test_routing_branch_retains_child_and_never_replays_or_archives_busy_main(self):
        self.put(parallel.message("main", "busy main", status="processing"), parallel.message("side", "# branch request"))
        with patch.object(gateway, "fork_thread", return_value="child") as fork:
            with patch.object(gateway, "run_model_process", side_effect=self.native("workspace routing discovery failed")) as model:
                self.assertEqual(["side"], self.worker.dispatch_parallel())
                parallel.wait_until(self.idle)
            model.assert_called_once()
            branch = self.store.state["messages"]["side"]
            self.assertEqual(("pending", "routing", 0), (branch["status"], branch["provider_wait"], branch["attempts"]))
            self.assertEqual("child", branch["branch_thread_id"])
            self.assertEqual("main-A", branch["branch_parent_thread_id"])
            self.assertEqual([], self.worker.dispatch_parallel())
            self.assertEqual("processing", self.store.state["messages"]["main"]["status"])
            with patch.object(gateway, "archive_thread") as archive:
                self.worker.reconcile_branch_archives()
            archive.assert_not_called()
            self.service().catch_up(["test"])
            with patch.object(gateway, "run_model_process", side_effect=self.native()) as resumed, \
                    patch.object(gateway, "reply_complete", return_value=""):
                self.assertEqual(["side"], self.worker.dispatch_parallel())
                parallel.wait_until(self.idle)
            resumed.assert_called_once()
            fork.assert_called_once()
        self.assertEqual("completed", branch["status"])
        self.assertEqual("processing", self.store.state["messages"]["main"]["status"])
        self.assertEqual("main-A", self.store.thread_id())

    def test_failed_normal_catchup_keeps_network_wait_without_an_extra_model_call(self):
        self.put(parallel.message("source", "original request"))
        with patch.object(gateway, "run_model_process", side_effect=self.native("stream disconnected")) as model:
            self.worker._process_batch(self.store.next_pending_batch(quiet_window_seconds=0))
            service = self.service()
            self.client.list_messages.side_effect = OSError("synthetic history service offline")
            with self.assertRaises(gateway.GatewayError):
                service.catch_up(["test"])
            self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
            model.assert_called_once()
        row = self.store.state["messages"]["source"]
        self.assertEqual(("pending", "transport", 0), (row["status"], row["provider_wait"], row["attempts"]))

    def test_authentication_uses_existing_failure_budget_and_safe_terminal_reason(self):
        self.put(parallel.message("source", "original request"))
        failure = "unexpected status 401 Unauthorized: synthetic-secret, url: https://synthetic.invalid/token"
        partials = []
        with patch.object(gateway, "run_model_process", side_effect=self.native(failure, partials)) as model:
            for attempt in (1, 2, 3):
                batch = self.store.next_pending_batch(quiet_window_seconds=0)
                self.assertEqual(["source"], [row["message_id"] for row in batch])
                self.worker._process_batch(batch)
                row = self.store.state["messages"]["source"]
                self.assertEqual("failed", row["status"])
                self.assertEqual(attempt, row["attempts"])
                self.assertFalse(row.get("provider_wait"))
                self.assertNotIn("synthetic-secret", row["error"])
            self.assertEqual(3, model.call_count)
            self.service().catch_up(["test"])
            self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
            self.assertEqual(3, model.call_count)
        terminal = [row["text"] for key, row in self.store.state.get("lifecycle_notices", {}).items()
                    if key.startswith("task-failed:")]
        self.assertEqual(1, len(terminal))
        self.assertIn("认证", terminal[0])
        self.assertIn("可能", terminal[0])
        self.assertNotIn("synthetic-secret", terminal[0])
        self.assertNotIn("https://", terminal[0])
        self.assertEqual("main-A", self.store.thread_id())
        self.assertEqual(3, len(partials))
        self.assertTrue(all(path.read_text(encoding="utf-8") == "saved partial result" for path, _prompt, _ids in partials))
        self.assertFalse(any(key.startswith("provider:authentication:")
                             for key in self.store.state.get("lifecycle_notices", {})))


if __name__ == "__main__":
    unittest.main()
