"""Durable receipts for one already-generated reply, using the existing store.

This module does not run models, schedule retries, or change routing policy.
The caller supplies the gateway's existing routing functions and owns its queue.
Remote creation and local persistence cannot be atomic: a crash before recording
an upload/document result can still leave an orphan remote resource. Message
retries keep the same UUID and payload; remote UUID deduplication is time-bounded.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any, Callable

from gateway_common import GatewayError, message_text


class MissingDeliveryReceipt(GatewayError):
    """A send did not return evidence that a remote message was created."""


class DeliveryPersistenceError(GatewayError):
    """Do not turn a local checkpoint failure into a remote-content fallback."""


def remote_message_id(result: Any) -> str:
    data = result.get("data") if isinstance(result, dict) else None
    value = data.get("message_id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise MissingDeliveryReceipt("Reply delivery has no remote message receipt")
    return value


@dataclass(frozen=True)
class ReplyRouting:
    extract_artifacts: Callable
    wants_document: Callable
    publish_document: Callable
    split_text: Callable
    image_suffixes: set[str]
    file_suffixes: set[str]
    presentation: Callable | None = None


class DeliveryJournal:
    """Short transactions on the source anchor; the existing worker owns sends."""

    def __init__(self, store, message_id: str, answer: str, chat_id: str):
        self.store, self.message_id = store, message_id
        identity = {"source_message_id": message_id, "chat_id": chat_id,
                    "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest()}
        with store.lock:
            row = store.state["messages"][message_id]
            current = row.get("reply_delivery")
            if current is not None:
                if not isinstance(current, dict) or current.get("identity") != identity:
                    raise GatewayError("Prepared delivery belongs to a different reply")
            else:
                self._write({"identity": identity, "prepared": {}, "plan": None,
                             "delivered_parts": {}, "document_link": "", "status": "pending"})

    def snapshot(self) -> dict[str, Any]:
        with self.store.lock:
            return deepcopy(self.store.state["messages"][self.message_id]["reply_delivery"])

    def _write(self, value: dict[str, Any]) -> None:
        with self.store.lock:
            row = self.store.state["messages"][self.message_id]
            had_previous = "reply_delivery" in row
            previous = row.get("reply_delivery")
            row["reply_delivery"] = deepcopy(value)
            try:
                self.store.save()
            except Exception as exc:
                # Unsaved state must never allow this process to skip a send or
                # claim completion. A retry uses the durable UUID/payload again.
                if had_previous:
                    row["reply_delivery"] = previous
                else:
                    row.pop("reply_delivery", None)
                raise DeliveryPersistenceError("Reply checkpoint could not be saved") from exc

    def prepare(self, key: str, operation: Callable, *, optional=False) -> Any:
        current = self.snapshot()
        if key in current["prepared"]:
            return deepcopy(current["prepared"][key])
        try:
            value = operation()
            result = {"value": value} if optional else value
        except Exception as exc:
            if not optional:
                raise
            result = {"error_type": type(exc).__name__}
        # Deliberately outside the optional-effect catch: disk failure must not
        # switch document output into text or mark an upload as remotely failed.
        with self.store.lock:
            current = self.snapshot()
            current["prepared"][key] = deepcopy(result)
            self._write(current)
        return result

    def set_plan(self, parts: list[dict[str, str]], document_link: str) -> None:
        if not parts:
            raise GatewayError("Prepared reply contains no deliverable message")
        with self.store.lock:
            current = self.snapshot()
            if current["plan"] is not None:
                if current["plan"] != parts or current["document_link"] != document_link:
                    raise GatewayError("Prepared reply delivery plan changed")
                return
            current.update(plan=deepcopy(parts), document_link=document_link)
            self._write(current)

    def deliver(self, client) -> str:
        current = self.snapshot()
        parts = current["plan"]
        if not parts:
            raise GatewayError("Reply delivery has no prepared plan")
        methods = {"reply_post", "send_post", "reply_image", "reply_file"}
        for part in parts:
            if self.snapshot()["delivered_parts"].get(part["key"]):
                continue
            if part["method"] not in methods:
                raise GatewayError("Invalid prepared reply delivery operation")
            result = getattr(client, part["method"])(part["target"], part["content"], part["uuid"])
            receipt = remote_message_id(result)
            with self.store.lock:
                current = self.snapshot()
                current["delivered_parts"][part["key"]] = receipt
                self._write(current)
        with self.store.lock:
            current = self.snapshot()
            if not all(current["delivered_parts"].get(part["key"]) for part in parts):
                raise MissingDeliveryReceipt("Reply delivery remains incomplete")
            if current["status"] != "delivered":
                current.update(status="delivered", delivered_at=datetime.now().astimezone().isoformat())
                self._write(current)
        return current["document_link"]


def _resource_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayError("Media preparation has no remote resource key")
    return value


def _published_document(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or not isinstance(value.get("document_url"), str)
            or not value["document_url"].startswith(("https://", "http://"))):
        raise GatewayError("Document preparation has no remote document link")
    return value


def _artifact_warning(project, failed: list[Path]) -> str:
    # Preserve the gateway's existing user-facing fallback and name truncation.
    names = "、".join(path.name for path in failed[:5])
    if len(failed) > 5:
        names += f" 等 {len(failed)} 个文件"
    next_step = (
        "可让 Codex 重试附件消息；本项目不会改发飞书文档。"
        if project.get("reply_delivery_mode") == "messages-only"
        else "可打开飞书文档中的备用链接，或让 Codex 重试。"
    )
    return f"部分附件未送达（{names}）。{next_step}"


def remaining_artifact_failures(failed: list[Path], delivered: list[dict[str, str]]) -> list[Path]:
    """Keep the existing document-link fallback's attachment matching behavior."""
    covered = {str(value.get("name") or "").strip().casefold() for value in delivered
               if str(value.get("url") or "").startswith("http")}
    return [path for path in failed if path.name.casefold() not in covered]


def deliver_reply(client, project, item, answer: str, final_path: Path, *, store,
                  routing: ReplyRouting) -> str:
    """Deliver an unchanged prepared answer; return only after durable receipts.

    Route choices, media keys and complete send payloads are checkpointed before
    message sends. A resumed plan requires no source-file reads, uploads, document
    publication or model execution. Call only under the existing per-batch owner.
    """
    message_id = str(item["message_id"])
    journal = DeliveryJournal(store, message_id, answer, str(project["chat_id"]))
    if journal.snapshot()["plan"] is not None:
        return journal.deliver(client)
    base_uuid = "codex-remote-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    allowed_root = project.get("working_directory") or final_path.parent
    artifacts = [Path(value) for value in journal.prepare(
        "artifacts", lambda: [str(path) for path in routing.extract_artifacts(answer, allowed_root)])]
    media, failed = [], []
    for kind, suffixes, upload, tag in (
        ("image", routing.image_suffixes, "upload_message_image", "img"),
        ("file", routing.file_suffixes, "upload_message_file", "file"),
    ):
        uploaded_index = 0
        for index, artifact in enumerate(artifacts):
            if artifact.suffix.lower() not in suffixes:
                continue
            prepared = journal.prepare(
                f"{kind}:{index}", lambda p=artifact, fn=upload: _resource_key(getattr(client, fn)(p, allowed_root)),
                optional=True)
            if "error_type" in prepared:
                failed.append(artifact)
                continue
            uploaded_index += 1
            media.append({"key": f"{kind}:{index}", "method": "reply_" + kind,
                          "target": message_id, "content": prepared["value"],
                          "uuid": f"{base_uuid[:35]}-{tag}-{uploaded_index}"})
    wants_document = journal.prepare(
        "wants_document", lambda: routing.wants_document(message_text(item.get("content")), answer, project))
    document = None
    if wants_document:
        prepared = journal.prepare(
            "document", lambda: _published_document(routing.publish_document(project, final_path, artifacts)),
            optional=True)
        document = prepared.get("value")
    link = ""
    if document is not None:
        link = document["document_url"]
        summary = next((line.strip("# ") for line in answer.splitlines() if line.strip()), "完整结果已整理")
        text = f"{summary[:240]}\n\n[打开完整飞书文档]({link})"
        fallback_artifacts = document.get("artifacts") or []
        if fallback_artifacts:
            links = [f"[{artifact['name']}]({artifact['url']})" for artifact in fallback_artifacts]
            text += "\n\n附件与产物：" + " · ".join(links)
        failed = remaining_artifact_failures(failed, fallback_artifacts)
        texts = [text]
    else:
        # The existing splitter owns boundaries and formatting; no output schema
        # or content filter is introduced by delivery bookkeeping.
        texts = list(routing.split_text(answer))
        if not texts:
            raise GatewayError("Prepared reply contains no deliverable message")
        if wants_document and failed:
            texts[0] = "附件上传失败；以下先返回文字结果，本地产物未送达。\n\n" + texts[0]
    if routing.presentation is not None:
        texts, failed = routing.presentation(project, answer, document, texts, failed)
    parts = [{"key": "text:0", "method": "reply_post", "target": message_id,
              "content": texts[0], "uuid": base_uuid}] + media
    if failed:
        parts.append({"key": "artifact-warning", "method": "send_post", "target": project["chat_id"],
                      "content": _artifact_warning(project, failed),
                      "uuid": f"{base_uuid[:35]}-artifact-warn"})
    for index, text in enumerate(texts[1:], start=2):
        parts.append({"key": f"text:{index - 1}", "method": "send_post", "target": project["chat_id"],
                      "content": text, "uuid": f"{base_uuid}-{index}"})
    journal.set_plan(parts, link)
    return journal.deliver(client)
