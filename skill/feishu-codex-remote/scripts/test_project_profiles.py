"""Provisioning inheritance and native provider recovery, without real accounts."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import project_profiles as profiles
import remote_gateway as gateway
from provider_failure import terminal_provider_failure
import test_gateway_lifecycle as lifecycle_fixture


class ProfileTests(unittest.TestCase):
    def test_retry_prompt_keeps_exact_feedback_from_each_batch_member(self):
        rows = [{"message_id": mid, "content": json.dumps({"text": "task " + mid}),
                 "error": error, "run_event_log": mid + ".jsonl"}
                for mid, error in [("first", "missing evidence"), ("last", "delivery interrupted")]]
        combined = gateway.combined_batch_item(rows)
        prompt = gateway.build_prompt("test", {"working_directory": "."}, combined, [], first_turn=False)
        self.assertEqual(combined["message_id"], "last")
        self.assertEqual(combined["batch_message_ids"], ["first", "last"])
        for expected in ("missing evidence", "delivery interrupted", "first.jsonl", "last.jsonl"):
            self.assertIn(expected, prompt)

    def test_snapshot_preserves_independent_profiles(self):
        source = dict(model="source-model", reasoning_effort="high",
                      service_tier="priority", permission_mode="auto-review")
        result = profiles.initial_profiles({}, source, {})
        self.assertEqual(result["agent_model"], "source-model")
        self.assertEqual(result["agent_service_tier"], "priority")
        self.assertEqual(result["agent_permission_mode"], "auto-review")
        self.assertEqual({k.removeprefix("patrol_"): v for k, v in result.items() if k.startswith("patrol_")}, profiles.PATROL_DEFAULTS)
        source.update(model="changed", service_tier="default")
        self.assertEqual(profiles.initial_profiles(result, source, {}), result)
        result["patrol_model"] = "custom-patrol"
        self.assertEqual(profiles.initial_profiles(result, source, {})["patrol_model"], "custom-patrol")

    def test_missing_source_is_not_silently_replaced_by_patrol(self):
        with self.assertRaisesRegex(gateway.GatewayError, "model.*reasoning_effort.*service_tier.*permission_mode"):
            profiles.initial_profiles({}, {}, {})
        with self.assertRaisesRegex(gateway.GatewayError, "service_tier"):
            profiles.initial_profiles({}, dict(model="source", reasoning_effort="high", permission_mode="full-access"), {})

    def test_read_exact_source_and_last_turn_without_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            log = root / "session.jsonl"
            events = [{"type": "turn_context", "payload": {"model": "old", "effort": "low"}},
                      {"type": "turn_context", "payload": {
                          "model": "new", "effort": "medium", "service_tier": "default",
                          "sandbox_policy": {"type": "danger-full-access"}, "approval_policy": "never"}}]
            log.write_text('\n'.join(map(json.dumps, events)), encoding='utf-8')
            dbpath = root / "state.sqlite"
            with closing(sqlite3.connect(dbpath)) as db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT)")
                db.execute("INSERT INTO threads VALUES (?,?,?)", ("source", str(log), str(root)))
                db.commit()
            before = (dbpath.read_bytes(), log.read_bytes())
            self.assertEqual(profiles.source_task_profile("source", root, database=dbpath), dict(
                model="new", reasoning_effort="medium", service_tier="default", permission_mode="full-access"))
            with self.assertRaises(gateway.GatewayError):
                profiles.source_task_profile("source", root / "other", database=dbpath)
            self.assertEqual(before, (dbpath.read_bytes(), log.read_bytes()))

    def test_new_patrol_settings_do_not_claim_unsupported_fields_applied(self):
        spec = gateway.patrol_creation_settings({"agent_model": "source", "agent_service_tier": "priority"})
        self.assertEqual(spec["requested_profile"], profiles.PATROL_DEFAULTS)
        self.assertFalse(spec["applied"])
        self.assertEqual(set(spec["requires_client_verification"]), {"service_tier", "permission_mode"})

    def test_rate_limit_is_distinct_from_capacity_and_native_success_wins(self):
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "events.jsonl"
            event = {"type": "turn.failed", "error": {"code": "usage_limit_reached", "message": "usage limit"}}
            log.write_text(json.dumps(event), encoding='utf-8')
            self.assertEqual(terminal_provider_failure(log), "rate_limit")
            log.write_text(json.dumps(event) + '\n' + json.dumps({"type": "turn.completed"}), encoding='utf-8')
            self.assertIsNone(terminal_provider_failure(log))


class ResumeTests(unittest.TestCase):
    setUp = lifecycle_fixture.LifecycleTests.setUp
    enqueue = lifecycle_fixture.LifecycleTests.enqueue
    enter = lifecycle_fixture.LifecycleTests.enter

    def test_quota_pending_survives_restart_and_empty_catchup_wakes_it(self):
        self.enqueue()
        batch = self.store.next_pending_batch(quiet_window_seconds=0)
        self.store.update_message("one", run_event_log="owned")
        self.store.fail_provider_capacity(["one"], "owned", "quota unavailable", "rate_limit")
        self.assertEqual([], self.store.next_pending_batch(quiet_window_seconds=0))
        self.assertEqual(0, self.store.state["messages"]["one"]["attempts"])
        restarted = gateway.ProjectStore("test")
        self.assertEqual("rate_limit", restarted.state["messages"]["one"]["provider_wait"])
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        import threading
        service.reconcile_lock = threading.Lock()
        service.workers = {"test": self.worker}
        service.client = self.client
        service.log_path = self.root / "service.log"
        self.client.list_messages.return_value = []
        service.catch_up(["test"])
        self.assertTrue(self.worker.signal.is_set())
        self.assertTrue(self.worker.branch_signal.is_set())
        self.assertEqual(["one"], [r["message_id"] for r in self.store.next_pending_batch(quiet_window_seconds=0)])

    def test_exit_keeps_true_failure_and_completion_but_resumes_maintenance_pending(self):
        self.enqueue("old")
        self.store.finish("old", "failed", attempts=3)
        self.enqueue("done")
        self.store.finish("done", "completed")
        self.enter()
        self.enqueue("new")
        self.worker.maintenance_action("exit", "exit")
        self.assertEqual("failed", self.store.state["messages"]["old"]["status"])
        self.assertEqual("completed", self.store.state["messages"]["done"]["status"])
        self.assertEqual(["new"], [r["message_id"] for r in self.store.next_pending_batch(quiet_window_seconds=0)])
        self.worker.deliver_lifecycle_notices()
        self.client.reply_post.assert_not_called()

if __name__ == '__main__':
    unittest.main()
