"""Disk-backed reply recovery faults; no cloud, model, or live service calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from gateway_common import GatewayError, atomic_write_json
from delivery_receipts import (
    DeliveryJournal, DeliveryPersistenceError, MissingDeliveryReceipt,
    ReplyRouting, deliver_reply, remote_message_id,
)


class Store:
    def __init__(self, root):
        self.path = root / "state.json"
        self.lock = threading.RLock()
        self.fail = None
        self.state = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                      else {"messages": {"source": {"status": "processing"}}})
        if not self.path.exists():
            self.save()

    def save(self):
        if self.fail and self.fail(self.state):
            raise OSError("synthetic checkpoint failure")
        atomic_write_json(self.path, self.state)


class Client:
    def __init__(self):
        self.calls, self.uploads, self.remote = [], [], {}
        self.fault = None
        self.upload_fault = None

    def _send(self, method, target, content, request_uuid):
        call = (method, target, content, request_uuid)
        self.calls.append(call)
        if self.fault:
            value = self.fault(call)
            if value is not False:
                return value
        return self._accept(call)

    def _accept(self, call):
        method, target, content, request_uuid = call
        previous = self.remote.get(request_uuid)
        if previous:
            assert previous["call"] == call, "UUID reused for changed content"
            return deepcopy(previous["response"])
        result = {"code": 0, "data": {"message_id": "remote-" + str(len(self.remote) + 1)}}
        self.remote[request_uuid] = {"call": call, "response": result}
        return deepcopy(result)

    def reply_post(self, *args):
        return self._send("reply_post", *args)

    def send_post(self, *args):
        return self._send("send_post", *args)

    def reply_image(self, *args):
        return self._send("reply_image", *args)

    def reply_file(self, *args):
        return self._send("reply_file", *args)

    def _upload(self, kind, path, root):
        self.uploads.append((kind, str(path), str(root)))
        if self.upload_fault:
            self.upload_fault(kind)
        return kind + "-" + Path(path).name

    def upload_message_image(self, *args):
        return self._upload("image", *args)

    def upload_message_file(self, *args):
        return self._upload("file", *args)


class ReplyReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.client = Client()
        self.project = {"chat_id": "chat", "working_directory": str(self.root)}
        self.item = {"message_id": "source", "content": {"text": "synthetic request"}}
        self.answer = "Original answer."
        self.final_path = self.root / "final.md"
        self.final_path.write_text(self.answer, encoding="utf-8")
        self.extract = Mock(return_value=[])
        self.wants_doc = Mock(return_value=False)
        self.publish = Mock(return_value={"document_url": "https://example.invalid/docx/synthetic", "artifacts": []})
        self.split = Mock(side_effect=lambda text: [text])
        self.routing = ReplyRouting(self.extract, self.wants_doc, self.publish, self.split,
                                    {".png"}, {".pdf", ".xlsx"})

    def deliver(self):
        return deliver_reply(self.client, self.project, self.item, self.answer, self.final_path,
                             store=self.store, routing=self.routing)

    def restart(self):
        self.store = Store(self.root)

    def state(self):
        return self.store.state["messages"]["source"]["reply_delivery"]

    def media(self):
        paths = [self.root / "preview.png", self.root / "result.pdf"]
        for path in paths:
            path.write_bytes(b"synthetic")
        self.extract.return_value = paths
        return paths

    def test_original_answer_is_not_filtered_or_parsed_as_a_schema(self):
        self.answer = '  {"status":"arbitrary","unknown":[1,2]}\n\n中文\n```text\n<x>\n```  '
        self.deliver()
        self.assertEqual(self.client.calls[0][2], self.answer)
        self.assertEqual("delivered", self.state()["status"])
        self.assertEqual("remote-1", self.state()["delivered_parts"]["text:0"])

    def test_short_answer_receipt_survives_restart_and_is_never_resent(self):
        self.assertEqual("", self.deliver())
        self.restart()
        self.deliver()
        self.assertEqual(1, len(self.client.calls))
        self.assertEqual(1, self.extract.call_count)
        self.assertEqual(1, self.split.call_count)

    def test_missing_message_receipts_remain_pending(self):
        for result in (None, {}, {"data": {}}, {"data": {"message_id": " "}},
                       {"data": {"message_id": 8}}, {"data": []}):
            with self.subTest(result=result):
                self.client.fault = lambda _call, result=result: result
                with self.assertRaises(MissingDeliveryReceipt):
                    self.deliver()
                self.assertEqual({}, self.state()["delivered_parts"])
                self.assertEqual("pending", self.state()["status"])

    def test_native_media_receipts_resume_only_missing_piece(self):
        self.media()
        self.client.fault = lambda call: {} if call[0] == "reply_file" else False
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        self.assertEqual({"text:0", "image:0"}, set(self.state()["delivered_parts"]))
        failed_uuid = self.client.calls[-1][3]
        self.restart()
        self.client.fault = None
        self.deliver()
        self.assertEqual(["reply_post", "reply_image", "reply_file", "reply_file"],
                         [call[0] for call in self.client.calls])
        self.assertEqual(failed_uuid, self.client.calls[-1][3])
        self.assertEqual(2, len(self.client.uploads))
        self.assertEqual(3, len(self.state()["delivered_parts"]))

    def test_native_send_exception_does_not_claim_warning_as_media_success(self):
        self.media()
        def fault(call):
            if call[0] == "reply_image":
                raise ConnectionError("synthetic")
            return False
        self.client.fault = fault
        with self.assertRaises(ConnectionError):
            self.deliver()
        self.assertEqual("pending", self.state()["status"])
        self.assertEqual({"text:0"}, set(self.state()["delivered_parts"]))
        self.assertFalse(any(call[0] == "send_post" for call in self.client.calls))

    def test_text_chunks_resume_without_repeating_confirmed_prefix(self):
        self.split.side_effect = None
        self.split.return_value = ["one", "two", "three"]
        self.client.fault = lambda call: {} if call[2] == "two" else False
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        failed_uuid = self.client.calls[-1][3]
        self.restart()
        self.client.fault = None
        self.deliver()
        self.assertEqual(["one", "two", "two", "three"], [call[2] for call in self.client.calls])
        self.assertEqual(failed_uuid, self.client.calls[-2][3])
        self.assertEqual(3, len(self.state()["delivered_parts"]))

    def test_document_link_receipt_failure_reuses_document_without_text_fallback(self):
        self.media()
        self.wants_doc.return_value = True
        self.client.fault = lambda _call: {}
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        self.restart()
        self.client.fault = None
        self.assertEqual(self.publish.return_value["document_url"], self.deliver())
        self.assertEqual(1, self.publish.call_count)
        self.assertEqual(2, len(self.client.uploads))
        self.assertEqual(self.client.calls[0], self.client.calls[1])
        self.split.assert_not_called()

    def test_document_send_exception_does_not_switch_same_uuid_to_text(self):
        self.wants_doc.return_value = True
        self.client.fault = Mock(side_effect=ConnectionError("synthetic"))
        with self.assertRaises(ConnectionError):
            self.deliver()
        self.split.assert_not_called()
        self.assertEqual(1, len(self.client.calls))
        self.assertIn("docx", self.client.calls[0][2])

    def test_document_creation_failure_uses_frozen_text_fallback_and_receipts(self):
        self.wants_doc.return_value = True
        self.publish.side_effect = ConnectionError("secret diagnostic must not enter state")
        self.split.side_effect = None
        self.split.return_value = ["a", "b"]
        self.client.fault = lambda call: {} if call[2] == "b" else False
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        self.restart()
        self.publish.side_effect = None
        self.client.fault = None
        self.assertEqual("", self.deliver())
        self.assertEqual(1, self.publish.call_count)
        self.assertEqual(["a", "b", "b"], [call[2] for call in self.client.calls])
        self.assertNotIn("secret diagnostic", json.dumps(self.store.state))

    def test_document_checkpoint_failure_is_not_misclassified_as_publish_failure(self):
        self.wants_doc.return_value = True
        self.store.fail = lambda state: "document" in state["messages"]["source"].get("reply_delivery", {}).get("prepared", {})
        with self.assertRaises(DeliveryPersistenceError):
            self.deliver()
        self.assertEqual([], self.client.calls)
        self.split.assert_not_called()
        self.assertEqual("pending", self.state()["status"])

    def test_upload_warning_also_requires_a_remote_receipt(self):
        self.media()
        self.project["reply_delivery_mode"] = "messages-only"
        self.client.upload_fault = Mock(side_effect=ConnectionError("upload unavailable"))
        self.client.fault = lambda call: {} if call[0] == "send_post" else False
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        self.assertEqual({"text:0"}, set(self.state()["delivered_parts"]))
        self.assertIn("不会改发飞书文档", self.client.calls[-1][2])
        self.restart()
        self.client.fault = None
        self.deliver()
        self.assertEqual(2, len(self.client.uploads))
        self.assertEqual(2, len(self.state()["delivered_parts"]))
        self.publish.assert_not_called()

    def test_document_fallback_resource_link_is_preserved_and_covered(self):
        paths = self.media()
        self.client.upload_fault = Mock(side_effect=ConnectionError("upload unavailable"))
        self.wants_doc.return_value = True
        links = [{"name": path.name, "url": "https://example.invalid/" + path.name} for path in paths]
        self.publish.return_value["artifacts"] = links
        self.deliver()
        self.assertEqual(1, len(self.client.calls))
        for value in links:
            self.assertIn(value["url"], self.client.calls[0][2])
        self.assertEqual({"text:0"}, set(self.state()["delivered_parts"]))

    def test_upload_keys_are_reused_after_interruption_before_plan(self):
        self.media()
        # Fail route determination after both uploads were saved, before sends.
        self.wants_doc.side_effect = RuntimeError("synthetic preparation interruption")
        with self.assertRaises(RuntimeError):
            self.deliver()
        self.restart()
        self.wants_doc.side_effect = None
        self.deliver()
        self.assertEqual(2, len(self.client.uploads))
        self.assertEqual(1, self.extract.call_count)

    def test_complete_plan_does_not_depend_on_surviving_local_media(self):
        paths = self.media()
        self.client.fault = lambda _call: {}
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        for path in paths:
            path.unlink()
        self.restart()
        self.client.fault = None
        self.deliver()
        self.assertEqual(2, len(self.client.uploads))
        self.assertEqual(1, self.extract.call_count)

    def test_remote_success_local_receipt_write_failure_reuses_same_uuid_after_restart(self):
        self.store.fail = lambda state: bool(state["messages"]["source"].get("reply_delivery", {}).get("delivered_parts"))
        with self.assertRaises(DeliveryPersistenceError):
            self.deliver()
        self.assertEqual({}, self.state()["delivered_parts"])
        self.assertEqual(1, len(self.client.remote))
        self.restart()
        self.deliver()
        self.assertEqual(self.client.calls[0], self.client.calls[1])
        self.assertEqual(1, len(self.client.remote))
        self.assertEqual("delivered", self.state()["status"])

    def test_remote_success_lost_http_response_reuses_uuid(self):
        def lose_response(call):
            self.client._accept(call)
            raise ConnectionError("response lost")
        self.client.fault = lose_response
        with self.assertRaises(ConnectionError):
            self.deliver()
        self.restart()
        self.client.fault = None
        self.deliver()
        self.assertEqual(self.client.calls[0], self.client.calls[1])
        self.assertEqual(1, len(self.client.remote))

    def test_final_checkpoint_failure_does_not_resend_recorded_parts(self):
        self.store.fail = lambda state: state["messages"]["source"].get("reply_delivery", {}).get("status") == "delivered"
        with self.assertRaises(DeliveryPersistenceError):
            self.deliver()
        self.assertEqual("pending", self.state()["status"])
        self.assertEqual({"text:0"}, set(self.state()["delivered_parts"]))
        self.restart()
        self.deliver()
        self.assertEqual(1, len(self.client.calls))

    def test_unsaved_plan_prevents_any_message_send(self):
        self.store.fail = lambda state: state["messages"]["source"].get("reply_delivery", {}).get("plan") is not None
        with self.assertRaises(DeliveryPersistenceError):
            self.deliver()
        self.assertEqual([], self.client.calls)

    def test_changed_answer_or_destination_cannot_reuse_existing_uuid(self):
        self.client.fault = lambda _call: {}
        with self.assertRaises(MissingDeliveryReceipt):
            self.deliver()
        for answer, chat in (("changed", "chat"), (self.answer, "other")):
            with self.subTest(answer=answer, chat=chat), self.assertRaises(GatewayError):
                DeliveryJournal(self.store, "source", answer, chat)
        self.assertEqual(1, len(self.client.calls))

    def test_existing_uuid_conventions_are_preserved_for_all_parts(self):
        self.media()
        self.split.side_effect = None
        self.split.return_value = ["one", "two"]
        self.deliver()
        base = "codex-remote-" + hashlib.sha256(b"source").hexdigest()[:32]
        self.assertEqual([base, base[:35] + "-img-1", base[:35] + "-file-1", base + "-2"],
                         [call[3] for call in self.client.calls])
        self.assertTrue(all(len(call[3]) <= 50 for call in self.client.calls))

    def test_empty_text_cannot_be_marked_delivered(self):
        self.split.side_effect = None
        self.split.return_value = []
        with self.assertRaises(GatewayError):
            self.deliver()
        self.assertEqual([], self.client.calls)
        self.assertEqual("pending", self.state()["status"])


class WorkerReplyReceiptTests(unittest.TestCase):
    """Exercise the real prepared_reply/claim/completion boundary with the adapter."""

    def setUp(self):
        import remote_gateway as gateway
        import test_gateway_lifecycle as fixtures
        self.gateway = gateway
        fixtures.LifecycleTests.setUp(self)
        self.enqueue = lambda *args, **kwargs: fixtures.LifecycleTests.enqueue(self, *args, **kwargs)
        self.answer = "One unchanged final answer."
        self.final_path = self.root / "final.md"
        self.final_path.write_text(self.answer, encoding="utf-8")
        self.routing = ReplyRouting(gateway.extract_local_artifacts, lambda source, text, _project: gateway.should_publish_document(source, text),
                                    Mock(side_effect=AssertionError("Unexpected document publish")), gateway.chunks,
                                    gateway.IMAGE_ARTIFACT_SUFFIXES, gateway.NATIVE_MESSAGE_FILE_SUFFIXES)
        def integrated(client, project, item, answer, final_path, log_path, *, store=None):
            return deliver_reply(client, project, item, answer, final_path,
                                 store=store or self.worker.store, routing=self.routing)
        guard = patch.object(gateway, "reply_complete", side_effect=integrated)
        guard.start()
        self.addCleanup(guard.stop)

    def process(self):
        with patch.object(self.gateway, "run_codex", return_value=(self.answer, "main", self.final_path)) as one, \
             patch.object(self.gateway, "run_codex_batch", return_value=(self.answer, "main", self.final_path)) as many:
            self.worker._process_batch(self.store.next_pending_batch(quiet_window_seconds=0))
            return one.call_count + many.call_count

    def restart(self):
        self.worker = self.gateway.ProjectWorker("test", self.project, self.client, self.root / "log")
        self.store = self.worker.store

    def test_worker_missing_receipt_is_not_completed_and_recovery_does_not_rerun_model(self):
        self.enqueue()
        self.client.reply_post.return_value = {}
        self.assertEqual(1, self.process())
        row = self.store.state["messages"]["one"]
        self.assertEqual("failed", row["status"])
        self.assertEqual(self.answer, row["prepared_reply"]["answer"])
        self.assertEqual("pending", row["reply_delivery"]["status"])
        first_call = self.client.reply_post.call_args
        self.restart()
        self.client.reply_post.return_value = {"data": {"message_id": "remote-receipt"}}
        self.assertEqual(0, self.process())
        row = self.store.state["messages"]["one"]
        self.assertEqual("completed", row["status"])
        self.assertEqual("remote-receipt", row["reply_delivery"]["delivered_parts"]["text:0"])
        self.assertEqual(first_call, self.client.reply_post.call_args)

    def test_batched_output_keeps_last_anchor_and_skips_confirmed_chunk(self):
        self.enqueue("first")
        self.enqueue("last")
        self.routing = ReplyRouting(self.gateway.extract_local_artifacts, lambda *_: False,
                                    self.routing.publish_document, lambda _: ["first part", "second part"],
                                    self.gateway.IMAGE_ARTIFACT_SUFFIXES, self.gateway.NATIVE_MESSAGE_FILE_SUFFIXES)
        self.client.send_post.return_value = {}
        self.assertEqual(1, self.process())
        first_call = self.client.reply_post.call_args
        self.assertEqual("last", first_call.args[0])
        self.restart()
        self.client.send_post.return_value = {"data": {"message_id": "last-part"}}
        self.assertEqual(0, self.process())
        self.assertEqual(1, self.client.reply_post.call_count)
        for message_id in ("first", "last"):
            self.assertEqual("completed", self.store.state["messages"][message_id]["status"])
            self.assertEqual("last", self.store.state["messages"][message_id]["reply_source_message_id"])

    def test_batch_completion_save_failure_reuses_all_receipts(self):
        self.enqueue()
        save = self.store.save
        failed = False
        def fail_completion_once():
            nonlocal failed
            if not failed and self.store.state["messages"]["one"]["status"] == "completed":
                failed = True
                raise OSError("synthetic completion persistence failure")
            save()
        with patch.object(self.store, "save", side_effect=fail_completion_once):
            self.assertEqual(1, self.process())
        self.assertTrue(failed)
        self.assertEqual("failed", self.store.state["messages"]["one"]["status"])
        self.restart()
        self.assertEqual(0, self.process())
        self.assertEqual(1, self.client.reply_post.call_count)
        self.assertEqual("completed", self.store.state["messages"]["one"]["status"])


class PublicReplyPresentationTests(unittest.TestCase):
    """Exercise the release adapter, including its existing language contract."""

    def exercise(self, root, language, *, document, fallback=False, missing_receipt=False):
        import remote_gateway as gateway
        store, client = Store(root), Client()
        image = root / "preview.png"
        image.write_bytes(b"synthetic")
        answer = 'Original answer.\n\n{"arbitrary":"kept verbatim"}'
        final = root / "final.md"
        final.write_text(answer, encoding="utf-8")
        project = {"chat_id": "chat", "working_directory": str(root), "language": language}
        item = {"message_id": "source", "content": {"text": "synthetic request"}}
        client.upload_fault = Mock(side_effect=OSError("synthetic upload failure"))
        published = {"document_url": "https://example.invalid/docx/synthetic", "artifacts": []}
        if fallback:
            published["artifacts"] = [{"name": image.name, "url": "https://example.invalid/file/synthetic"}]
        args = (client, project, item, answer, final, root / "gateway.log")
        with patch.object(gateway, "extract_local_artifacts", return_value=[image]) as extract, \
                patch.object(gateway, "should_publish_document", return_value=document), \
                patch.object(gateway, "publish_document", return_value=published) as publish:
            if missing_receipt:
                client.fault = lambda _call: {}
                with self.assertRaises(MissingDeliveryReceipt):
                    gateway.reply_complete(*args, store=store)
                self.assertEqual("pending", store.state["messages"]["source"]["reply_delivery"]["status"])
                client.fault = None
                store = Store(root)
                final.unlink()
                image.unlink()
            gateway.reply_complete(*args, store=store)
            gateway.reply_complete(*args, store=store)
            self.assertEqual(int(document), publish.call_count)
            self.assertEqual(1, extract.call_count)
        self.assertEqual(1, len(client.uploads))
        self.assertEqual("delivered", store.state["messages"]["source"]["reply_delivery"]["status"])
        return client.calls, answer

    def test_bilingual_document_fallback_is_receipted_and_reused(self):
        for language, label in (("en", "Open the full Feishu document"), ("zh-CN", "打开完整飞书文档")):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as raw:
                calls, _ = self.exercise(Path(raw), language, document=True, fallback=True)
                self.assertEqual(1, len(calls))
                self.assertIn(label, calls[0][2])
                self.assertIn("https://example.invalid/file/synthetic", calls[0][2])
                self.assertNotIn("not delivered", calls[0][2])
                self.assertNotIn("未送达", calls[0][2])

    def test_bilingual_upload_warning_preserves_original_answer(self):
        for language, prefix in (("en", "Attachments were not delivered"), ("zh-CN", "附件未送达")):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as raw:
                calls, answer = self.exercise(Path(raw), language, document=False)
                self.assertEqual(1, len(calls))
                self.assertTrue(calls[0][2].startswith(prefix))
                self.assertTrue(calls[0][2].endswith(answer))

    def test_unconfirmed_document_reply_reuses_uuid_and_prepared_resources(self):
        with tempfile.TemporaryDirectory() as raw:
            calls, _ = self.exercise(Path(raw), "en", document=True, missing_receipt=True)
            self.assertEqual(2, len(calls))
            self.assertEqual(calls[0], calls[1])
            self.assertIn("Attachments were not delivered", calls[1][2])
            self.assertIn("Open the full Feishu document", calls[1][2])
            self.assertNotIn("附件", calls[1][2])


if __name__ == "__main__":
    unittest.main()
