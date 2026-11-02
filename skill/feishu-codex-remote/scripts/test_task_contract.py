"""Fault-injection acceptance with no live gateway, credentials or model calls."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import unittest

import gateway_lifecycle as lifecycle
import remote_gateway as gateway
from task_contract import restore_result, prepared_result
import test_gateway_lifecycle as fixtures

class TransactionTests(unittest.TestCase):
    setUp = fixtures.LifecycleTests.setUp
    enqueue = fixtures.LifecycleTests.enqueue
    enter = fixtures.LifecycleTests.enter
    acceptance = fixtures.LifecycleTests.acceptance
    def final(self, text="Completed"):
        path = self.root / "run" / "final.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def process(self, batch, path, delivery=None):
        with patch.object(gateway, "run_codex", return_value=(path.read_text(encoding="utf-8"), "main", path)) as run, \
             patch.object(gateway, "run_codex_batch", return_value=(path.read_text(encoding="utf-8"), "main", path)) as runs, \
             patch.object(gateway, "reply_complete", side_effect=delivery, return_value="") as send:
            self.worker._process_batch(batch)
            return run.call_count + runs.call_count, send.call_count

    def test_delivery_retry_reuses_output_after_restart_without_model(self):
        self.enqueue()
        path = self.final()
        first = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual((1, 1), self.process(first, path, RuntimeError("offline")))
        self.worker = gateway.ProjectWorker("test", self.project, self.client, self.root / "log")
        self.store = self.worker.store
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual((0, 1), self.process(batch, path))
        self.assertEqual("completed", self.store.state["messages"]["one"]["status"])

    def test_result_survives_metadata_commit_fault_without_model_replay(self):
        self.enqueue()
        path = self.final('Already generated reply')
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        produced, faulted = [False], [False]
        real_save = self.store.save
        def produce(*args):
            produced[0] = True
            return path.read_text(encoding='utf-8'), 'main', path
        def save():
            if produced[0] and not faulted[0]:
                faulted[0] = True
                raise OSError('synthetic metadata commit interrupted after model reply')
            return real_save()
        with patch.object(gateway, 'run_codex', side_effect=produce) as model, \
                patch.object(self.store, 'save', side_effect=save):
            self.worker._process_batch(batch)
        self.assertEqual(1, model.call_count)
        self.worker = gateway.ProjectWorker('test', self.project, self.client, self.root / 'log')
        self.store = self.worker.store
        self.assertEqual((0,1), self.process(self.store.next_pending_batch(quiet_window_seconds=0), path))
        self.assertEqual('completed', self.store.state['messages']['one']['status'])

    def test_batch_delivery_preserves_last_anchor_and_members(self):
        self.enqueue("a")
        self.enqueue("b")
        path = self.final()
        first = self.store.next_pending_batch(quiet_window_seconds=0)
        self.process(first, path, RuntimeError("offline"))
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.assertEqual(["a", "b"], [x["message_id"] for x in batch])
        self.assertEqual((0, 1), self.process(batch, path))
        self.assertEqual("b", self.store.state["messages"]["a"]["reply_source_message_id"])

    def test_completed_state_survives_reaction_and_late_failures(self):
        self.enqueue()
        with patch.object(self.worker, "_replace_working_with_completion", side_effect=RuntimeError("reaction offline")):
            self.process(self.store.next_pending_batch(quiet_window_seconds=0), self.final())
        self.store.finish("one", "failed", error="late cleanup failed")
        self.assertEqual("completed", self.store.state["messages"]["one"]["status"])
        self.assertNotIn("error", self.store.state["messages"]["one"])

    def test_clarification_reply_delivers_once_despite_legacy_incomplete_receipt(self):
        self.enqueue()
        path = self.final("Please confirm which document you mean.")
        outcome = {"status": "incomplete", "failed": ["Missing clarification"]}
        sidecar = path.with_name("outcome.json")
        sidecar.write_text(json.dumps(outcome), encoding="utf-8")
        self.assertEqual((1, 1), self.process(self.store.next_pending_batch(quiet_window_seconds=0), path))
        self.assertEqual("completed", self.store.state["messages"]["one"]["status"])
        self.assertEqual(1, self.store.state["messages"]["one"]["attempts"])
        self.assertEqual(outcome, json.loads(sidecar.read_text(encoding="utf-8")))
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.assertFalse(self.store.state.get("lifecycle_notices"))


    def test_terminal_failure_notice_retries_without_business_call(self):
        self.enqueue()
        self.store.finish("one", "pending", attempts=2)
        path = self.final()
        self.process(self.store.next_pending_batch(quiet_window_seconds=0), path, RuntimeError("secret diagnostic"))
        self.client.reply_post.side_effect = RuntimeError("offline")
        self.worker.deliver_lifecycle_notices()
        first_uuid = self.client.reply_post.call_args.args[2]
        self.client.reply_post.side_effect = None
        with patch.object(lifecycle.time, "time", return_value=10**12):
            self.worker.deliver_lifecycle_notices()
        self.assertEqual(first_uuid, self.client.reply_post.call_args.args[2])
        self.assertNotIn("secret diagnostic", self.client.reply_post.call_args.args[1])
        self.assertEqual("failed", self.store.state["messages"]["one"]["status"])

    def test_notice_without_remote_receipt_remains_pending(self):
        self.store._queue_lifecycle_notice("test", "notice", "one")
        self.store.save()
        self.client.reply_post.return_value = {}
        self.worker.deliver_lifecycle_notices()
        self.assertEqual("pending", self.store.state["lifecycle_notices"]["test"]["status"])

    def test_integrity_mismatch_does_not_trigger_business_rerun(self):
        path = self.final()
        saved = prepared_result("Completed", "main", path, ["one"])
        path.write_text("changed", encoding="utf-8")
        with self.assertRaises(gateway.GatewayError):
            restore_result(saved, ["one"])
        with self.assertRaises(gateway.GatewayError):
            restore_result(saved, ["other"])

    def test_acceptance_rejects_changed_artifact_or_unfinished_required_request(self):
        self.enter()
        self.store.exit_maintenance()
        path = self.acceptance()
        (self.root / "acceptance-test.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(gateway.GatewayError):
            self.store.queue_completion("release", "done", path)
        path = self.acceptance()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["required_message_ids"] = ["unfinished"]
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(gateway.GatewayError):
            self.store.queue_completion("release", "done", path)

    def test_acceptance_does_not_require_unrelated_backlog_to_be_empty(self):
        self.enter()
        self.enqueue()
        self.store.exit_maintenance()
        self.store.queue_completion("release", "done", self.acceptance())
        self.assertEqual("pending", self.store.state["messages"]["one"]["status"])
        self.assertEqual(["one"], [x["message_id"] for x in self.store.next_pending_batch(quiet_window_seconds=0)])

    def test_prompt_includes_contract_and_prior_outcome(self):
        self.enqueue()
        row = self.store.state["messages"]["one"]
        row["task_outcome"] = {"completed": ["verified-A"]}
        self.assertIn("verified-A", gateway.build_prompt("test", self.project, row, [], first_turn=False))

    def test_long_failure_delivery_resumes_unsent_parts_only(self):
        self.store._queue_lifecycle_notice("long", "x" * 7000, "one")
        self.store.save()
        self.client.reply_post.side_effect = [{"data": {"message_id": "first"}}, RuntimeError("offline")]
        self.worker.deliver_lifecycle_notices()
        failed_uuid = self.client.reply_post.call_args.args[2]
        self.client.reply_post.reset_mock()
        self.client.reply_post.side_effect = None
        with patch.object(lifecycle.time, "time", return_value=10**12):
            self.worker.deliver_lifecycle_notices()
        self.assertEqual(2, self.client.reply_post.call_count)
        self.assertEqual(failed_uuid, self.client.reply_post.call_args_list[0].args[2])
        self.assertEqual("delivered", self.store.state["lifecycle_notices"]["long"]["status"])

    def test_missing_acceptance_cannot_claim_repair_completion(self):
        with self.assertRaises(gateway.GatewayError):
            self.store.queue_completion("release", "done")
        self.assertEqual({}, self.store.state.get("lifecycle_notices", {}))

    def test_atomic_batch_completion_rolls_back_memory_on_write_error(self):
        self.enqueue("a")
        self.enqueue("b")
        self.store.next_pending_batch(quiet_window_seconds=0)
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.complete_batch(["a", "b"], completed_at="now")
        self.assertEqual(["processing", "processing"],
                         [self.store.state["messages"][mid]["status"] for mid in ["a", "b"]])

    def test_reply_with_literal_or_malformed_old_control_markup_is_delivered_unchanged(self):
        for index, text in enumerate((
                'Example: <codex-task-outcome> is ordinary text.',
                'Partial reply\n<codex-task-outcome>\n{"status":"incomplete"}\n</codex-task-outcome>',
                'Bad <codex-task-outcome> raw')):
            with self.subTest(text=text):
                mid = 'literal-' + str(index)
                self.enqueue(mid)
                path = self.final(text)
                self.assertEqual((1, 1), self.process(self.store.next_pending_batch(quiet_window_seconds=0), path))
                self.assertEqual(text, self.worker.store.state['messages'][mid]['prepared_reply']['answer'])
                self.assertEqual('completed', self.store.state['messages'][mid]['status'])


if __name__ == "__main__":
    unittest.main()
