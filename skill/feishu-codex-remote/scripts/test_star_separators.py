"""Star-boundary regressions with real temporary stores and mocked external I/O."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import queue_batching as qb
import remote_gateway as gateway

_REAL_RUN_CODEX = gateway.run_codex
_REAL_RUN_CODEX_BATCH = gateway.run_codex_batch


def message(mid, received=100, text="ordinary", sender="test-user"):
    return {
        "message_id": mid, "chat_id": "test-chat", "sender": {"id": sender},
        "create_time": int(received * 1000), "message_type": "text",
        "content": json.dumps({"text": text}),
    }


class StarSeparatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.isolate = patch.object(gateway, "REMOTE_STATE", self.root)
        self.isolate.start()
        self.addCleanup(self.isolate.stop)
        # Fail closed if any execution or delivery path loses its local fake.
        self.external = {}
        for name in ("run_codex", "run_codex_batch", "run_model_process",
                     "fork_thread", "start_thread", "archive_thread", "reply_complete"):
            guard = patch.object(gateway, name, side_effect=AssertionError("unexpected external " + name))
            self.external[name] = guard.start()
            self.addCleanup(guard.stop)
        for name in ("Popen", "run"):
            guard = patch.object(gateway.subprocess, name, side_effect=AssertionError("unexpected subprocess"))
            guard.start()
            self.addCleanup(guard.stop)
        self.client = MagicMock()
        self.client.add_reaction.return_value = {"data": {"reaction_id": "fake-reaction"}}
        self.client.download_resource.side_effect = AssertionError("unexpected attachment request")
        self.project = {"working_directory": str(self.root), "chat_id": "test-chat"}
        self.worker = gateway.ProjectWorker("star-test", self.project, self.client, self.root / "gateway.log")
        self.store = self.worker.store

    def put(self, *rows):
        for row in rows:
            timestamp = datetime.fromtimestamp(row["create_time"] / 1000, timezone.utc).isoformat()
            with patch.object(gateway, "now_iso", return_value=timestamp):
                self.assertTrue(self.store.enqueue(row))

    def claim(self, at):
        with patch.object(qb.time, "time", return_value=at):
            return self.store.next_pending_batch()

    def wait(self, at):
        with patch.object(qb.time, "time", return_value=at):
            return self.store.pending_wait_seconds()

    def ids(self, rows):
        return [row["message_id"] for row in rows]

    def complete_selection(self, rows):
        for row in rows:
            self.store.finish(row["message_id"], "completed")

    def result(self):
        final_path = self.root / "final.md"
        final_path.write_text("isolated answer", encoding="utf-8")
        return "isolated answer", "retained-main", final_path

    def capture_model_input(self, rows):
        self.put(*rows)
        batch = self.claim(1000)
        self.assertEqual(self.ids(batch), self.ids(rows))
        captured = []

        def execute(command, **kwargs):
            captured.append(kwargs["input"].decode("utf-8"))
            Path(command[command.index("-o") + 1]).write_text("received", encoding="utf-8")
            kwargs["stdout"].write(b'{"type":"thread.started","thread_id":"retained-main"}\n')
            return gateway.subprocess.CompletedProcess(command, 0)

        with patch.object(gateway, "run_codex", side_effect=_REAL_RUN_CODEX), \
                patch.object(gateway, "run_codex_batch", side_effect=_REAL_RUN_CODEX_BATCH), \
                patch.object(gateway, "run_model_process", side_effect=execute), \
                patch.object(gateway, "reply_complete", return_value=""):
            self.worker._process_batch(batch)
        self.assertEqual(len(captured), 1)
        for original in rows:
            stored = self.store.state["messages"][original["message_id"]]
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(stored["content"], original["content"])
            self.assertNotIn("control_action", stored)
        return captured[0].split("用户消息：\n", 1)[1].split("\n\n本地附件：", 1)[0]

    def test_actual_single_model_input_consumes_only_one_tail_marker(self):
        cases = (("只回复收到*", "只回复收到"),
                 ("  只回复收到* \n\t", "只回复收到"),
                 ("只回复收到**", "只回复收到*"),
                 ("*正文 a*b*", "正文 a*b"))
        for index, (source, expected) in enumerate(cases):
            with self.subTest(source=source):
                self.assertEqual(self.capture_model_input([
                    message("projection-" + str(index), 100 + index, source)
                ]), expected)

    def test_actual_batch_projection_retains_literal_star_and_source_anchors(self):
        rows = [message("first", 100, "正文 a*b"),
                message("tail", 110, "补充**")]
        prompt = self.capture_model_input(rows)
        self.assertIn("正文 a*b", prompt)
        self.assertIn("补充*", prompt)
        self.assertNotIn("补充**", prompt)
        for row in rows:
            stored = self.store.state["messages"][row["message_id"]]
            self.assertEqual(stored["batch_message_ids"], ["first", "tail"])
            self.assertEqual(stored["reply_source_message_id"], "tail")

    def test_branch_projection_consumes_tail_without_changing_branch_precedence(self):
        for source, expected in (("#任务*", "任务"), ("#*任务*", "*任务"),
                                 ("# /doc 任务*", "/doc 任务"), ("#任务**", "任务*")):
            with self.subTest(source=source):
                row = message("branch", text=source)
                row["branch_thread_id"] = "retained-branch"
                projected = gateway.combined_batch_item([row])
                self.assertEqual(gateway.message_text(projected["content"]), expected)
                self.assertEqual(projected["branch_thread_id"], "retained-branch")
                self.assertTrue(gateway.parallel_message(row))
                self.assertEqual(qb.star_boundaries(row), (False, False))
                self.assertEqual(gateway.message_text(row["content"]), source)

    def test_actual_pure_multiple_stars_remain_model_work_with_visible_payload(self):
        for index, (source, expected) in enumerate((("**", "*"), ("* *", "*"), ("***", "**"))):
            with self.subTest(source=source):
                self.assertEqual(self.capture_model_input([
                    message("literal-" + str(index), 100 + index, source)
                ]), expected)

    def test_rich_text_projection_does_not_mutate_attachment_source(self):
        row = message("rich")
        row["message_type"] = "post"
        row["content"] = json.dumps({"content": [[
            {"tag": "text", "text": "查看附件*  "},
            {"tag": "img", "image_key": "synthetic-image"},
        ]]})
        original = row["content"]
        projected = gateway.combined_batch_item([row])
        self.assertEqual(gateway.message_text(projected["content"]), "查看附件")
        self.assertEqual(row["content"], original)
        self.assertEqual(gateway.resource_keys(row["content"]), [("image", "synthetic-image")])

    def test_interior_and_nonfinal_stars_remain_payload(self):
        for source in ("正文 a*b", "正文*。", "只回复星号 * 然后句号。"):
            row = message("literal", text=source)
            self.assertEqual(gateway.message_text(gateway.combined_batch_item([row])["content"]), source)

    def test_trimmed_edges_and_hash_precedence_preserve_source(self):
        cases = (("", (False, False)), ("plain", (False, False)),
                 (" \t*leading\n", (True, False)), (" trailing* \n", (False, True)),
                 (" *both* ", (True, True)), ("middle * content", (False, False)),
                 (" \n#branch* ", (False, False)), ("#*branch*", (False, False)))
        for text, expected in cases:
            with self.subTest(text=text):
                row = message("source", text=text)
                original = row["content"]
                self.assertEqual(qb.star_boundaries(row), expected)
                self.assertEqual(row["content"], original)

    def test_trailing_star_includes_preceding_work_and_seals_before_followup(self):
        self.put(message("first", 100), message("tail", 110, "supplement*"),
                 message("next", 111))
        batch = self.claim(111)
        self.assertEqual(self.ids(batch), ["first", "tail"])
        self.assertEqual(self.store.state["messages"]["next"]["status"], "pending")
        self.complete_selection(batch)
        self.assertEqual(self.claim(111), [])
        self.assertEqual(self.wait(111), qb.QUIET_WINDOW_SECONDS)

    def test_both_edges_produce_independent_ready_segment(self):
        self.put(message("first", 100), message("both", 110, " *second* \n"),
                 message("next", 120))
        first = self.claim(120)
        self.assertEqual(self.ids(first), ["first"])
        self.complete_selection(first)
        both = self.claim(120)
        self.assertEqual(self.ids(both), ["both"])
        self.complete_selection(both)
        self.assertEqual(self.claim(120), [])
        self.assertEqual(self.wait(120), qb.QUIET_WINDOW_SECONDS)

    def test_successive_trailing_stars_stay_separate_after_busy_backlog(self):
        self.put(message("first", 100, "one*"), message("second", 200, "two*"),
                 message("third", 300, "three*"))
        for expected in ("first", "second", "third"):
            batch = self.claim(1000)
            self.assertEqual(self.ids(batch), [expected])
            self.complete_selection(batch)

    def test_middle_star_does_not_shorten_existing_quiet_window(self):
        self.put(message("first", 100), message("middle", 110, "one * two"))
        self.assertEqual(self.claim(110), [])
        self.assertEqual(self.wait(110), qb.QUIET_WINDOW_SECONDS)
        self.assertEqual(self.ids(self.claim(170)), ["first", "middle"])

    def test_leading_only_star_still_accepts_followup_and_waits(self):
        self.put(message("leading", 100, "*topic"), message("more", 150, "supplement"))
        self.assertEqual(self.claim(150), [])
        self.assertEqual(self.wait(150), qb.QUIET_WINDOW_SECONDS)
        self.assertEqual(self.ids(self.claim(210)), ["leading", "more"])

    def test_hash_trailing_star_neither_seals_nor_resets_main_wait(self):
        self.put(message("first", 100), message("more", 110),
                 message("branch", 114, " #independent*"))
        self.assertEqual(self.claim(114), [])
        self.assertEqual(self.wait(114), 1)
        self.assertEqual(self.ids(self.claim(115)), ["first", "more"])
        self.assertEqual(self.store.state["messages"]["branch"]["status"], "pending")

    def test_trailing_star_does_not_preempt_an_active_main_claim(self):
        self.put(message("active", 100))
        active = self.claim(160)
        self.put(message("tail", 161, "next task*"), message("later", 162))
        self.assertEqual(self.claim(162), [])
        self.assertIsNone(self.wait(162))
        self.assertEqual(self.store.state["messages"]["active"]["status"], "processing")
        self.complete_selection(active)
        self.assertEqual(self.ids(self.claim(162)), ["tail"])

    def attachment_message(self):
        row = message("attachment", 100)
        row["message_type"] = "post"
        row["content"] = json.dumps({"content": [[
            {"tag": "text", "text": "*"}, {"tag": "img", "image_key": "synthetic-image"},
        ]]})
        return row

    def test_star_attachment_is_downloaded_before_model_and_kept_as_work(self):
        row = self.attachment_message()
        self.put(row, message("later", 101))
        batch = self.claim(101)
        self.assertEqual(self.ids(batch), ["attachment"])
        events = []

        def download(mid, kind, key, target):
            self.assertEqual((mid, kind, key), ("attachment", "image", "synthetic-image"))
            target.mkdir(parents=True, exist_ok=True)
            path = target / "synthetic.png"
            path.write_bytes(b"isolated attachment fixture")
            events.append("downloaded")
            return path

        def run(_key, _project, _store, _item, attachments):
            self.assertEqual(events, ["downloaded"])
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0].read_bytes(), b"isolated attachment fixture")
            events.append("model")
            return self.result()

        self.client.download_resource.side_effect = download
        with patch.object(gateway, "run_codex", side_effect=run) as model, \
                patch.object(gateway, "reply_complete", return_value="") as deliver:
            self.worker._process_batch(batch)
        model.assert_called_once()
        deliver.assert_called_once()
        self.assertEqual(events, ["downloaded", "model"])
        stored = self.store.state["messages"]["attachment"]
        self.assertEqual(stored["status"], "completed")
        self.assertNotIn("control_action", stored)
        self.assertEqual(stored["content"], row["content"])
        self.assertEqual(self.store.state["messages"]["later"]["status"], "pending")

    def test_attachment_failure_cannot_become_empty_control_or_model_execution(self):
        self.put(self.attachment_message())
        self.client.download_resource.side_effect = OSError("isolated download failure")
        self.worker._process_batch(self.claim(100))
        self.external["run_codex"].assert_not_called()
        self.external["run_codex_batch"].assert_not_called()
        self.external["reply_complete"].assert_not_called()
        stored = self.store.state["messages"]["attachment"]
        self.assertEqual(stored["status"], "failed")
        self.assertNotIn("control_action", stored)
        self.assertEqual(gateway.resource_keys(stored["content"]), [("image", "synthetic-image")])

    def test_restart_preserves_source_ids_text_and_last_source_reply_anchor(self):
        rows = [message("first", 100, "original request"),
                message("tail", 110, "supplement*"), message("next", 120, "separate topic")]
        self.put(*rows)
        self.worker = gateway.ProjectWorker("star-test", self.project, self.client, self.root / "gateway.log")
        self.store = self.worker.store
        batch = self.claim(120)
        self.assertEqual(self.ids(batch), ["first", "tail"])
        with patch.object(gateway, "run_codex_batch", return_value=self.result()) as model, \
                patch.object(gateway, "reply_complete", return_value="") as deliver:
            self.worker._process_batch(batch)
        model.assert_called_once()
        passed_sources = model.call_args.args[3]
        self.assertEqual(self.ids(passed_sources), ["first", "tail"])
        self.assertEqual([row["content"] for row in passed_sources], [row["content"] for row in rows[:2]])
        reply_source = deliver.call_args.args[2]
        self.assertEqual(reply_source["message_id"], "tail")
        self.assertEqual(reply_source["batch_message_ids"], ["first", "tail"])
        self.assertIn("supplement", gateway.message_text(reply_source["content"]))
        self.assertNotIn("supplement*", gateway.message_text(reply_source["content"]))
        self.store = gateway.ProjectStore("star-test")
        for source in rows[:2]:
            stored = self.store.state["messages"][source["message_id"]]
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(stored["batch_message_ids"], ["first", "tail"])
            self.assertEqual(stored["reply_source_message_id"], "tail")
            self.assertEqual(stored["content"], source["content"])
        self.assertEqual(self.claim(120), [])
        self.assertEqual(self.ids(self.claim(180)), ["next"])

    def test_response_modes_split_within_sealed_prefix_and_never_cross_tail(self):
        self.put(message("document", 100, "/doc report"),
                 message("direct", 110, "/direct short*"), message("after", 111, "/direct next"))
        first = self.claim(111)
        self.assertEqual(self.ids(first), ["document"])
        self.complete_selection(first)
        second = self.claim(111)
        self.assertEqual(self.ids(second), ["direct"])
        self.assertEqual(gateway.explicit_routing_mode(second[0]), "direct")
        self.complete_selection(second)
        self.assertEqual(self.claim(111), [])
        self.assertEqual(self.wait(111), qb.QUIET_WINDOW_SECONDS)

    def test_single_bare_star_is_consumed_without_model_or_business_reply(self):
        self.put(message("control", 100, " \n* \t"), message("later", 101))
        batch = self.claim(101)
        self.assertEqual(self.ids(batch), ["control"])
        self.worker._process_batch(batch)
        self.external["run_codex"].assert_not_called()
        self.external["run_codex_batch"].assert_not_called()
        self.external["reply_complete"].assert_not_called()
        self.client.download_resource.assert_not_called()
        self.assertEqual(self.store.state["messages"]["control"]["status"], "completed")
        self.assertEqual(self.store.state["messages"]["control"]["control_action"], "flush_previous_batch")
        self.assertEqual(self.claim(101), [])
        self.assertEqual(self.wait(101), qb.QUIET_WINDOW_SECONDS)


if __name__ == "__main__":
    unittest.main()
