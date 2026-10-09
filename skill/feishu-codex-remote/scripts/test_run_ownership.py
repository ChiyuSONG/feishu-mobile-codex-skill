"""Real hidden orphan-child recovery; synthetic work only, no model or network."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import gateway_lifecycle as lifecycle
import remote_gateway as gateway
import run_ownership as ownership


def hidden_options():
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def until(predicate):
    limit = time.monotonic() + 10
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("isolated process did not reach expected state")


def put(store, mid, text, created=100):
    store.enqueue({"message_id": mid, "chat_id": "test-chat", "sender": {"id": "user"},
                   "message_type": "text", "content": json.dumps({"text": text}),
                   "create_time": created})


def child_fixture(root, final_path, mode):
    root, final_path = Path(root), Path(final_path)
    sys.stdin.buffer.read()
    thread = "branch-child" if mode == "branch" else "main-original"
    print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
    print(json.dumps({"type": "turn.started"}), flush=True)
    if mode == "stderr":
        print("synthetic CLI stderr diagnostic", file=sys.stderr, flush=True)
    with (root / "effects.jsonl").open("a", encoding="utf-8") as output:
        output.write("synthetic-side-effect\n")
    (root / "child-ready").write_text(str(os.getpid()), encoding="utf-8")
    until(lambda: (root / "release-child").exists())
    if mode != "unknown":
        final_path.write_text("isolated retained answer", encoding="utf-8")
        print(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "isolated retained answer"}}), flush=True)
        print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
        if mode == "stderr":
            print("synthetic shutdown diagnostic", file=sys.stderr, flush=True)


def owner_fixture(root, mode):
    root = Path(root)
    gateway.REMOTE_STATE = root
    store = gateway.ProjectStore("test")
    store.set_thread_id("main-original")
    branch = mode == "branch"
    put(store, "one", "# synthetic task" if branch else "synthetic first")
    if branch:
        store.update_message("one", status="processing", attempts=1, branch_thread_id="branch-child",
                             branch_parent_thread_id="main-original", branch_state="ready")
        with store.lock:
            store.claim_run(["one"])
            store.save()
        rows = [dict(store.state["messages"]["one"])]
    else:
        if mode != "partial":
            put(store, "two", "synthetic final*", 200)
        rows = store.next_pending_batch(quiet_window_seconds=0)
    project = {"working_directory": str(root), "chat_id": "test-chat"}
    command = lambda _project, final, _attachments, _thread: [
        sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(root), str(final), mode]
    with patch.object(gateway, "build_codex_command", side_effect=command), \
            patch.object(gateway, "build_prompt", return_value="synthetic fixture"):
        gateway.run_codex_batch("test", project, store, rows, [])


class RunOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime = patch.object(gateway, "REMOTE_STATE", self.root)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.client = MagicMock()
        self.client.add_reaction.return_value = {"data": {"reaction_id": "synthetic-reaction"}}
        self.project = {"working_directory": str(self.root), "chat_id": "test-chat"}

    def worker(self):
        return gateway.ProjectWorker("test", self.project, self.client, self.root / "gateway.log")

    def orphan(self, mode):
        parent = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                   "--owner", str(self.root), mode],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **hidden_options())
        self.addCleanup(lambda: parent.stderr.close())
        def cleanup():
            (self.root / "release-child").touch()
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=10)
        self.addCleanup(cleanup)
        until(lambda: (self.root / "child-ready").exists() or parent.poll() is not None)
        if parent.poll() is not None:
            self.fail(parent.stderr.read().decode("utf-8", errors="replace"))
        saved = gateway.ProjectStore("test").state["messages"]["one"]["run_ownership"]
        self.assertEqual(saved["phase"], "running")
        self.assertEqual(ownership.identity_state(saved["child"]), "alive")
        parent.kill()  # Deliberately kill only our synthetic listener, not its child.
        parent.wait(timeout=10)
        self.assertEqual(ownership.identity_state(saved["child"]), "alive")
        return saved

    def release(self, record):
        (self.root / "release-child").touch()
        until(lambda: ownership.identity_state(record["child"]) in {"absent", "exited"})

    def test_main_child_survives_listener_and_completed_artifact_is_delivered_once(self):
        record = self.orphan("main")
        worker = self.worker()
        store = worker.store
        self.assertEqual(store.state["messages"]["one"]["run_recovery"], "waiting")
        self.assertEqual(store.next_pending_batch(quiet_window_seconds=0), [])
        self.assertEqual(store.state["messages"]["one"]["attempts"], 1)
        self.release(record)
        self.assertTrue(store.reconcile_runs())
        batch = store.next_pending_batch()
        self.assertEqual([row["message_id"] for row in batch], ["one", "two"])
        with patch.object(gateway, "run_codex", side_effect=AssertionError("duplicate model")), \
                patch.object(gateway, "run_codex_batch", side_effect=AssertionError("duplicate model")), \
                patch.object(gateway, "reply_complete", return_value="") as send:
            worker._process_batch(batch)
            self.assertEqual(store.next_pending_batch(quiet_window_seconds=0), [])
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2]["message_id"], "two")
        self.assertEqual(store.thread_id(), "main-original")
        for mid in ("one", "two"):
            self.assertEqual(store.state["messages"][mid]["status"], "completed")
            self.assertEqual(store.state["messages"][mid]["attempts"], 1)
        self.assertEqual((self.root / "effects.jsonl").read_text().splitlines(), ["synthetic-side-effect"])

    def test_branch_child_survives_and_reuses_original_branch_without_refork(self):
        record = self.orphan("branch")
        worker = self.worker()
        self.assertEqual(worker.dispatch_parallel(), [])
        self.release(record)
        worker.store.reconcile_runs()
        with patch.object(gateway, "fork_thread", side_effect=AssertionError("duplicate fork")) as fork, \
                patch.object(gateway, "run_codex", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", return_value="") as send:
            self.assertEqual(worker.dispatch_parallel(), ["one"])
            until(lambda: not worker.active_branches)
            self.assertEqual(worker.dispatch_parallel(), [])
        fork.assert_not_called()
        model.assert_not_called()
        send.assert_called_once()
        self.assertEqual(worker.store.thread_id(), "main-original")
        row = worker.store.state["messages"]["one"]
        self.assertEqual(row["branch_thread_id"], "branch-child")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual((self.root / "effects.jsonl").read_text().splitlines(), ["synthetic-side-effect"])

    def test_dead_child_without_terminal_artifact_remains_unknown_without_replay(self):
        record = self.orphan("unknown")
        worker = self.worker()
        self.release(record)
        worker.store.reconcile_runs()
        self.assertEqual(worker.store.state["messages"]["one"]["run_recovery"], "outcome_unknown")
        self.assertEqual(worker.store.next_pending_batch(quiet_window_seconds=0), [])
        self.assertEqual((self.root / "effects.jsonl").read_text().splitlines(), ["synthetic-side-effect"])

    def test_real_stderr_diagnostics_do_not_hide_complete_native_result(self):
        record = self.orphan("stderr")
        worker = self.worker()
        self.release(record)
        worker.store.reconcile_runs()
        self.assertEqual(worker.store.state["messages"]["one"]["run_recovery"], "ready")
        self.assertIn("synthetic CLI stderr", Path(record["event_log"]).read_text())
        with patch.object(gateway, "run_codex_batch", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", return_value="") as send:
            worker._process_batch(worker.store.next_pending_batch())
        model.assert_not_called()
        send.assert_called_once()

    def test_native_completion_still_runs_existing_outcome_review_before_delivery(self):
        record = self.orphan("partial")
        worker = self.worker()
        self.release(record)
        gateway.outcome_path(Path(record["final_path"])).write_text(
            json.dumps({"status": "incomplete", "failed": ["unfinished synthetic check"]}), encoding="utf-8")
        worker.store.reconcile_runs()
        batch = worker.store.next_pending_batch()
        self.assertEqual(len(batch), 1)
        with patch.object(gateway, "run_codex", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", side_effect=AssertionError("unreviewed delivery")) as send:
            worker._process_batch(batch)
        model.assert_not_called()
        send.assert_not_called()
        self.assertEqual(worker.store.state["messages"]["one"]["status"], "failed")
        self.assertEqual(worker.store.state["messages"]["one"]["task_outcome"]["status"], "incomplete")
        self.assertNotIn("prepared_reply", worker.store.state["messages"]["one"])
        self.assertNotIn("recovered_model_result", worker.store.state["messages"]["one"])
        original_final = Path(record["final_path"])
        self.assertTrue(original_final.exists())
        final = self.root / "continued-run" / "final.md"
        final.parent.mkdir()
        final.write_text("finished remaining work", encoding="utf-8")

        def continue_model(key, project, store, item, attachments):
            self.assertEqual(item["task_outcome"]["status"], "incomplete")
            prompt = gateway.build_prompt(key, project, item, attachments, first_turn=False)
            self.assertIn("unfinished synthetic check", prompt)
            self.assertIn(json.dumps(str(original_final)), prompt)
            return "finished remaining work", "main-original", final

        with patch.object(gateway, "run_codex", side_effect=continue_model) as model, \
                patch.object(gateway, "reply_complete", return_value="") as send:
            worker._process_batch(worker.store.next_pending_batch())
        model.assert_called_once()
        self.assertEqual(worker.store.state["messages"]["one"]["status"], "completed",
                         worker.store.state["messages"]["one"].get("error"))
        send.assert_called_once()
        self.assertEqual(worker.store.state["messages"]["one"]["status"], "completed")
        self.assertEqual(worker.store.state["messages"]["one"]["attempts"], 2)

    def test_partial_native_terminal_and_wrong_thread_do_not_authorize_result(self):
        final = self.root / "final.md"
        final.write_text("retained", encoding="utf-8")
        event_log = self.root / "events.jsonl"
        record = {"event_log": str(event_log), "final_path": str(final),
                  "message_ids": ["one"], "thread_id": "expected"}
        for text in ('{"type":"thread.started","thread_id":"expected"}\n{"type":"turn.completed"',
                     '{"type":"thread.started","thread_id":"wrong"}\n{"type":"turn.completed"}\n'):
            with self.subTest(text=text):
                event_log.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    ownership.finished_result(record)

    def test_pid_reuse_is_not_mistaken_for_original_alive_child(self):
        store = gateway.ProjectStore("test")
        put(store, "one", "original")
        store.next_pending_batch(quiet_window_seconds=0)
        record = store.state["messages"]["one"]["run_ownership"]
        record.update(owner={"pid": 99999999, "created": "past", "platform": "win32"},
                      phase="running", child={**ownership.current_identity(), "created": "different-generation"})
        store.save()
        self.assertEqual(ownership.identity_state(record["child"]), "replaced")
        store.reconcile_runs()
        self.assertEqual(store.state["messages"]["one"]["run_recovery"], "outcome_unknown")
        self.assertEqual(store.next_pending_batch(quiet_window_seconds=0), [])

    def test_legacy_processing_without_identity_is_retained_as_unknown(self):
        store = gateway.ProjectStore("test")
        put(store, "one", "old task")
        store.update_message("one", status="processing", attempts=2, branch_thread_id="original")
        worker = self.worker()
        row = worker.store.state["messages"]["one"]
        self.assertEqual((row["status"], row["run_recovery"], row["attempts"]),
                         ("processing", "outcome_unknown", 2))
        self.assertEqual(row["branch_thread_id"], "original")
        self.assertEqual(worker.store.next_pending_batch(quiet_window_seconds=0), [])

    def test_unsubmitted_claim_resumes_same_attempt_after_owner_exit(self):
        store = gateway.ProjectStore("test")
        put(store, "one", "not submitted")
        store.next_pending_batch(quiet_window_seconds=0)
        record = store.state["messages"]["one"]["run_ownership"]
        record["owner"] = {"pid": 99999999, "created": "past", "platform": "win32"}
        store.save()
        store.recover_interrupted()
        self.assertEqual(store.state["messages"]["one"]["run_recovery"], "ready")
        self.assertEqual(len(store.next_pending_batch()), 1)
        self.assertEqual(store.state["messages"]["one"]["attempts"], 1)

    def test_launch_pending_without_child_identity_cannot_resume_unsubmitted(self):
        store = gateway.ProjectStore("test")
        put(store, "one", "possible side effects")
        store.next_pending_batch(quiet_window_seconds=0)
        store.begin_process(["one"])
        store.state["messages"]["one"]["run_ownership"]["owner"] = {
            "pid": 99999999, "created": "past", "platform": "win32"}
        store.save()
        store.recover_interrupted()
        self.assertEqual(store.state["messages"]["one"]["run_recovery"], "outcome_unknown")

    def test_legacy_prepared_result_can_finish_without_new_model_attempt(self):
        store = gateway.ProjectStore("test")
        put(store, "one", "old completed model")
        final = self.root / "final.md"
        final.write_text("retained", encoding="utf-8")
        receipt = gateway.prepared_result("retained", "main-original", final, ["one"])
        store.update_message("one", status="processing", attempts=3, prepared_reply=receipt)
        worker = self.worker()
        batch = worker.store.next_pending_batch()
        self.assertEqual(len(batch), 1)
        with patch.object(gateway, "run_codex", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", return_value=""):
            worker._process_batch(batch)
        model.assert_not_called()
        self.assertEqual(worker.store.state["messages"]["one"]["status"], "completed")
        self.assertEqual(worker.store.state["messages"]["one"]["attempts"], 3)

    def test_reviewed_prepared_reply_survives_delivery_failure_without_model_retry(self):
        worker = self.worker()
        put(worker.store, "one", "synthetic task")
        final = self.root / "final.md"
        final.write_text("reviewed answer", encoding="utf-8")
        with patch.object(gateway, "run_codex", return_value=("reviewed answer", "main-original", final)) as model, \
                patch.object(gateway, "reply_complete", side_effect=OSError("synthetic send failure")):
            worker._process_batch(worker.store.next_pending_batch(quiet_window_seconds=0))
        model.assert_called_once()
        row = worker.store.state["messages"]["one"]
        self.assertEqual(row["status"], "failed")
        self.assertIn("prepared_reply", row)
        restarted = self.worker()
        with patch.object(gateway, "run_codex", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", return_value="") as send:
            restarted._process_batch(restarted.store.next_pending_batch())
        model.assert_not_called()
        send.assert_called_once()
        self.assertEqual(restarted.store.state["messages"]["one"]["status"], "completed")

    def test_paused_unreviewed_result_keeps_original_batch_before_new_arrivals(self):
        record = self.orphan("main")
        worker = self.worker()
        self.release(record)
        worker.store.reconcile_runs()
        batch = worker.store.next_pending_batch()
        worker.store.enter_maintenance("isolated-upgrade", "test", "synthetic notice")
        with patch.object(gateway, "run_codex_batch", side_effect=AssertionError("duplicate model")):
            worker._process_batch(batch)
        self.assertEqual(worker.store.state["messages"]["one"]["status"], "pending")
        self.assertIn("recovered_model_result", worker.store.state["messages"]["one"])
        self.assertNotIn("prepared_reply", worker.store.state["messages"]["one"])
        put(worker.store, "later", "new task", 300)
        worker.store.exit_maintenance()
        batch = worker.store.next_pending_batch()
        self.assertEqual([row["message_id"] for row in batch], ["one", "two"])
        with patch.object(gateway, "run_codex_batch", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", return_value="") as send:
            worker._process_batch(batch)
        model.assert_not_called()
        send.assert_called_once()
        self.assertEqual(worker.store.state["messages"]["later"]["status"], "pending")

    def test_unknown_identity_is_not_treated_as_exited(self):
        with patch.object(ownership, "process_snapshot", return_value={"state": "unknown", "error": 5}):
            self.assertEqual(ownership.identity_state({"pid": 42, "created": "stamp"}), "unknown")

    def test_maintenance_waits_for_live_orphan_then_retains_result_until_exit(self):
        record = self.orphan("main")
        worker = self.worker()
        store = worker.store
        store.enter_maintenance("isolated-upgrade", "test", "synthetic notice")
        with self.assertRaises(gateway.GatewayError):
            store.exit_maintenance()
        self.release(record)
        store.reconcile_runs()
        self.assertEqual(store.next_pending_batch(), [])
        store.exit_maintenance()
        self.assertEqual([row["message_id"] for row in store.next_pending_batch()], ["one", "two"])
        self.assertEqual(store.state["messages"]["one"]["attempts"], 1)

    def test_legacy_branch_prepared_result_needs_no_main_bootstrap(self):
        store = gateway.ProjectStore("test")
        put(store, "one", "# retained result")
        final = self.root / "final.md"
        final.write_text("retained", encoding="utf-8")
        receipt = gateway.prepared_result("retained", "branch-original", final, ["one"])
        store.update_message("one", status="processing", attempts=3, prepared_reply=receipt)
        worker = self.worker()
        with patch.object(gateway, "start_thread", side_effect=AssertionError("new main")) as start, \
                patch.object(gateway, "fork_thread", side_effect=AssertionError("new branch")) as fork, \
                patch.object(gateway, "run_codex", side_effect=AssertionError("duplicate model")) as model, \
                patch.object(gateway, "reply_complete", return_value="") as send:
            self.assertEqual(worker.dispatch_parallel(), ["one"])
            until(lambda: not worker.active_branches)
        start.assert_not_called()
        fork.assert_not_called()
        model.assert_not_called()
        send.assert_called_once()
        self.assertEqual(worker.store.thread_id(), "")
        self.assertEqual(worker.store.state["messages"]["one"]["branch_thread_id"], "branch-original")

    def test_darwin_identity_uses_microseconds_and_checks_native_structure_size(self):
        lib = MagicMock()
        def native(_pid, flavor, _arg, buffer, size):
            self.assertEqual((flavor, size), (3, 136))
            info = ctypes.cast(buffer, ctypes.POINTER(ownership._DarwinBSDInfo)).contents
            info.pid, info.status, info.start_sec, info.start_usec = 42, 2, 1700000000, 123456
            return size
        lib.proc_pidinfo.side_effect = native
        with patch.object(ctypes, "CDLL", return_value=lib):
            result = ownership._darwin_snapshot(42)
        self.assertEqual(result, {"state": "alive", "identity": {
            "pid": 42, "created": "1700000000:123456", "platform": "darwin"}})

    def test_reload_can_handoff_ready_claim_without_changing_processing_state(self):
        worker = self.worker()
        put(worker.store, "one", "synthetic ready result")
        worker.store.update_message("one", status="processing", run_recovery="ready")
        worker.store.admission_open = False
        service = object.__new__(gateway.GatewayService)
        service.workers = {"test": worker}
        with patch.object(gateway.time, "monotonic", side_effect=[0, 0, 0, 1, 1]), \
                patch.object(gateway.time, "sleep"):
            snapshot = service.wait_for_quiescence(["test"], 10, quiet_seconds=0)
        self.assertEqual(snapshot["counts"], {"test": {"processing": 1}})
        self.assertEqual(snapshot["active"][0]["status"], "processing")
        self.assertTrue(snapshot["active"][0]["recovery_handoff_ready"])
        self.assertEqual(worker.store.state["messages"]["one"]["status"], "processing")

    def test_reload_still_blocks_unknown_waiting_resuming_or_active_owners(self):
        cases = [(state, False, False, False) for state in ("waiting", "outcome_unknown", "resuming")]
        cases += [("ready", True, False, False), ("ready", False, True, False),
                  ("ready", False, False, True)]
        for state, admitted, main_active, branch_active in cases:
            with self.subTest(state=state, admitted=admitted, main_active=main_active, branch_active=branch_active):
                worker = self.worker()
                worker.store.state["messages"] = {}
                put(worker.store, "one", "# synthetic branch" if branch_active else "synthetic task")
                worker.store.update_message("one", status="processing", run_recovery=state)
                worker.store.admission_open = admitted
                live = MagicMock()
                live.is_alive.return_value = True
                worker._active_main_thread = live if main_active else None
                worker.active_branches = {"one": live} if branch_active else {}
                service = object.__new__(gateway.GatewayService)
                service.workers = {"test": worker}
                with patch.object(gateway.time, "monotonic", side_effect=[0, 0, 11]), \
                        patch.object(gateway.time, "sleep"):
                    with self.assertRaises(gateway.GatewayError):
                        service.wait_for_quiescence(["test"], 10, quiet_seconds=0)

    def test_linux_identity_includes_boot_and_start_tick(self):
        stat = "42 (name with spaces) S " + " ".join(["0"] * 18 + ["12345", "0"])
        def read(path, *_args, **_kwargs):
            return stat if path.name == "stat" else "boot-generation\n"
        with patch.object(Path, "read_text", read):
            result = ownership._linux_snapshot(42)
        self.assertEqual(result["identity"]["created"], "boot-generation:12345")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child_fixture(*sys.argv[2:])
    elif sys.argv[1:2] == ["--owner"]:
        owner_fixture(*sys.argv[2:])
    else:
        unittest.main()
