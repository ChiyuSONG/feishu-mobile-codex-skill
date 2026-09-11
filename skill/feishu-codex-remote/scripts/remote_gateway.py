from __future__ import annotations

from queue_batching import QUIET_WINDOW_SECONDS, select_pending
from codex_thread_fork import archive_thread, fork_thread, start_thread
from provider_failure import ProviderCapacityError, terminal_capacity_failure
from gateway_lifecycle import LifecycleStore, LifecycleWorker, MaintenancePaused, MaintenanceStopFailed, run_model_process

import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from typing import Any
import urllib.parse
import uuid

from reminders import due_reminders, mark_sent

from gateway_common import (
    CONFIG_PATH,
    PUBLISH_CONFIG_PATH,
    REMOTE_STATE,
    FeishuClient,
    GatewayError,
    atomic_write_json,
    load_app_credentials,
    load_config,
    load_json,
    message_text,
    now_epoch,
    resource_keys,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PUBLISHER = SCRIPT_DIR / "feishu_publish.py"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WORKING_REACTION = "Typing"
COMPLETION_REACTION = "CheckMark"
SYNC_REQUESTS = REMOTE_STATE / "sync_requests"
SYNC_RESPONSES = REMOTE_STATE / "sync_responses"
RELOAD_REQUESTS = REMOTE_STATE / "reload_requests"
RELOAD_RESPONSES = REMOTE_STATE / "reload_responses"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
AUTOMATIONS_ROOT = CODEX_HOME / "automations"
CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_PERMISSION_MODE = "full-access"
DEFAULT_AGENT_MODEL = "gpt-6-astra"
PROJECT_ONLY_PERMISSION_MODE = "project-only-auto"
AUTO_REVIEW_PERMISSION_MODE = "auto-review"
PERMISSION_MODES = {
    DEFAULT_PERMISSION_MODE,
    PROJECT_ONLY_PERMISSION_MODE,
    AUTO_REVIEW_PERMISSION_MODE,
}


def project_agent_model(project: dict[str, Any]) -> str:
    return str(project.get("agent_model") or "").strip() or DEFAULT_AGENT_MODEL


def cli_json_dumps(value: Any) -> str:
    """Return JSON that is safe on legacy Windows console code pages."""
    return json.dumps(value, ensure_ascii=True, indent=2)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{now_iso()}] {text.rstrip()}\n")


def codex_cli_path() -> Path:
    override = os.environ.get("CODEX_CLI_PATH", "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path
    if os.name == "nt":
        candidates = sorted(
            (Path.home() / "AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    discovered = shutil.which("codex")
    if discovered:
        return Path(discovered)
    raise GatewayError("Cannot find Codex CLI; open Codex or set CODEX_CLI_PATH")


def runtime_python_path(system: str | None = None) -> Path:
    current = system or platform.system()
    if current == "Windows":
        return REMOTE_STATE / ".venv" / "Scripts" / "python.exe"
    return REMOTE_STATE / ".venv" / "bin" / "python"


def codex_rate_limits(timeout: float = 15.0) -> dict[str, Any]:
    process = subprocess.Popen(
        [str(codex_cli_path()), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )
    responses: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read_responses() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                responses.put(value)
        responses.put(None)

    threading.Thread(target=read_responses, name="codex-rate-limits", daemon=True).start()

    def request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = responses.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise GatewayError(f"Timed out waiting for Codex {method}") from exc
            if response is None:
                raise GatewayError(f"Codex app-server exited while handling {method}")
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise GatewayError(f"Codex {method} failed: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise GatewayError(f"Codex {method} returned no result")
            return result
        raise GatewayError(f"Timed out waiting for Codex {method}")

    try:
        request(
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "feishu_remote_usage",
                    "title": "Feishu Remote Usage",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        result = request(2, "account/rateLimits/read", {})
        rate_limits = result.get("rateLimits")
        if not isinstance(rate_limits, dict):
            raise GatewayError("Codex account/rateLimits/read returned no primary rate limit")
        return rate_limits
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def format_codex_usage(rate_limits: dict[str, Any], language: str = "zh-CN") -> str:
    english = language == "en"

    def window_label(minutes: int) -> str:
        if minutes > 0 and minutes % 1440 == 0:
            value = minutes // 1440
            return f"{value} day" + ("s" if value != 1 else "") if english else f"{value}天"
        if minutes > 0 and minutes % 60 == 0:
            value = minutes // 60
            return f"{value} hour" + ("s" if value != 1 else "") if english else f"{value}小时"
        return f"{minutes} minutes" if english else f"{minutes}分钟"

    parts: list[str] = []
    for item in (rate_limits.get("primary"), rate_limits.get("secondary")):
        if not isinstance(item, dict) or item.get("usedPercent") is None:
            continue
        used = max(0, min(100, int(round(float(item["usedPercent"])))))
        remaining = 100 - used
        minutes = int(item.get("windowDurationMins") or 0)
        reset_at = int(item.get("resetsAt") or 0)
        reset_text = ""
        if reset_at > 0:
            reset = datetime.fromtimestamp(reset_at).astimezone()
            reset_text = (
                f", resets {reset:%b %d %H:%M}"
                if english
                else f"，{reset.month}月{reset.day}日 {reset:%H:%M} 重置"
            )
        parts.append(
            f"{window_label(minutes)}: {remaining}% remaining ({used}% used{reset_text})"
            if english
            else f"{window_label(minutes)}额度剩余 {remaining}%（已用 {used}%{reset_text}）"
        )
    if not parts:
        raise GatewayError("Codex rate-limit response contains no usable window")
    if english:
        suffix = "; spend limit reached" if rate_limits.get("spendControlReached") else ""
        return "Codex usage: " + "; ".join(parts) + suffix
    suffix = "；已达到消费上限" if rate_limits.get("spendControlReached") else ""
    return "Codex 用量：" + "；".join(parts) + suffix


def usage_report_uuid(project_key: str, at: datetime | None = None) -> str:
    bucket = (at or datetime.now().astimezone()).astimezone().strftime("%Y%m%d%H")
    digest = hashlib.sha256(f"{project_key}\0{bucket}".encode("utf-8")).hexdigest()[:28]
    return "codex-usage-" + digest


def send_codex_usage_report(config: dict[str, Any], project_key: str) -> dict[str, Any]:
    project = config["projects"].get(project_key)
    if not isinstance(project, dict) or not project.get("chat_id"):
        raise GatewayError(f"Remote project has no Feishu chat binding: {project_key}")
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to send Codex usage in an unexpected tenant")
    available = True
    error = ""
    language = project_language(project)
    try:
        text = format_codex_usage(codex_rate_limits(), language)
    except Exception as exc:
        available = False
        error = str(exc)
        text = "Codex usage: temporarily unavailable." if language == "en" else "Codex 用量：暂时无法读取。"
    payload = client.send_post(str(project["chat_id"]), text, usage_report_uuid(project_key))
    message_id = str((payload.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise GatewayError(f"Codex usage report returned no message_id: {payload}")
    return {"available": available, "error": error, "message_id": message_id, "text": text}


def send_due_reminders(config: dict[str, Any], project_key: str) -> list[dict[str, Any]]:
    project = config["projects"].get(project_key)
    if not isinstance(project, dict) or not project.get("chat_id"):
        raise GatewayError(f"Remote project has no Feishu chat binding: {project_key}")
    due = due_reminders(project_key)
    if not due:
        return []
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to send reminders in an unexpected tenant")
    bucket = datetime.now().astimezone().strftime("%Y%m%d%H")
    sent: list[dict[str, Any]] = []
    heading = "**Reminder**" if project_language(project) == "en" else "**提醒**"
    for reminder in due:
        reminder_id = str(reminder["reminder_id"])
        text = f"{heading}\n\n{reminder['text']}"
        digest = hashlib.sha256(f"{project_key}\0{reminder_id}\0{bucket}".encode("utf-8")).hexdigest()[:24]
        payload = client.send_post(str(project["chat_id"]), text, "codex-reminder-" + digest)
        message_id = str((payload.get("data") or {}).get("message_id") or "")
        if not message_id:
            raise GatewayError(f"Reminder returned no message_id: {payload}")
        mark_sent(project_key, reminder_id)
        sent.append({"reminder_id": reminder_id, "message_id": message_id})
    return sent


def assert_config(config: dict[str, Any]) -> None:
    required = ("app_id", "expected_tenant_key", "projects")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise GatewayError(f"Gateway config is incomplete: {', '.join(missing)}")
    if not isinstance(config["projects"], dict) or not config["projects"]:
        raise GatewayError("No remote projects are registered")


def project_runtime(project_key: str) -> Path:
    return REMOTE_STATE / "projects" / project_key


def _parse_local_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def _elapsed_text(started_at: Any, at: datetime | None = None, language: str = "zh-CN") -> str:
    started = _parse_local_datetime(started_at)
    current = (at or datetime.now().astimezone()).astimezone()
    seconds = max(0, int((current - started).total_seconds())) if started else 0
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if language == "en":
        if hours:
            return f"{hours}h {minutes}m"
        return f"{max(1, minutes)}m"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{max(1, minutes)}分钟"


def _jsonl_tail(path: Path, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            payload = handle.read()
        if start:
            newline = payload.find(b"\n")
            payload = payload[newline + 1 :] if newline >= 0 else b""
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for raw in payload.splitlines():
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _latest_run_event_log(project_key: str, started_at: Any = None) -> Path | None:
    run_root = project_runtime(project_key) / "runs"
    try:
        candidates = [path for path in run_root.glob("*/events.jsonl") if path.is_file()]
        started = _parse_local_datetime(started_at)
        if started is not None:
            candidates = [path for path in candidates if path.stat().st_mtime >= started.timestamp()]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except OSError:
        return None


def _one_line_progress_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"(?i)(?:/?[a-z]:[\\/])[^\s，。；;）)]*", "[redacted path]", text)
    text = re.sub(r"\b(?:om|oc|ou)_[A-Za-z0-9]+\b", "[redacted id]", text)
    text = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "[redacted id]",
        text,
    )
    text = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{12,}|LTAI[A-Za-z0-9]{8,})\b", "[redacted secret]", text)
    text = re.sub(
        r"`([^`]+)`",
        lambda match: "[redacted command]"
        if re.search(r"[\\/]|--|\s|\.(?:exe|ps1|bat|cmd|sh|py)\b", match.group(1), re.IGNORECASE)
        else match.group(0),
        text,
    )
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _event_progress_detail(records: list[dict[str, Any]], language: str = "zh-CN") -> str:
    latest_item: dict[str, Any] | None = None
    latest_event_type = ""
    latest_agent_text = ""
    for record in reversed(records):
        item = record.get("item")
        if latest_item is None and isinstance(item, dict):
            latest_item = item
            latest_event_type = str(record.get("type") or "")
        if not latest_agent_text and isinstance(item, dict) and item.get("type") == "agent_message":
            text = _one_line_progress_text(item.get("text"))
            if text:
                latest_agent_text = text
    generic = "Codex is still running" if language == "en" else "Codex 仍在执行"
    if latest_item is not None:
        item_type = str(latest_item.get("type") or "")
        in_progress = latest_event_type.endswith("started") or latest_item.get("status") == "in_progress"
        if language == "en":
            if item_type == "command_execution":
                generic = "running a project command or validation" if in_progress else "a project command or validation just finished"
            elif item_type == "file_change":
                generic = "updating project files" if in_progress else "a project file update just finished"
            elif item_type in {"mcp_tool_call", "web_search"}:
                generic = "using a tool or checking external information"
        else:
            if item_type == "command_execution":
                generic = "正在运行项目命令或验证" if in_progress else "刚完成一项项目命令或验证"
            elif item_type == "file_change":
                generic = "正在修改项目文件" if in_progress else "刚完成一项文件修改"
            elif item_type in {"mcp_tool_call", "web_search"}:
                generic = "正在调用工具或核验外部信息"
    if latest_agent_text:
        if latest_item is not None and latest_item.get("type") != "agent_message":
            connector = "; current: " if language == "en" else "；当前："
            return latest_agent_text + connector + generic
        return latest_agent_text
    return generic


def active_task_progress_snapshot(
    project_key: str,
    at: datetime | None = None,
    language: str = "zh-CN",
) -> dict[str, Any] | None:
    state = load_json(project_runtime(project_key) / "state.json", {})
    processing = [
        dict(item)
        for item in (state.get("messages") or {}).values()
        if item.get("status") == "processing"
    ]
    if not processing:
        return None
    processing.sort(key=lambda item: (str(item.get("processing_started_at") or ""), str(item.get("message_id") or "")))
    batch_id = str(processing[0].get("batch_id") or processing[0].get("message_id") or "active")
    batch = [item for item in processing if str(item.get("batch_id") or item.get("message_id") or "active") == batch_id]
    event_log_value = next((item.get("run_event_log") for item in batch if item.get("run_event_log")), None)
    event_log = (
        Path(str(event_log_value))
        if event_log_value
        else _latest_run_event_log(project_key, processing[0].get("processing_started_at"))
    )
    records = _jsonl_tail(event_log) if event_log else []
    return {
        "project_key": project_key,
        "batch_id": batch_id,
        "message_count": len(batch),
        "started_at": processing[0].get("processing_started_at"),
        "elapsed": _elapsed_text(processing[0].get("processing_started_at"), at, language),
        "detail": _event_progress_detail(records, language),
    }


def active_task_progress_text(snapshot: dict[str, Any], language: str = "zh-CN") -> str:
    count = int(snapshot.get("message_count") or 1)
    detail = _one_line_progress_text(snapshot.get("detail"))
    if language == "en":
        batch = f" ({count} queued messages in this batch)" if count > 1 else ""
        return f"Task progress: running{batch} for {snapshot['elapsed']}; latest progress: {detail}."
    batch = f"（本批次 {count} 条消息）" if count > 1 else ""
    return f"任务进度：执行中{batch}，已运行 {snapshot['elapsed']}；最近进展：{detail}。"


def active_task_progress_uuid(project_key: str, batch_id: str, at: datetime | None = None) -> str:
    bucket = (at or datetime.now().astimezone()).astimezone().strftime("%Y%m%d%H")
    digest = hashlib.sha256(f"{project_key}\0{batch_id}\0{bucket}".encode("utf-8")).hexdigest()[:28]
    return "codex-progress-" + digest


def send_active_task_progress_report(config: dict[str, Any], project_key: str) -> dict[str, Any]:
    project = config["projects"].get(project_key)
    if not isinstance(project, dict) or not project.get("chat_id"):
        raise GatewayError(f"Remote project has no Feishu chat binding: {project_key}")
    if not project_hourly_catch_up_enabled(project):
        return {"ok": True, "sent": False, "reason": "hourly-disabled"}
    language = project_language(project)
    snapshot = active_task_progress_snapshot(project_key, language=language)
    if snapshot is None:
        return {"ok": True, "sent": False, "reason": "idle"}
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to send task progress in an unexpected tenant")
    text = active_task_progress_text(snapshot, language)
    payload = client.send_post(
        str(project["chat_id"]),
        text,
        active_task_progress_uuid(project_key, str(snapshot["batch_id"])),
    )
    message_id = str((payload.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise GatewayError(f"Task progress report returned no message_id: {payload}")
    return {"ok": True, "sent": True, "message_id": message_id, "text": text}


def ignore_message(message: dict[str, Any]) -> bool:
    sender = message.get("sender") or {}
    sender_type = str(sender.get("sender_type") or "").lower()
    message_type = str(message.get("message_type") or "").lower()
    return sender_type in {"app", "bot", "system"} or message_type == "system"


class ProjectStore(LifecycleStore):
    def __init__(self, project_key: str) -> None:
        self.project_key = project_key
        self.root = project_runtime(project_key)
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        self.admission_open = True
        self.state = load_json(
            self.path,
            {
                "schema_version": 1,
                "thread_id": "",
                "cursor": 0,
                "messages": {},
                "updated_at": None,
            },
        )

    def save(self) -> None:
        with self.lock:
            self.state["updated_at"] = now_iso()
            atomic_write_json(self.path, self.state)

    def enqueue(self, message: dict[str, Any]) -> bool:
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            return False
        create_time = int(str(message.get("create_time") or "0") or 0)
        if ignore_message(message):
            with self.lock:
                self.state["cursor"] = max(int(self.state.get("cursor") or 0), create_time // 1000)
                self.save()
            return False
        sender = message.get("sender") or {}
        with self.lock:
            items = self.state.setdefault("messages", {})
            if message_id in items:
                return False
            items[message_id] = {
                "message_id": message_id,
                "chat_id": message.get("chat_id"),
                "create_time": create_time,
                "message_type": message.get("message_type"),
                "content": message.get("content"),
                "sender": sender,
                "status": "pending",
                "attempts": 0,
                "received_at": now_iso(),
            }
            if self.pause_requested():
                self._defer(items[message_id])
            self.state["cursor"] = max(int(self.state.get("cursor") or 0), create_time // 1000)
            self.save()
        return True

    def recover_interrupted(self) -> None:
        changed = False
        with self.lock:
            for item in self.state.get("messages", {}).values():
                if ignore_message(item):
                    if item.get("status") != "ignored":
                        item["status"] = "ignored"
                        item["ignored_at"] = now_iso()
                        changed = True
                elif item.get("status") == "processing" and self.pause_requested([item["message_id"]]):
                    item["maintenance_stop_error"] = "Listener interrupted; stopping has not been confirmed"
                    changed = True
                elif item.get("status") in {"pending", "failed"} and self.pause_requested([item["message_id"]]):
                    self._defer(item)
                    changed = True
                elif item.get("status") == "processing" or (
                    item.get("status") == "pending" and int(item.get("attempts") or 0) >= 3
                ):
                    item["status"] = "failed" if int(item.get("attempts") or 0) >= 3 else "pending"
                    item["recovered_at"] = now_iso()
                    changed = True
            if changed:
                self.save()

    def next_pending(self, *, quiet_window_seconds: float = QUIET_WINDOW_SECONDS) -> dict[str, Any] | None:
        batch = self.next_pending_batch(max_messages=1, quiet_window_seconds=quiet_window_seconds)
        return batch[0] if batch else None

    def next_pending_batch(
        self,
        *,
        max_messages: int | None = None,
        merge_window_seconds: int | None = None,
        quiet_window_seconds: float = QUIET_WINDOW_SECONDS,
    ) -> list[dict[str, Any]]:
        with self.lock:
            if not self.admission_open or self.pause_requested() or self.state.get("main_bootstrap_state") == "outcome_unknown":
                return []
            selected, _delay = select_pending(
                list(self.state.get("messages", {}).values()),
                forced_single_message, explicit_routing_mode,
                quiet_seconds=quiet_window_seconds, max_messages=max_messages,
                merge_window_seconds=merge_window_seconds,
                parallel=parallel_message,
            )
            if not selected:
                return []
            batch_id = "batch-" + hashlib.sha256(
                "\0".join(str(item["message_id"]) for item in selected).encode("utf-8")
            ).hexdigest()[:24]
            started_at = now_iso()
            for item in selected:
                item["status"] = "processing"
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item["processing_started_at"] = started_at
                item["batch_id"] = batch_id
            self.save()
            return [dict(item) for item in selected]

    def pending_wait_seconds(self) -> float | None:
        with self.lock:
            if not self.admission_open or self.pause_requested() or self.state.get("main_bootstrap_state") == "outcome_unknown":
                return None
            selected, delay = select_pending(
                list(self.state.get("messages", {}).values()),
                forced_single_message, explicit_routing_mode,
                parallel=parallel_message,
            )
            return 0.0 if selected else delay

    def finish(self, message_id: str, status: str, **fields: Any) -> None:
        with self.lock:
            item = self.state["messages"][message_id]
            item.update(fields)
            item["status"] = status
            item["updated_at"] = now_iso()
            self.save()

    def update_message(self, message_id: str, **fields: Any) -> None:
        with self.lock:
            item = self.state["messages"][message_id]
            item.update(fields)
            item["updated_at"] = now_iso()
            self.save()

    def record_run(self, message_ids: list[str], event_log: Path) -> None:
        with self.lock:
            changed = False
            for message_id in message_ids:
                item = self.state.get("messages", {}).get(message_id)
                if not isinstance(item, dict):
                    continue
                item["run_event_log"] = str(event_log)
                item["run_started_at"] = now_iso()
                item["updated_at"] = now_iso()
                changed = True
            if changed:
                self.save()

    def thread_id(self) -> str:
        return str(self.state.get("thread_id") or "")

    def set_thread_id(self, thread_id: str) -> None:
        with self.lock:
            self.state["thread_id"] = thread_id
            self.save()


def normalize_history_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": message.get("message_id"),
        "chat_id": message.get("chat_id"),
        "create_time": message.get("create_time"),
        "message_type": message.get("msg_type") or message.get("message_type"),
        "content": (message.get("body") or {}).get("content") or message.get("content"),
        "sender": message.get("sender") or {},
    }


def without_forced_marker(item: dict[str, Any]) -> str:
    text = message_text(item.get("content"))
    stripped = text.lstrip()
    if stripped.startswith(("*", "#")):
        stripped = stripped[1:].lstrip()
    return stripped


def normalized_user_text(item: dict[str, Any]) -> str:
    stripped = without_forced_marker(item)
    if stripped.startswith("/doc"):
        stripped = stripped[4:].lstrip()
    elif stripped.startswith("/direct"):
        stripped = stripped[7:].lstrip()
    return stripped


def forced_single_message(item: dict[str, Any]) -> bool:
    return message_text(item.get("content")).lstrip().startswith("*")


def parallel_message(item: dict[str, Any]) -> bool:
    return message_text(item.get("content")).lstrip().startswith("#")


def explicit_routing_mode(item: dict[str, Any]) -> str:
    text = message_text(item.get("content")).lstrip()
    if text.startswith(("*", "#")):
        text = text[1:].lstrip()
    if text.startswith("/doc"):
        return "doc"
    if text.startswith("/direct"):
        return "direct"
    return "default"


def combined_batch_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        raise ValueError("A Codex batch must contain at least one message")
    if len(items) == 1:
        item = dict(items[0])
        item["content"] = json.dumps({"text": without_forced_marker(item)}, ensure_ascii=False)
        return item
    sections = []
    for index, item in enumerate(items, start=1):
        text = normalized_user_text(item) or "[消息无可提取文本，请结合对应附件处理]"
        sections.append(f"### 待处理消息 {index}\nmessage_id: {item['message_id']}\n{text}")
    modes = {explicit_routing_mode(item) for item in items}
    directive = "/doc\n" if "doc" in modes else "/direct\n" if "direct" in modes else ""
    combined = (
        directive
        + "以下消息在上一轮处理期间连续积压，现作为一个批次处理。完整响应全部请求；"
        "语义相近的内容合并回答，明显不同的内容分节独立回答，不要遗漏任何 message_id。\n\n"
        + "\n\n".join(sections)
    )
    item = dict(items[-1])
    item["content"] = json.dumps({"text": combined}, ensure_ascii=False)
    item["batch_message_ids"] = [entry["message_id"] for entry in items]
    return item


def project_workspace_mode(project: dict[str, Any]) -> str:
    return "general" if str(project.get("workspace_mode") or "project").casefold() == "general" else "project"


def project_hourly_catch_up_enabled(project: dict[str, Any]) -> bool:
    return project.get("hourly_catch_up_enabled") is not False


def project_language(project: dict[str, Any]) -> str:
    value = str(project.get("language") or "zh-CN").casefold()
    return "en" if value.startswith("en") else "zh-CN"


def project_permission_mode(project: dict[str, Any]) -> str:
    mode = str(project.get("agent_permission_mode") or DEFAULT_PERMISSION_MODE).strip()
    if mode not in PERMISSION_MODES:
        raise GatewayError(f"Unsupported remote permission mode: {mode}")
    return mode


def hourly_catch_up_project_keys(workers: dict[str, Any]) -> list[str]:
    return [key for key, worker in workers.items() if project_hourly_catch_up_enabled(worker.project)]


def scheduled_catch_up_project_keys(workers: dict[str, Any], *, reconnected: bool) -> list[str]:
    return list(workers) if reconnected else hourly_catch_up_project_keys(workers)


def attach_reconnect_reconciliation(client: Any, signal: threading.Event) -> bool:
    """Wake the non-blocking reconciliation worker after an SDK reconnect."""
    if not hasattr(client, "on_reconnected"):
        return False
    client.on_reconnected = signal.set
    return True


def build_prompt(
    project_key: str,
    project: dict[str, Any],
    item: dict[str, Any],
    attachments: list[Path],
    *,
    first_turn: bool,
) -> str:
    text = message_text(item.get("content"))
    if text.startswith("/doc"):
        text = text[4:].lstrip()
    elif text.startswith("/direct"):
        text = text[7:].lstrip()
    attachment_lines = "\n".join(f"- {path}" for path in attachments) or "- 无"
    workspace_mode = project_workspace_mode(project)
    workspace_label = "General 日常问答" if workspace_mode == "general" else "本地项目"
    permission_mode = project_permission_mode(project)
    initial = ""
    if first_turn and workspace_mode == "general":
        initial = "这是独立的 General 问答线程；内部运行目录仅用于隔离临时文件，不代表本地项目。不要扫描或猜测其他目录。"
    elif first_turn:
        initial = "首次运行时先读取当前 Working Directory 的 AGENTS.md、README 和与问题直接相关的持久文档。"
    scope_rule = (
        "直接处理用户的日常问答和轻量任务。仅使用当前消息、附件、既有线程上下文和内部隔离目录；不要扫描、推断或修改其他本地项目。"
        if workspace_mode == "general"
        else "直接处理用户请求；需要修改或运行时实际完成，不要只给方案。使用当前 Working Directory 和项目现有规则，不猜测其他目录。"
    )
    return f"""你是通过飞书为本地项目提供服务的远程 Codex。当前注册信息：

- project_key: {project_key}
- 工作模式: {workspace_label}
- working_directory: {project['working_directory']}
- 工作重点: {project.get('focus') or '按用户当前消息确定'}
- 远程 Codex 模型: {project_agent_model(project)}
- 推理等级: {project.get('agent_reasoning_effort') or '继承用户级 Codex 配置'}
- 加速档位: {project.get('agent_service_tier') or '继承用户级 Codex 配置'}
- 执行权限: {permission_mode}
- 对应桌面对话仅作主题标识: {project.get('bootstrap_source_thread_id') or '无'}
- Feishu message_id: {item['message_id']}

{initial}

执行规则：
1. {scope_rule}
2. Full Access 是本地 Codex 进程的技术执行能力，不扩大任务范围。普通操作自动执行，不等待工具审批；仍只处理当前绑定项目，不访问无关项目。不要自行创建命令白名单。
3. 已配置 Codex 账号的正常额度、模型、推理等级、Fast 和 heartbeat 属于已授权的 Codex 使用；只有新购或升级飞书会员、云资源、独立 API 账单等外部付费生命周期，才需先说明费用、续费和生命周期并等待批准。
4. 禁止公网暴露对话、文档、链接、存储或服务。
5. 常规项目内权限和安全配置可以自动处理并在回复中说明；账号/租户管理员提权、跨项目授权、凭据泄露、关闭 MFA/审计/Defender/防火墙、移除最后恢复管理员、越界持久提权必须停下并告诉用户。
6. 不删除飞书对话、生成文档或预览产物。普通项目编辑所需的文件删除可正常完成；批量删除、越界删除和不可恢复历史破坏必须停下确认。
7. 所有用户可见结论、问题、失败和下一步都写入最终回复。不要要求用户去 Codex Desktop 对账。不要自行调用飞书发布工具；网关会负责回复路由。
8. 不自动把桌面聊天镜像到飞书；只有用户明确查询桌面聊天内容时，才运行 `{sys.executable} {SCRIPT_DIR / 'remote_gateway.py'} desktop-context --project-key {project_key}`，并仅使用精确绑定到当前 working_directory 的结果回答。
9. 飞书聊天也不自动显示到桌面；桌面端以后要续接飞书任务时，由桌面 Codex 按需运行同一脚本的 `feishu-context --project-key {project_key}` 补齐近期上下文。
10. Reply in the language of the user's current message: Chinese for Chinese, English for English, and the explicitly requested language when specified. Never reduce Chinese output quality merely because English is also supported.

用户消息：
{text or '[消息无可提取文本，请结合附件处理]'}

本地附件：
{attachment_lines}
"""


def extract_thread_id(event_log: Path) -> str:
    with event_log.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
    return ""


def build_codex_command(
    project: dict[str, Any],
    final_path: Path,
    attachments: list[Path],
    thread_id: str,
) -> list[str]:
    command = [
        str(codex_cli_path()),
        "exec",
        "--skip-git-repo-check",
        "--disable",
        "hooks",
        "-C",
        str(project["working_directory"]),
        "--json",
        "-o",
        str(final_path),
    ]
    permission_mode = project_permission_mode(project)
    if permission_mode == DEFAULT_PERMISSION_MODE:
        command.extend(
            [
                "--sandbox",
                "danger-full-access",
                "--config",
                'approval_policy="never"',
            ]
        )
    elif permission_mode == PROJECT_ONLY_PERMISSION_MODE:
        command.extend(
            [
                "--sandbox",
                "workspace-write",
                "--config",
                'approval_policy="never"',
                "--config",
                "sandbox_workspace_write.network_access=true",
                "--add-dir",
                str(SCRIPT_DIR.parent),
            ]
        )
    else:
        command.append("--approve-for-me")
    model = project_agent_model(project)
    reasoning_effort = str(project.get("agent_reasoning_effort") or "").strip()
    service_tier = str(project.get("agent_service_tier") or "").strip()
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--config", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
    if service_tier:
        command.extend(["--config", "service_tier=" + json.dumps(service_tier)])
        if service_tier == "fast":
            command.extend(["--config", "features.fast_mode=true"])
    images = [path for path in attachments if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}]
    if thread_id:
        command.extend(["resume", thread_id])
        for path in images:
            command.extend(["--image", str(path)])
        command.append("-")
    else:
        for path in images:
            command.extend(["--image", str(path)])
        command.append("-")
    return command


def run_codex(project_key: str, project: dict[str, Any], store: ProjectStore, item: dict[str, Any], attachments: list[Path]) -> tuple[str, str, Path]:
    run_dir = store.root / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=True)
    final_path = run_dir / "final.md"
    event_log = run_dir / "events.jsonl"
    message_ids = [str(value) for value in (item.get("batch_message_ids") or [item.get("message_id")]) if value]
    store.record_run(message_ids, event_log)
    thread_id = str(item.get("branch_thread_id") or store.thread_id())
    command = build_codex_command(project, final_path, attachments, thread_id)
    prompt = build_prompt(
        project_key,
        project,
        item,
        attachments,
        first_turn=not bool(thread_id),
    )
    if item.get("branch_thread_id"):
        prompt = (
            "本轮是井号触发的一次性旁支，只处理本条消息。继承主线快照但不改变主线归属；"
            "后续普通消息仍回主线。\n\n" + prompt
        )
    with event_log.open("wb") as output:
        result = run_model_process(
            command,
            input=prompt.encode("utf-8"),
            store=store,
            message_ids=message_ids,
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=project["working_directory"],
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    new_thread_id = extract_thread_id(event_log) or thread_id
    if item.get("branch_thread_id") and new_thread_id != thread_id:
        raise GatewayError("Codex returned a different branch thread ID")
    if terminal_capacity_failure(event_log):
        if new_thread_id and not item.get("branch_thread_id"):
            store.set_thread_id(new_thread_id)
        raise ProviderCapacityError(event_log)
    if result.returncode != 0:
        raise GatewayError(f"Codex exited with code {result.returncode}; log={event_log}")
    if not new_thread_id:
        raise GatewayError(f"Codex returned no persistent thread ID; log={event_log}")
    answer = final_path.read_text(encoding="utf-8").strip()
    if not answer:
        raise GatewayError("Codex returned an empty final answer")
    return answer, new_thread_id, final_path


def run_codex_batch(
    project_key: str,
    project: dict[str, Any],
    store: ProjectStore,
    items: list[dict[str, Any]],
    attachments: list[Path],
) -> tuple[str, str, Path]:
    return run_codex(project_key, project, store, combined_batch_item(items), attachments)


def should_publish_document(source_text: str, answer: str) -> bool:
    if source_text.lstrip().startswith("/doc"):
        return True
    if source_text.lstrip().startswith("/direct"):
        return False
    structural = answer.count("```") >= 4 or bool(re.search(r"(?m)^\|.+\|\s*$", answer))
    return structural or len(answer) > 3500


IMAGE_ARTIFACT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
NATIVE_MESSAGE_FILE_SUFFIXES = {".pdf", ".xls", ".xlsx"}
FILE_ARTIFACT_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".tsv",
    ".ppt", ".pptx", ".zip", ".txt", ".json",
}
MARKDOWN_LINK_RE = re.compile(r"(!?)\[[^\]\r\n]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))[^)]*\)")


def extract_local_artifacts(answer: str, working_directory: str | Path) -> list[Path]:
    """Return explicit Markdown-linked artifacts that are safe to publish.

    Only existing files below the bound working directory are accepted. This
    prevents an answer from turning an arbitrary local path into an upload.
    """
    root = Path(working_directory).expanduser().resolve()
    artifacts: list[Path] = []
    seen: set[str] = set()
    for match in MARKDOWN_LINK_RE.finditer(answer):
        is_image = bool(match.group(1))
        raw = urllib.parse.unquote((match.group(2) or match.group(3) or "").strip())
        if not raw or raw.startswith(("http://", "https://", "data:", "mailto:")):
            continue
        if raw.startswith("file:///"):
            raw = raw[8:]
        # Codex renders Windows absolute Markdown targets as `/C:/...`.
        # pathlib treats that spelling as a drive-less rooted path on Windows,
        # so normalize it before resolving and enforcing the allowed root.
        if re.match(r"^/[A-Za-z]:[\\/]", raw):
            raw = raw[1:]
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            candidate = candidate.resolve(strict=True)
            candidate.relative_to(root)
        except (FileNotFoundError, OSError, ValueError):
            continue
        suffix = candidate.suffix.lower()
        if not candidate.is_file():
            continue
        if is_image and suffix not in IMAGE_ARTIFACT_SUFFIXES:
            continue
        if not is_image and suffix not in FILE_ARTIFACT_SUFFIXES:
            continue
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            artifacts.append(candidate)
    return artifacts


def chunks(text: str, limit: int = 3000) -> list[str]:
    result: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        index = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit), rest.rfind("。", 0, limit))
        if index < limit // 2:
            index = limit
        result.append(rest[:index].strip())
        rest = rest[index:].strip()
    if rest:
        result.append(rest)
    return result


def publish_document(project: dict[str, Any], source: Path, artifacts: list[Path] | None = None) -> dict[str, Any]:
    title = f"{project.get('chat_name') or 'Codex 远程结果'} - {datetime.now().strftime('%Y-%m-%d %H%M')}"
    command = [sys.executable, str(PUBLISHER), "publish", "--title", title, "--source", str(source)]
    for artifact in artifacts or []:
        command.extend(["--embed", str(artifact)])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False)
    if result.returncode != 0:
        raise GatewayError(f"Feishu document publish failed: {result.stdout}\n{result.stderr}")
    payload = json.loads(result.stdout)
    links: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"url", "link"} and isinstance(child, str) and child.startswith("http"):
                    links.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    doc_links = [link for link in links if "/docx/" in link or "/wiki/" in link]
    if not doc_links:
        doc_links = [link for link in links if "/drive/" in link]
    if not doc_links:
        raise GatewayError(f"Publisher returned no Feishu link: {payload}")
    fallback_artifacts: list[dict[str, str]] = []
    for artifact in payload.get("embedded_artifacts") or []:
        if artifact.get("mode") == "separate_file" and artifact.get("url"):
            fallback_artifacts.append({
                "name": str(artifact.get("name") or "artifact"),
                "url": str(artifact["url"]),
            })
    return {
        "document_url": doc_links[0],
        "artifacts": fallback_artifacts,
        "content_validation": payload.get("content_validation") or {},
    }


def welcome_message(project: dict[str, Any]) -> str:
    working_directory = str(project["working_directory"])
    workspace_mode = project_workspace_mode(project)
    hourly_enabled = project_hourly_catch_up_enabled(project)
    if project_language(project) == "en":
        binding = (
            "This group is bound to an isolated General workspace and cannot access other projects. "
            "You may rename the group without affecting the connection."
            if workspace_mode == "general"
            else f"This group is bound to local project `{Path(working_directory).name or working_directory}`. "
            "You may rename the group without affecting the connection."
        )
        inspection = (
            "**Automatic inspection runs hourly by default** while the computer and Codex are available."
            if hourly_enabled
            else "**Automatic inspection is disabled**; real-time delivery and reconnect recovery remain available."
        )
        return (
            "**Connected — you can start now**\n\n"
            f"{binding}\n\n"
            "- Send tasks consecutively; queued messages may be combined, or start one with `*` to handle it separately\n"
            "- Start with `#` for one parallel message with the main context; later ordinary messages stay on the main conversation\n"
            "- If you sent something wrong, send a new correction; editing or recalling is not treated as correction or cancellation\n"
            "- Offline messages stay in Feishu and are processed in order later\n"
            "- Simple results reply directly; complex results can use private Feishu documents\n"
            "- Want different behavior or a new feature? Tell Codex directly; each group can be customized separately\n"
            "- Say “remind me every hour starting tomorrow at 10” or “done, cancel the reminder”\n\n"
            f"{inspection}\n"
            "Later inspections use three lines for message status, Token plan usage, and a customization tip. "
            "Ask in natural language to change the content or cadence, pause it, or resume it."
        )
    binding = (
        "本群已绑定一个独立的 General 工作区，不会访问其他项目。群名可以随时修改，不影响连接。"
        if workspace_mode == "general"
        else f"本群已绑定本地项目 `{Path(working_directory).name or working_directory}`。群名可以随时修改，不影响连接。"
    )
    inspection = (
        "**自动巡检默认每小时运行一次**：在电脑和 Codex 可用时检查 Listener、补拉遗漏并处理积压。"
        if hourly_enabled
        else "**自动巡检当前已关闭**：实时接收与启动/重连补漏仍然保留。"
    )
    return (
        "**已连接，可以直接使用**\n\n"
        f"{binding}\n\n"
        "- 直接连续发送任务；积压消息可能合并处理，必须单独处理时在开头加 `*`\n"
        "- 开头加 `#`：继承主线上下文，独立并行处理本条；后续普通消息仍回主线\n"
        "- 发错时请另发一条更正；编辑或撤回不会被当作更正或取消指令\n"
        "- 电脑离线时消息留在飞书，上线后按顺序处理\n"
        "- 简单结果直接回复，复杂结果可生成私有飞书文档\n"
        "- 想调整现有行为或增加功能，直接告诉 Codex；不同群可以分别定制\n"
        "- 直接说“从明天 10 点开始每小时提醒我……”或“已完成，取消提醒”\n\n"
        f"{inspection}\n"
        "后续默认用三句报告消息状态、Token plan 用量和定制提示；"
        "直接告诉我修改巡检内容或频率、暂停或恢复即可。"
    )


def send_welcome(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    project = config["projects"].get(args.project_key)
    if not isinstance(project, dict):
        raise GatewayError(f"Unknown remote project: {args.project_key}")
    chat_id = str(project.get("chat_id") or "")
    if not chat_id:
        raise GatewayError(f"Remote project has no Feishu chat binding: {args.project_key}")
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to send a welcome message in an unexpected tenant")
    text = welcome_message(project)
    request_uuid = "codex-remote-welcome-" + hashlib.sha256(
        f"{chat_id}\0{text}".encode("utf-8")
    ).hexdigest()[:28]
    payload = client.send_post(chat_id, text, request_uuid)
    message_id = str((payload.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise GatewayError(f"Welcome message returned no message_id: {payload}")
    return {"ok": True, "project_key": args.project_key, "message_id": message_id}


def inspection_counts(project_key: str) -> dict[str, int]:
    state = load_json(project_runtime(project_key) / "state.json", {})
    counts: dict[str, int] = {}
    for item in (state.get("messages") or {}).values():
        status_name = str(item.get("status") or "unknown")
        counts[status_name] = counts.get(status_name, 0) + 1
    return counts


def first_inspection_text(project_key: str, language: str = "zh-CN") -> str:
    counts = inspection_counts(project_key)
    active = counts.get("pending", 0) + counts.get("processing", 0)
    progress = active_task_progress_snapshot(project_key, language=language)
    if language == "en":
        if progress:
            current = (
                f"Listener ready | task running for {progress['elapsed']}; "
                f"latest progress: {_one_line_progress_text(progress['detail'])}."
            )
        else:
            current = (
                f"Listener ready | {active} queued message(s) found; processing has been triggered."
                if active
                else "Listener ready | no queued messages."
            )
        return (
            "**First automatic inspection completed**\n\n"
            "It runs hourly by default while the computer and Codex are available, starts or checks the Listener, "
            "recovers missed messages, and continues queued work.\n\n"
            f"Current status: {current}\n"
            "Later unchanged runs use only three lines: message status, Token usage, and a natural-language customization tip. "
            "Ask to pause, resume, change the inspection, or add a reminder.\n\n"
            "Each inspection starts a lightweight Codex run and consumes the corresponding Codex usage."
        )
    if progress:
        current = (
            f"Listener 已就绪｜任务执行中，已运行 {progress['elapsed']}；"
            f"最近进展：{_one_line_progress_text(progress['detail'])}。"
        )
    else:
        current = (
            f"Listener 已就绪｜发现 {active} 条待处理消息，已经触发处理。"
            if active
            else "Listener 已就绪｜当前没有待处理消息。"
        )
    return (
        "**自动巡检首次运行成功**\n\n"
        "默认每小时运行一次。在电脑和 Codex 可用时，它会检查并按需启动 Listener、"
        "补拉遗漏消息并继续处理积压。\n\n"
        f"本次状态：{current}\n"
        "后续默认每次只发送三句：消息状态、Token 用量和自然语言定制提示。"
        "你可以直接要求暂停、恢复或调整巡检，也可以按你的描述设置备忘提醒。\n\n"
        "自动巡检会发起一次轻量 Codex 运行并消耗相应的 Codex 使用额度。"
    )


def send_first_inspection_message(config: dict[str, Any], project_key: str) -> dict[str, Any]:
    project = config["projects"].get(project_key)
    if not isinstance(project, dict) or not project.get("chat_id"):
        raise GatewayError(f"Remote project has no Feishu chat binding: {project_key}")
    marker = project_runtime(project_key) / "inspection.json"
    recorded = load_json(marker, {})
    if recorded.get("first_message_sent"):
        return {"ok": True, "skipped": True, "message_id": recorded.get("message_id")}
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to send inspection status in an unexpected tenant")
    text = first_inspection_text(project_key, project_language(project))
    request_uuid = "codex-inspection-first-" + hashlib.sha256(
        f"{project['chat_id']}\0v1".encode("utf-8")
    ).hexdigest()[:24]
    payload = client.send_post(str(project["chat_id"]), text, request_uuid)
    message_id = str((payload.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise GatewayError(f"First inspection message returned no message_id: {payload}")
    atomic_write_json(
        marker,
        {
            "first_message_sent": True,
            "message_id": message_id,
            "sent_at": now_iso(),
            "version": 1,
        },
    )
    return {"ok": True, "skipped": False, "message_id": message_id, "text": text}


def routine_inspection_text(project_key: str, language: str = "zh-CN") -> str:
    counts = inspection_counts(project_key)
    failures = counts.get("failed", 0) + counts.get("provider_failed", 0)
    progress = active_task_progress_snapshot(project_key, language=language)
    if language == "en":
        if progress:
            status = (
                f"Status: Listener healthy; task running for {progress['elapsed']}; "
                f"latest progress: {_one_line_progress_text(progress['detail'])}; "
                f"pending {counts.get('pending', 0)}, processing {counts.get('processing', 0)}, "
                f"failed {failures}."
            )
        else:
            status = (
                "Status: Listener healthy; "
                f"pending {counts.get('pending', 0)}, processing {counts.get('processing', 0)}, "
                f"failed {failures}."
            )
        try:
            usage = format_codex_usage(codex_rate_limits(), "en")
        except Exception:
            usage = "Codex usage: temporarily unavailable"
        usage = usage.rstrip(".") + "."
        tip = "Tip: use natural language to change this inspection, add reminders, or create other custom behavior."
        return "\n".join((status, usage, tip))
    if progress:
        status = (
            f"状态：Listener 正常，任务执行中，已运行 {progress['elapsed']}；"
            f"最近进展：{_one_line_progress_text(progress['detail'])}；"
            f"待处理 {counts.get('pending', 0)}，处理中 {counts.get('processing', 0)}，"
            f"失败 {failures}。"
        )
    else:
        status = (
            "状态：Listener 正常，"
            f"待处理 {counts.get('pending', 0)}，处理中 {counts.get('processing', 0)}，"
            f"失败 {failures}。"
        )
    try:
        usage = format_codex_usage(codex_rate_limits())
    except Exception:
        usage = "Codex 用量：暂时无法读取"
    usage = usage.rstrip("。") + "。"
    tip = "提示：可以直接用自然语言修改巡检内容、设置提醒或增加其他用法。"
    return "\n".join((status, usage, tip))


def inspection_report_uuid(project_key: str, at: datetime | None = None) -> str:
    bucket = (at or datetime.now().astimezone()).astimezone().strftime("%Y%m%d%H")
    digest = hashlib.sha256(f"{project_key}\0{bucket}\0default-inspection".encode("utf-8")).hexdigest()[:22]
    return "codex-inspection-" + digest


def send_routine_inspection_message(config: dict[str, Any], project_key: str) -> dict[str, Any]:
    project = config["projects"].get(project_key)
    if not isinstance(project, dict) or not project.get("chat_id"):
        raise GatewayError(f"Remote project has no Feishu chat binding: {project_key}")
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to send inspection status in an unexpected tenant")
    text = routine_inspection_text(project_key, project_language(project))
    payload = client.send_post(
        str(project["chat_id"]),
        text,
        inspection_report_uuid(project_key),
    )
    message_id = str((payload.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise GatewayError(f"Inspection report returned no message_id: {payload}")
    return {"ok": True, "kind": "routine", "message_id": message_id, "text": text}


def send_default_inspection_message(config: dict[str, Any], project_key: str) -> dict[str, Any]:
    first = send_first_inspection_message(config, project_key)
    if not first.get("skipped"):
        return {**first, "kind": "first"}
    return send_routine_inspection_message(config, project_key)


def upload_inline_images(
    client: FeishuClient,
    artifacts: list[Path],
    allowed_root: str | Path,
    log_path: Path,
) -> tuple[list[tuple[Path, str]], list[Path]]:
    uploaded: list[tuple[Path, str]] = []
    failed: list[Path] = []
    for artifact in artifacts:
        if artifact.suffix.lower() not in IMAGE_ARTIFACT_SUFFIXES:
            continue
        try:
            uploaded.append((artifact, client.upload_message_image(artifact, allowed_root)))
        except Exception:
            failed.append(artifact)
            append_log(log_path, f"direct image upload failed for {artifact}:\n" + traceback.format_exc())
    return uploaded, failed


def reply_inline_images(
    client: FeishuClient,
    message_id: str,
    uploaded: list[tuple[Path, str]],
    base_uuid: str,
    log_path: Path,
) -> list[Path]:
    failed: list[Path] = []
    for index, (artifact, image_key) in enumerate(uploaded, start=1):
        try:
            client.reply_image(message_id, image_key, f"{base_uuid[:35]}-img-{index}")
        except Exception:
            failed.append(artifact)
            append_log(log_path, f"direct image reply failed for {artifact}:\n" + traceback.format_exc())
    return failed


def upload_inline_files(
    client: FeishuClient,
    artifacts: list[Path],
    allowed_root: str | Path,
    log_path: Path,
) -> tuple[list[tuple[Path, str]], list[Path]]:
    uploaded: list[tuple[Path, str]] = []
    failed: list[Path] = []
    for artifact in artifacts:
        if artifact.suffix.lower() not in NATIVE_MESSAGE_FILE_SUFFIXES:
            continue
        try:
            uploaded.append((artifact, client.upload_message_file(artifact, allowed_root)))
        except Exception:
            failed.append(artifact)
            append_log(log_path, f"direct file upload failed for {artifact}:\n" + traceback.format_exc())
    return uploaded, failed


def reply_inline_files(
    client: FeishuClient,
    message_id: str,
    uploaded: list[tuple[Path, str]],
    base_uuid: str,
    log_path: Path,
) -> list[Path]:
    failed: list[Path] = []
    for index, (artifact, file_key) in enumerate(uploaded, start=1):
        try:
            client.reply_file(message_id, file_key, f"{base_uuid[:35]}-file-{index}")
        except Exception:
            failed.append(artifact)
            append_log(log_path, f"direct file reply failed for {artifact}:\n" + traceback.format_exc())
    return failed


def send_artifact_warning(
    client: FeishuClient,
    project: dict[str, Any],
    failed: list[Path],
    base_uuid: str,
    log_path: Path,
) -> None:
    if not failed:
        return
    names = "、".join(path.name for path in failed[:5])
    if len(failed) > 5:
        names += f" 等 {len(failed)} 个文件"
    english = project_language(project) == "en"
    text = (
        f"Some attachments were not delivered ({names}). Ask Codex to retry and confirm that the attachment appears before relying on it."
        if english
        else f"部分附件未送达（{names}）。请让 Codex 重试，并在确认附件实际出现后再使用。"
    )
    try:
        client.send_post(project["chat_id"], text, f"{base_uuid[:35]}-artifact-warn")
    except Exception:
        append_log(log_path, "artifact warning delivery failed:\n" + traceback.format_exc())


def remaining_artifact_failures(failed: list[Path], delivered: list[dict[str, str]]) -> list[Path]:
    delivered_names = {
        str(item.get("name") or "").strip().casefold()
        for item in delivered
        if str(item.get("url") or "").startswith("http")
    }
    return [path for path in failed if path.name.casefold() not in delivered_names]


def reply_complete(
    client: FeishuClient,
    project: dict[str, Any],
    item: dict[str, Any],
    answer: str,
    final_path: Path,
    log_path: Path,
) -> str:
    source = message_text(item.get("content"))
    message_id = item["message_id"]
    base_uuid = "codex-remote-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    allowed_root = project.get("working_directory") or final_path.parent
    artifacts = extract_local_artifacts(answer, allowed_root)
    uploaded_images, image_upload_failures = upload_inline_images(
        client,
        artifacts,
        allowed_root,
        log_path,
    )
    uploaded_files, file_upload_failures = upload_inline_files(
        client,
        artifacts,
        allowed_root,
        log_path,
    )
    if should_publish_document(source, answer):
        try:
            published = publish_document(project, final_path, artifacts)
            link = str(published["document_url"])
            english = project_language(project) == "en"
            fallback = "The complete result is ready" if english else "完整结果已整理"
            label = "Open the full Feishu document" if english else "打开完整飞书文档"
            summary = next((line.strip("# ") for line in answer.splitlines() if line.strip()), fallback)
            text = f"{summary[:240]}\n\n[{label}]({link})"
            fallback_artifacts = published.get("artifacts") or []
            if fallback_artifacts:
                artifact_label = "Attachments" if english else "附件与产物"
                lines = [f"[{item['name']}]({item['url']})" for item in fallback_artifacts]
                text += f"\n\n{artifact_label}：" + " · ".join(lines)
            known_missing = remaining_artifact_failures(
                image_upload_failures + file_upload_failures,
                fallback_artifacts,
            )
            if known_missing:
                names = "、".join(path.name for path in known_missing[:5])
                notice = (
                    f"Attachments were not delivered ({names}); the document and text result are still available."
                    if english
                    else f"附件未送达（{names}）；飞书文档和文字结果仍可查看。"
                )
                text = notice + "\n\n" + text
            client.reply_post(message_id, text, base_uuid)
            image_reply_failures = reply_inline_images(client, message_id, uploaded_images, base_uuid, log_path)
            file_reply_failures = reply_inline_files(client, message_id, uploaded_files, base_uuid, log_path)
            failed_artifacts = remaining_artifact_failures(
                image_reply_failures + file_reply_failures,
                fallback_artifacts,
            )
            send_artifact_warning(
                client,
                project,
                failed_artifacts,
                base_uuid,
                log_path,
            )
            return link
        except Exception:
            append_log(log_path, "document publish failed; falling back to message chunks:\n" + traceback.format_exc())
    parts = chunks(answer)
    upload_failures = image_upload_failures + file_upload_failures
    if upload_failures:
        names = "、".join(path.name for path in upload_failures[:5])
        warning = (
            f"Attachments were not delivered ({names}); the text result follows.\n\n"
            if project_language(project) == "en"
            else f"附件未送达（{names}）；以下仅返回文字结果。\n\n"
        )
        parts[0] = warning + parts[0]
    client.reply_post(message_id, parts[0], base_uuid)
    image_reply_failures = reply_inline_images(client, message_id, uploaded_images, base_uuid, log_path)
    file_reply_failures = reply_inline_files(client, message_id, uploaded_files, base_uuid, log_path)
    send_artifact_warning(
        client,
        project,
        image_reply_failures + file_reply_failures,
        base_uuid,
        log_path,
    )
    for index, part in enumerate(parts[1:], start=2):
        client.send_post(project["chat_id"], part, f"{base_uuid}-{index}")
    return ""


class ProjectWorker(LifecycleWorker):
    def __init__(self, key: str, project: dict[str, Any], client: FeishuClient, log_path: Path) -> None:
        self.key = key
        self.project = project
        self.client = client
        self.log_path = log_path
        self.store = ProjectStore(key)
        self.store.recover_interrupted()
        self.signal = threading.Event()
        self.branch_signal = threading.Event()
        self.active_branches: dict[str, threading.Thread] = {}
        self.init_lifecycle()
        self.branch_thread = threading.Thread(target=self._parallel_loop, name=f"branches-{key}", daemon=True)
        self.thread = threading.Thread(target=self._loop, name=f"worker-{key}", daemon=True)

    def start(self) -> None:
        self.start_lifecycle()
        self.thread.start()
        self.branch_thread.start()
        self.branch_signal.set()
        self.signal.set()

    def enqueue(self, message: dict[str, Any]) -> bool:
        added = self.store.enqueue(message)
        if added:
            append_log(self.log_path, f"queued {self.key}:{message.get('message_id')}")
            self.signal.set()
            self.branch_signal.set()
            self.lifecycle_signal.set()
        return added

    def _archive_temporary_thread(self, thread_id: str) -> None:
        archive_thread(thread_id, codex_cli_path(), self.project["working_directory"])

    def _branch_parent(self) -> str:
        # Called under the shared store lock: first-main initialization and
        # branch bootstrap must never choose competing persistent roots.
        parent = self.store.thread_id()
        if parent:
            return parent
        rows = list(self.store.state.get("messages", {}).values())
        active = [row for row in rows if not parallel_message(row) and row.get("status") == "processing"]
        if active:
            event_path = active[0].get("run_event_log")
            if event_path and Path(event_path).is_file():
                return extract_thread_id(Path(event_path))
            return ""
        previous_runs = sorted(
            [row for row in rows if not parallel_message(row) and row.get("run_event_log")],
            key=lambda row: str(row.get("run_started_at") or row.get("processing_started_at") or ""),
            reverse=True,
        )
        if previous_runs:
            event_path = Path(previous_runs[0]["run_event_log"])
            recovered = extract_thread_id(event_path) if event_path.is_file() else ""
            if not recovered:
                raise GatewayError("Prior main initialization has no confirmed thread; preserve it for reconciliation")
            self.store.set_thread_id(recovered)
            return recovered
        if self.store.state.get("main_bootstrap_state") == "outcome_unknown":
            return ""
        if any(not parallel_message(row) and row.get("status") == "completed" for row in rows):
            raise GatewayError("Missing main thread for existing history; preserve state for reconciliation")
        codex_path = codex_cli_path()
        self.store.state["main_bootstrap_state"] = "outcome_unknown"
        self.store.save()
        try:
            parent = start_thread(
                codex_path, self.project["working_directory"],
                model=project_agent_model(self.project),
                reasoning_effort=self.project.get("agent_reasoning_effort") or None,
            )
        except Exception as exc:
            if not getattr(exc, "mutation_submitted", True):
                self.store.state.pop("main_bootstrap_state", None)
                self.store.save()
            raise
        self.store.set_thread_id(parent)
        self.store.state["main_bootstrap_state"] = "ready"
        self.store.save()
        return parent

    def dispatch_parallel(self) -> list[str]:
        """Atomically register independent one-message branches before launch."""
        launched = []
        with self.store.lock:
            if not self.store.admission_open or self.store.pause_requested():
                return launched
            rows = sorted(
                self.store.state.get("messages", {}).values(),
                key=lambda row: (int(row.get("create_time") or 0), row["message_id"]),
            )
            for row in rows:
                message_id = str(row["message_id"])
                if (not parallel_message(row) or message_id in self.active_branches
                        or row.get("status") not in {"pending", "failed"}
                        or int(row.get("attempts") or 0) >= 3
                        or row.get("branch_state") == "fork_outcome_unknown"):
                    continue
                try:
                    parent = str(row.get("branch_parent_thread_id") or self._branch_parent())
                except Exception as exc:
                    row.update(status="failed", attempts=int(row.get("attempts") or 0) + 1, error=str(exc))
                    self.store.save()
                    append_log(self.log_path, f"branch parent unavailable {self.key}:{message_id}: {exc}")
                    self._report_branch_failure(message_id)
                    continue
                if not parent:
                    continue
                previous = dict(row)
                row.update(
                    status="processing", attempts=int(row.get("attempts") or 0) + 1,
                    processing_started_at=now_iso(), batch_id="branch-" + hashlib.sha256(message_id.encode()).hexdigest()[:24],
                    branch_parent_thread_id=parent,
                )
                self.store.save()
                thread = threading.Thread(
                    target=self._run_parallel, args=(dict(row),),
                    name=f"branch-{self.key}-{message_id}", daemon=True,
                )
                self.active_branches[message_id] = thread
                try:
                    thread.start()
                except Exception:
                    self.active_branches.pop(message_id, None)
                    row.clear()
                    row.update(previous)
                    self.store.save()
                    raise
                launched.append(message_id)
        return launched

    def _run_parallel(self, item: dict[str, Any]) -> None:
        message_id = str(item["message_id"])
        try:
            if self.store.pause_requested([message_id]):
                self.store.defer_batch([message_id])
                return
            child = str(item.get("branch_thread_id") or "")
            if not child:
                codex_path = codex_cli_path()
                # Record ambiguity before the RPC; a crash or timeout must not
                # blindly fork twice or ever fall back to resuming the parent.
                self.store.update_message(message_id, branch_state="fork_outcome_unknown")
                try:
                    child = fork_thread(
                        item["branch_parent_thread_id"], codex_path, self.project["working_directory"],
                    )
                except Exception as exc:
                    if not getattr(exc, "mutation_submitted", True):
                        self.store.update_message(message_id, branch_state="not_submitted")
                    raise
                self.store.update_message(message_id, branch_state="ready", branch_thread_id=child)
            item["branch_thread_id"] = child
            self._process_batch([item])
        except Exception as exc:
            if self.store.pause_requested([message_id]):
                self.store.defer_batch([message_id])
                return
            self.store.finish(message_id, "failed", error=str(exc), failed_at=now_iso())
            append_log(self.log_path, f"branch failed {self.key}:{message_id}: {traceback.format_exc()}")
            self._report_branch_failure(message_id)
        finally:
            with self.store.lock:
                self.active_branches.pop(message_id, None)
            self.signal.set()
            self.lifecycle_signal.set()

    def _report_branch_failure(self, message_id: str) -> None:
        try:
            self.client.reply_post(
                message_id,
                "这条旁支暂未完成，原消息和上下文来源已保留，可按确认的状态重试或对账恢复。",
                "codex-branch-failed-" + hashlib.sha256(message_id.encode()).hexdigest()[:30],
            )
        except Exception:
            append_log(self.log_path, "failed to report branch failure")

    def _parallel_loop(self) -> None:
        while True:
            self.branch_signal.wait(timeout=1.0)
            self.branch_signal.clear()
            try:
                self.dispatch_parallel()
            except Exception:
                append_log(self.log_path, "branch dispatch failed: " + traceback.format_exc())

    def _download_attachments(self, item: dict[str, Any]) -> list[Path]:
        target = self.store.root / "attachments" / item["message_id"]
        paths: list[Path] = []
        for resource_type, key in resource_keys(item.get("content")):
            paths.append(self.client.download_resource(item["message_id"], resource_type, key, target))
        return paths

    @staticmethod
    def _reaction_id(payload: dict[str, Any]) -> str:
        return str(((payload.get("data") or {}).get("reaction_id")) or "").strip()

    def _start_working_reaction(self, item: dict[str, Any]) -> str:
        message_id = item["message_id"]
        existing = str(item.get("working_reaction_id") or "").strip()
        if existing and item.get("working_reaction_active"):
            return existing
        payload = self.client.add_reaction(message_id, WORKING_REACTION)
        reaction_id = self._reaction_id(payload)
        if not reaction_id:
            raise GatewayError(f"Feishu returned no reaction_id for {WORKING_REACTION}")
        self.store.update_message(
            message_id,
            working_reaction_id=reaction_id,
            working_reaction_type=WORKING_REACTION,
            working_reaction_active=True,
            working_reaction_at=now_iso(),
            reaction_sync_error=None,
        )
        return reaction_id

    def _clear_working_reaction(self, message_id: str, reaction_id: str) -> str | None:
        if not reaction_id:
            return None
        try:
            self.client.remove_reaction(message_id, reaction_id)
            self.store.update_message(
                message_id,
                working_reaction_active=False,
                working_reaction_cleared_at=now_iso(),
                reaction_sync_error=None,
            )
            return None
        except Exception as exc:
            error = str(exc)
            self.store.update_message(message_id, reaction_sync_error=error)
            append_log(self.log_path, f"working reaction cleanup failed for {message_id}: {error}")
            return error

    def _replace_working_with_completion(self, message_id: str, reaction_id: str) -> str | None:
        cleanup_error = self._clear_working_reaction(message_id, reaction_id)
        if cleanup_error:
            return cleanup_error
        try:
            payload = self.client.add_reaction(message_id, COMPLETION_REACTION)
            completion_id = self._reaction_id(payload)
            if not completion_id:
                raise GatewayError(f"Feishu returned no reaction_id for {COMPLETION_REACTION}")
            self.store.update_message(
                message_id,
                completion_reaction_id=completion_id,
                completion_reaction_type=COMPLETION_REACTION,
                completion_reaction_at=now_iso(),
                reaction_sync_error=None,
            )
            return None
        except Exception as exc:
            error = str(exc)
            self.store.update_message(message_id, reaction_sync_error=error)
            append_log(self.log_path, f"completion reaction failed for {message_id}: {error}")
            return error

    def _own_reactions(self, message_id: str, emoji_type: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.client.list_reactions(message_id, emoji_type)
            if str((item.get("operator") or {}).get("operator_type") or "").lower() == "app"
            and str((item.get("operator") or {}).get("operator_id") or "") == self.client.app_id
        ]

    def reconcile_reactions(self) -> None:
        with self.store.lock:
            items = [dict(item) for item in self.store.state.get("messages", {}).values()]
        for item in items:
            status = str(item.get("status") or "")
            if status == "processing" or status == "ignored":
                continue
            message_id = str(item.get("message_id") or "")
            if not message_id:
                continue
            try:
                typing = self._own_reactions(message_id, WORKING_REACTION)
                for reaction in typing:
                    reaction_id = str(reaction.get("reaction_id") or "")
                    if reaction_id:
                        self.client.remove_reaction(message_id, reaction_id)
                if typing:
                    self.store.update_message(
                        message_id,
                        working_reaction_active=False,
                        working_reaction_cleared_at=now_iso(),
                    )
                checks = self._own_reactions(message_id, COMPLETION_REACTION)
                if status == "completed" and not checks:
                    payload = self.client.add_reaction(message_id, COMPLETION_REACTION)
                    completion_id = self._reaction_id(payload)
                    if completion_id:
                        self.store.update_message(
                            message_id,
                            completion_reaction_id=completion_id,
                            completion_reaction_type=COMPLETION_REACTION,
                            completion_reaction_at=now_iso(),
                            reaction_sync_error=None,
                        )
                elif status != "completed":
                    for reaction in checks:
                        reaction_id = str(reaction.get("reaction_id") or "")
                        if reaction_id:
                            self.client.remove_reaction(message_id, reaction_id)
            except Exception as exc:
                self.store.update_message(message_id, reaction_sync_error=str(exc))
                append_log(self.log_path, f"reaction reconciliation failed for {message_id}: {exc}")

    def _process(self, item: dict[str, Any]) -> None:
        self._process_batch([item])

    def _process_batch(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with self.store.lock:
            if any(self.store.state.get("messages", {}).get(str(item["message_id"]), {}).get("status") != "processing" for item in items):
                raise GatewayError("Only the atomically claimed active batch may start processing")
        message_ids = [str(item["message_id"]) for item in items]
        reply_item = combined_batch_item(items)
        working_reactions: dict[str, str] = {}
        try:
            if self.store.pause_requested(message_ids):
                raise MaintenancePaused("System upgrade; original request retained")
            attachments = [path for item in items for path in self._download_attachments(item)]
            for item in items:
                message_id = str(item["message_id"])
                try:
                    working_reactions[message_id] = self._start_working_reaction(item)
                except Exception as exc:
                    working_reactions[message_id] = ""
                    append_log(self.log_path, f"ack reaction failed for {message_id}: {exc}")
            if len(items) == 1:
                answer, thread_id, final_path = run_codex(
                    self.key, self.project, self.store, reply_item, attachments
                )
            else:
                answer, thread_id, final_path = run_codex_batch(
                    self.key, self.project, self.store, items, attachments
                )
            if parallel_message(items[0]):
                self.store.update_message(items[0]["message_id"], branch_thread_id=thread_id)
            else:
                self.store.set_thread_id(thread_id)
            if self.store.pause_requested(message_ids):
                raise MaintenancePaused("System upgrade; completed output retained")
            document_link = reply_complete(
                self.client,
                self.project,
                reply_item,
                answer,
                final_path,
                self.log_path,
            )
            answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
            completed_at = now_iso()
            for item in items:
                message_id = str(item["message_id"])
                reaction_error = self._replace_working_with_completion(
                    message_id, working_reactions.get(message_id, "")
                )
                self.store.finish(
                    message_id,
                    "completed",
                    completed_at=completed_at,
                    answer_sha256=answer_sha256,
                    document_link=document_link,
                    batch_message_ids=message_ids,
                    reply_source_message_id=reply_item["message_id"],
                    reaction_sync_error=reaction_error,
                )
            append_log(
                self.log_path,
                f"completed {self.key}:{','.join(message_ids)}; thread={thread_id}",
            )
        except Exception as exc:
            if isinstance(exc, MaintenanceStopFailed):
                for message_id in message_ids:
                    self.store.update_message(message_id, maintenance_stop_error=str(exc))
                append_log(self.log_path, f"maintenance stop failed: {','.join(message_ids)}")
                return
            if isinstance(exc, MaintenancePaused) or self.store.pause_requested(message_ids):
                for item in items:
                    message_id = str(item["message_id"])
                    self._clear_working_reaction(message_id, working_reactions.get(message_id, ""))
                    event_path = self.store.state["messages"][message_id].get("run_event_log")
                    if not parallel_message(item) and event_path:
                        recovered = extract_thread_id(Path(event_path))
                        if recovered:
                            self.store.set_thread_id(recovered)
                self.store.defer_batch(message_ids)
                self.lifecycle_signal.set()
                return
            if isinstance(exc, ProviderCapacityError):
                text = (
                    "Codex ended this turn with a model capacity error (at capacity). "
                    "The request and any saved work are retained. Please try again later; "
                    "the gateway will not rerun it or change models automatically."
                    if self.project.get("language") == "en" else
                    "Codex 本轮最终仍返回模型容量不足（at capacity）。原消息和已保存的工作都保留；"
                    "网关不会额外重复执行或切换模型，稍后可以重试。"
                )
                self.store.fail_provider_capacity(message_ids, exc.event_log, text)
                for message_id in message_ids:
                    self._clear_working_reaction(message_id, working_reactions.get(message_id, ""))
                self.lifecycle_signal.set()
                return
            trace = traceback.format_exc()
            append_log(self.log_path, f"failed {self.key}:{','.join(message_ids)}: {trace}")
            failed_at = now_iso()
            for item in items:
                message_id = str(item["message_id"])
                attempts = int(self.store.state["messages"][message_id].get("attempts") or 0)
                reaction_error = self._clear_working_reaction(
                    message_id, working_reactions.get(message_id, "")
                )
                self.store.finish(
                    message_id,
                    "failed",
                    error=str(exc),
                    failed_at=failed_at,
                    batch_message_ids=message_ids,
                    reaction_sync_error=reaction_error,
                )
                if attempts >= 3:
                    try:
                        self.client.reply_post(
                            message_id,
                            "这条消息已保留，但连续处理失败三次。我已经停止自动重试，需要检查本地网关日志。",
                            "codex-remote-failed-" + hashlib.sha256(message_id.encode()).hexdigest()[:32],
                        )
                    except Exception:
                        append_log(self.log_path, "failed to report terminal failure")
        finally:
            self.lifecycle_signal.set()

    def _loop(self) -> None:
        while True:
            self.signal.wait()
            self.signal.clear()
            while True:
                items = self.store.next_pending_batch()
                if not items:
                    delay = self.store.pending_wait_seconds()
                    if delay is not None:
                        self.signal.wait(timeout=delay)
                        self.signal.clear()
                        continue
                    break
                self._process_batch(items)


class GatewayService:
    def __init__(self, config: dict[str, Any]) -> None:
        assert_config(config)
        self.config = config
        app_id, secret = load_app_credentials(config)
        self.client = FeishuClient(app_id, secret)
        tenant = self.client.tenant_info()
        actual_tenant = str(tenant.get("tenant_key") or "")
        expected_tenant = str(config["expected_tenant_key"])
        if actual_tenant != expected_tenant:
            raise GatewayError(f"Wrong Feishu tenant: expected {expected_tenant}, got {actual_tenant}")
        self.client._verified_tenant_key = actual_tenant
        self.log_path = REMOTE_STATE / "logs" / "gateway.log"
        self.reconcile_lock = threading.Lock()
        self.workers: dict[str, ProjectWorker] = {}
        self.chat_to_key: dict[str, str] = {}
        for key, project in config["projects"].items():
            chat_id = str(project.get("chat_id") or "")
            if not chat_id:
                continue
            path = Path(project["working_directory"]).resolve()
            if not path.is_dir():
                raise GatewayError(f"Working directory does not exist: {path}")
            project["working_directory"] = str(path)
            worker = ProjectWorker(key, project, self.client, self.log_path)
            self.workers[key] = worker
            self.chat_to_key[chat_id] = key

    def start_workers(self) -> None:
        for worker in self.workers.values():
            worker.reconcile_reactions()
            worker.start()

    def enqueue(self, message: dict[str, Any], tenant_key: str = "") -> bool:
        chat_id = str(message.get("chat_id") or "")
        key = self.chat_to_key.get(chat_id)
        if not key:
            return False
        if tenant_key and tenant_key != str(self.config["expected_tenant_key"]):
            return False
        return self.workers[key].enqueue(message)

    def catch_up(
        self,
        project_keys: list[str] | None = None,
        *,
        continue_on_error: bool = False,
        log_when_empty: bool = True,
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        errors: dict[str, str] = {}
        # Manual sync, reconnect, periodic repair and graceful reload can race.
        # Serialize only the bounded Feishu history calls; never hold this lock
        # while Codex processes a task.
        with self.reconcile_lock:
            for key, worker in self.workers.items():
                if project_keys is not None and key not in project_keys:
                    continue
                try:
                    cursor = int(
                        worker.store.state.get("cursor")
                        or worker.project.get("registered_epoch")
                        or now_epoch()
                    )
                    messages = self.client.list_messages(worker.project["chat_id"], max(0, cursor - 2))
                    added = 0
                    for message in messages:
                        added += int(worker.enqueue(normalize_history_message(message)))
                    result[key] = added
                except Exception as exc:
                    if not continue_on_error:
                        raise
                    errors[key] = f"{type(exc).__name__}: {exc}"
        if log_when_empty or any(result.values()) or errors:
            append_log(self.log_path, f"catch-up complete: {result}; errors={errors}")
        return result

    def process_sync_requests(self) -> None:
        SYNC_REQUESTS.mkdir(parents=True, exist_ok=True)
        SYNC_RESPONSES.mkdir(parents=True, exist_ok=True)
        while True:
            for request_path in sorted(SYNC_REQUESTS.glob("*.json")):
                request_id = request_path.stem
                response_path = SYNC_RESPONSES / f"{request_id}.json"
                try:
                    request = load_json(request_path, {})
                    project_keys = list(request.get("project_keys") or [])
                    unknown = [key for key in project_keys if key not in self.workers]
                    if not project_keys or unknown:
                        raise GatewayError(f"Invalid sync request project keys: {project_keys}")
                    if request.get("maintenance_action"):
                        if len(project_keys) != 1:
                            raise GatewayError("Maintenance must target exactly one project")
                        worker = self.workers[project_keys[0]]
                        result = worker.maintenance_action(
                            request["maintenance_action"], request_id,
                            reason=request.get("reason", ""), release_id=request.get("release_id", ""),
                            text=request.get("text", ""),
                        )
                        if request["maintenance_action"] == "notify-complete":
                            worker.deliver_lifecycle_notices()
                        response = {"ok": True, "request_id": request_id, "result": result,
                                    "acknowledged_at": now_iso()}
                        atomic_write_json(response_path, response)
                        request_path.unlink(missing_ok=True)
                        continue
                    added = self.catch_up(project_keys)
                    for key in project_keys:
                        self.workers[key].signal.set()
                    response = {
                        "ok": True,
                        "request_id": request_id,
                        "project_keys": project_keys,
                        "added": added,
                        "acknowledged_at": now_iso(),
                    }
                    append_log(self.log_path, f"manual sync acknowledged: {request_id} {added}")
                except Exception as exc:
                    response = {"ok": False, "request_id": request_id, "error": str(exc)}
                    append_log(self.log_path, f"manual sync failed: {request_id}: {traceback.format_exc()}")
                atomic_write_json(response_path, response)
                request_path.unlink(missing_ok=True)
            time.sleep(0.5)

    def queue_snapshot(self, project_keys: list[str]) -> dict[str, Any]:
        active: list[dict[str, Any]] = []
        terminal_failures: list[dict[str, Any]] = []
        counts: dict[str, dict[str, int]] = {}
        for key in project_keys:
            worker = self.workers[key]
            with worker.store.lock:
                items = [dict(item) for item in worker.store.state.get("messages", {}).values()]
                bootstrap_unknown = worker.store.state.get("main_bootstrap_state") == "outcome_unknown"
            project_counts: dict[str, int] = {}
            for item in items:
                status_name = str(item.get("status") or "unknown")
                project_counts[status_name] = project_counts.get(status_name, 0) + 1
                attempts = int(item.get("attempts") or 0)
                fork_unknown = item.get("branch_state") == "fork_outcome_unknown" or bootstrap_unknown
                if status_name == "processing" or (not fork_unknown and (
                    status_name == "pending" or (status_name == "failed" and attempts < 3)
                )):
                    active.append({"project_key": key, "message_id": item.get("message_id"), "status": status_name})
                elif status_name == "provider_failed" or (status_name == "failed" and attempts >= 3) or (fork_unknown and status_name in {"pending", "failed"}):
                    terminal_failures.append(
                        {"project_key": key, "message_id": item.get("message_id"),
                         "error": item.get("error") or "Thread creation outcome unknown; reconciliation required"}
                    )
            counts[key] = project_counts
        return {"active": active, "terminal_failures": terminal_failures, "counts": counts}

    def wait_for_quiescence(self, project_keys: list[str], timeout: float, quiet_seconds: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            snapshot = self.queue_snapshot(project_keys)
            draining = any(
                entry["status"] == "processing" or self.workers[entry["project_key"]].store.admission_open
                for entry in snapshot["active"]
            )
            branches_running = any(self.workers[key].active_branches for key in project_keys)
            if draining or branches_running:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_seconds:
                return snapshot
            time.sleep(0.25)
        raise GatewayError(f"Timed out waiting for a safe Listener reload: {self.queue_snapshot(project_keys)['active']}")

    def set_admission(self, opened: bool) -> None:
        for worker in self.workers.values():
            with worker.store.lock:
                worker.store.admission_open = opened
            worker.signal.set()
            worker.branch_signal.set()

    def process_reload_requests(self) -> None:
        RELOAD_REQUESTS.mkdir(parents=True, exist_ok=True)
        RELOAD_RESPONSES.mkdir(parents=True, exist_ok=True)
        while True:
            for request_path in sorted(RELOAD_REQUESTS.glob("*.json")):
                request_id = request_path.stem
                response_path = RELOAD_RESPONSES / f"{request_id}.json"
                try:
                    request = load_json(request_path, {})
                    timeout = float(request.get("wait_timeout") or 3600)
                    project_keys = list(self.workers)
                    # Freeze new main/branch claims before waiting for active
                    # runs. Keep all incoming messages durable for the next
                    # listener; continuous arrivals cannot starve the reload.
                    self.set_admission(False)
                    added = self.catch_up(project_keys)
                    for worker in self.workers.values():
                        worker.signal.set()
                    snapshot = self.wait_for_quiescence(project_keys, timeout)
                    # A final history pass closes the gap between the first catch-up and queue drain.
                    final_added = self.catch_up(project_keys)
                    for worker in self.workers.values():
                        worker.signal.set()
                    snapshot = self.wait_for_quiescence(project_keys, timeout)
                    response = {
                        "ok": True,
                        "request_id": request_id,
                        "project_keys": project_keys,
                        "added": added,
                        "final_added": final_added,
                        "counts": snapshot["counts"],
                        "terminal_failures": snapshot["terminal_failures"],
                        "ready_to_restart_at": now_iso(),
                    }
                    # Finish atomic queue writes before acknowledging and
                    # exiting. Arrivals after admission closes remain durable
                    # here or recover through the next startup history pass.
                    with ExitStack() as locks:
                        for worker in self.workers.values():
                            locks.enter_context(worker.store.lock)
                        atomic_write_json(response_path, response)
                        request_path.unlink(missing_ok=True)
                        append_log(self.log_path, f"graceful reload acknowledged: {request_id}")
                        time.sleep(0.5)
                        os._exit(75)
                except Exception as exc:
                    self.set_admission(True)
                    response = {"ok": False, "request_id": request_id, "error": str(exc)}
                    append_log(self.log_path, f"graceful reload failed: {request_id}: {traceback.format_exc()}")
                    atomic_write_json(response_path, response)
                    request_path.unlink(missing_ok=True)
            time.sleep(0.5)

    def run(self) -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws.client import Client as WsClient
        except ImportError as exc:
            raise GatewayError("lark-oapi is not installed in the gateway environment") from exc

        self.start_workers()
        self.catch_up()

        reconnect_signal = threading.Event()

        def hourly() -> None:
            interval = int(self.config.get("catch_up_seconds") or 3600)
            while True:
                reconnected = reconnect_signal.wait(timeout=interval)
                reconnect_signal.clear()
                try:
                    project_keys = scheduled_catch_up_project_keys(
                        self.workers,
                        reconnected=reconnected,
                    )
                    if project_keys:
                        self.catch_up(
                            project_keys,
                            continue_on_error=reconnected,
                            log_when_empty=True,
                        )
                except Exception:
                    append_log(self.log_path, "hourly/reconnect catch-up failed:\n" + traceback.format_exc())

        threading.Thread(target=hourly, name="hourly-catch-up", daemon=True).start()
        threading.Thread(target=self.process_sync_requests, name="manual-sync", daemon=True).start()
        threading.Thread(target=self.process_reload_requests, name="graceful-reload", daemon=True).start()

        def on_message(event: Any) -> None:
            data = getattr(event, "event", None)
            message = getattr(data, "message", None)
            sender = getattr(data, "sender", None)
            if message is None or sender is None:
                return
            sender_id = getattr(sender, "sender_id", None)
            normalized = {
                "message_id": str(getattr(message, "message_id", "") or ""),
                "chat_id": str(getattr(message, "chat_id", "") or ""),
                "create_time": str(getattr(message, "create_time", "") or "0"),
                "message_type": str(getattr(message, "message_type", "") or ""),
                "content": getattr(message, "content", ""),
                "sender": {
                    "sender_type": str(getattr(sender, "sender_type", "") or ""),
                    "sender_id": str(getattr(sender_id, "open_id", "") or ""),
                },
            }
            tenant_key = str(getattr(sender, "tenant_key", "") or "")
            self.enqueue(normalized, tenant_key)

        dispatcher = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )
        app_id, secret = load_app_credentials(self.config)
        append_log(self.log_path, f"listener starting; projects={list(self.workers)}")
        ws_client = WsClient(
            app_id,
            secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=dispatcher,
            auto_reconnect=True,
        )
        if not attach_reconnect_reconciliation(ws_client, reconnect_signal):
            append_log(self.log_path, "SDK has no reconnect callback; startup/hourly reconciliation remains active")
        ws_client.start()


def init_project(args: argparse.Namespace) -> dict[str, Any]:
    publish_config = load_json(PUBLISH_CONFIG_PATH, {})
    config = load_config()
    if not config:
        config = {
            "schema_version": 1,
            "app_id": publish_config.get("app_id"),
            "expected_tenant_key": publish_config.get("expected_tenant_key"),
            "publisher_script": str(PUBLISHER),
            "catch_up_seconds": 3600,
            "projects": {},
        }
    path = Path(args.working_directory).resolve()
    if not path.is_dir():
        raise GatewayError(f"Working directory does not exist: {path}")
    existing_profile = config.get("projects", {}).get(args.project_key, {})
    config.setdefault("projects", {})[args.project_key] = {
        "working_directory": str(path),
        "workspace_mode": args.workspace_mode,
        "language": args.language,
        "hourly_catch_up_enabled": not args.disable_hourly_catch_up,
        "chat_name": args.chat_name,
        "chat_id": args.chat_id or "",
        "focus": args.focus,
        "bootstrap_source_thread_id": args.bootstrap_source_thread_id or "",
        "registered_epoch": now_epoch(),
        "agent_permission_mode": args.permission_mode,
        "agent_model": project_agent_model(existing_profile),
        **{key: existing_profile[key] for key in ("agent_reasoning_effort", "agent_service_tier") if key in existing_profile},
    }
    atomic_write_json(CONFIG_PATH, config)
    notice = (
        "Default Full Access lets the local Codex process access the whole machine without interactive approvals. "
        "Task scope and safety rules still bind it to this project. Ask in conversation to switch to "
        "project-only-auto or auto-review at any time."
    )
    return {
        "ok": True,
        "config": str(CONFIG_PATH),
        "project": config["projects"][args.project_key],
        "permission_notice": notice,
    }


def set_project_permission(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    project = config["projects"].get(args.project_key)
    if not isinstance(project, dict):
        raise GatewayError(f"Unknown remote project: {args.project_key}")
    project["agent_permission_mode"] = args.permission_mode
    atomic_write_json(CONFIG_PATH, config)
    return {
        "ok": True,
        "project_key": args.project_key,
        "agent_permission_mode": project_permission_mode(project),
    }


def create_chat(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    project = config["projects"][args.project_key]
    if project.get("chat_id"):
        return {"ok": True, "existing": True, "chat_id": project["chat_id"]}
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    if str(tenant.get("tenant_key") or "") != str(config["expected_tenant_key"]):
        raise GatewayError("Refusing to create a chat in an unexpected tenant")
    if project_language(project) == "en":
        description = (
            "Personal General remote Codex workspace; the internal directory only isolates temporary files"
            if project_workspace_mode(project) == "general"
            else f"Bound local working directory: {project['working_directory']}"
        )
    else:
        description = (
            "个人 General 远程 Codex 问答；内部运行目录仅用于隔离临时文件"
            if project_workspace_mode(project) == "general"
            else f"绑定本地 Working Directory：{project['working_directory']}"
        )
    payload = client.create_chat(
        project["chat_name"],
        args.owner_open_id,
        description,
    )
    chat = (payload.get("data") or {}).get("chat") or payload.get("data") or {}
    chat_id = str(chat.get("chat_id") or "")
    if not chat_id:
        raise GatewayError(f"Create chat returned no chat_id: {payload}")
    project["chat_id"] = chat_id
    project["registered_epoch"] = now_epoch()
    atomic_write_json(CONFIG_PATH, config)
    return {"ok": True, "chat_id": chat_id, "name": project["chat_name"]}


def status() -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    app_id, secret = load_app_credentials(config)
    client = FeishuClient(app_id, secret)
    tenant = client.tenant_info()
    projects = {}
    for key, project in config["projects"].items():
        store = ProjectStore(key)
        counts: dict[str, int] = {}
        for item in store.state.get("messages", {}).values():
            state = str(item.get("status") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        projects[key] = {
            "working_directory": project.get("working_directory"),
            "chat_id": project.get("chat_id"),
            "thread_id": store.thread_id(),
            "agent_permission_mode": project_permission_mode(project),
            "counts": counts,
            "cursor": store.state.get("cursor"),
        }
    return {
        "ok": True,
        "app_id": app_id,
        "tenant_key": tenant.get("tenant_key"),
        "tenant_name": tenant.get("name"),
        "projects": projects,
    }


def set_project_profile(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    project = config["projects"].get(args.project_key)
    if not isinstance(project, dict):
        raise GatewayError(f"Unknown remote project: {args.project_key}")
    model = str(args.model or "").strip()
    reasoning_effort = str(args.reasoning_effort or "").strip()
    if not model or not reasoning_effort:
        raise GatewayError("Both model and reasoning effort are required")
    project["agent_model"] = model
    project["agent_reasoning_effort"] = reasoning_effort
    if args.service_tier is not None:
        service_tier = str(args.service_tier).strip()
        if service_tier:
            project["agent_service_tier"] = service_tier
        else:
            project.pop("agent_service_tier", None)
    atomic_write_json(CONFIG_PATH, config)
    return {
        "ok": True,
        "project_key": args.project_key,
        "agent_model": project.get("agent_model"),
        "agent_reasoning_effort": project.get("agent_reasoning_effort"),
        "agent_service_tier": project.get("agent_service_tier"),
    }


def normalized_local_path(value: str | Path) -> str:
    text = str(value).strip()
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def is_hidden_context_injection(text: str) -> bool:
    normalized = text.lstrip("\ufeff\r\n\t ")
    return normalized.startswith(
        (
            "# AGENTS.md instructions",
            "<environment_context>",
            "<permissions instructions>",
            "<skills_instructions>",
            "<apps_instructions>",
            "<plugins_instructions>",
            "<recommended_plugins>",
        )
    )


def visible_rollout_messages(path: Path) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if not path.is_file():
        return messages
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload") or {}
            role = str(payload.get("role") or "")
            if payload.get("type") != "message" or role not in {"user", "assistant"}:
                continue
            phase = payload.get("phase")
            if role == "assistant" and phase not in {None, "final_answer"}:
                continue
            text = "\n".join(
                str(part.get("text") or "")
                for part in payload.get("content") or []
                if isinstance(part, dict)
                and part.get("type") in {"input_text", "output_text"}
                and part.get("text")
            ).strip()
            if text and not (role == "user" and is_hidden_context_injection(text)):
                messages.append(
                    {
                        "timestamp": event.get("timestamp"),
                        "role": role,
                        "text": text,
                    }
                )
    return messages


def strip_remote_prompt(text: str) -> str:
    marker = "\n用户消息：\n"
    if marker not in text:
        return text
    value = text.split(marker, 1)[1]
    attachment_marker = "\n\n本地附件：\n"
    if attachment_marker in value:
        value = value.split(attachment_marker, 1)[0]
    return value.strip()


def codex_thread_rows() -> list[dict[str, Any]]:
    if not CODEX_STATE_DB.is_file():
        raise GatewayError(f"Codex thread index does not exist: {CODEX_STATE_DB}")
    uri = CODEX_STATE_DB.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, rollout_path, cwd, title, source, thread_source, updated_at "
            "FROM threads ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def registered_project(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config()
    assert_config(config)
    project = config["projects"].get(args.project_key)
    if not isinstance(project, dict):
        raise GatewayError(f"Unknown remote project: {args.project_key}")
    return config, project


def display_thread_title(value: Any, limit: int = 160) -> str:
    title = " ".join(str(value or "").split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def allowed_desktop_threads(project_key: str, project: dict[str, Any]) -> list[dict[str, Any]]:
    wanted_cwd = normalized_local_path(project["working_directory"])
    remote_thread_id = ProjectStore(project_key).thread_id()
    bootstrap_id = str(project.get("bootstrap_source_thread_id") or "")
    workspace_mode = project_workspace_mode(project)
    allowed: list[dict[str, Any]] = []
    for row in codex_thread_rows():
        thread_id = str(row["id"])
        if workspace_mode == "general":
            if not bootstrap_id or thread_id != bootstrap_id:
                continue
        elif normalized_local_path(row["cwd"]) != wanted_cwd:
            continue
        source = str(row.get("source") or "")
        title = str(row.get("title") or "")
        if thread_id == remote_thread_id or source == "exec":
            continue
        if "guardian" in source or (title.startswith("<codex_delegation>") and thread_id != bootstrap_id):
            continue
        allowed.append(row)
    return allowed


def desktop_context(args: argparse.Namespace) -> dict[str, Any]:
    _, project = registered_project(args)
    rows = allowed_desktop_threads(args.project_key, project)
    if args.thread_id:
        rows = [row for row in rows if str(row["id"]) == args.thread_id]
        if not rows:
            raise GatewayError("Requested thread is not a user-visible Desktop thread in this project")
    threads = [
        {
            "thread_id": str(row["id"]),
            "title": display_thread_title(row.get("title")),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    ]
    transcripts: list[dict[str, Any]] = []
    selected_rows = rows[:1] if not args.thread_id else rows
    for row in selected_rows:
        path = Path(str(row["rollout_path"]))
        visible = visible_rollout_messages(path)
        for message in visible[-max(1, args.limit) :]:
            transcripts.append(
                {
                    "thread_id": str(row["id"]),
                    "title": display_thread_title(row.get("title")),
                    **message,
                }
            )
    return {
        "ok": True,
        "source": "desktop",
        "project_key": args.project_key,
        "working_directory": str(project["working_directory"]),
        "automatic_mirroring": False,
        "threads": threads,
        "transcript": transcripts[-max(1, args.limit) :],
    }


def feishu_context(args: argparse.Namespace) -> dict[str, Any]:
    _, project = registered_project(args)
    store = ProjectStore(args.project_key)
    items = sorted(
        (store.state.get("messages") or {}).values(),
        key=lambda item: (int(item.get("create_time") or 0), str(item.get("message_id") or "")),
    )
    recent_messages = [
        {
            "message_id": str(item.get("message_id") or ""),
            "create_time": item.get("create_time"),
            "status": item.get("status"),
            "text": normalized_user_text(item),
        }
        for item in items[-max(1, args.limit) :]
    ]
    thread_id = store.thread_id()
    transcript: list[dict[str, Any]] = []
    if thread_id:
        matches = [row for row in codex_thread_rows() if str(row["id"]) == thread_id]
        if matches:
            transcript = visible_rollout_messages(Path(str(matches[0]["rollout_path"])))
            for message in transcript:
                if message["role"] == "user":
                    message["text"] = strip_remote_prompt(message["text"])
    return {
        "ok": True,
        "source": "feishu",
        "project_key": args.project_key,
        "working_directory": str(project["working_directory"]),
        "automatic_mirroring": False,
        "remote_thread_id": thread_id,
        "recent_messages": recent_messages,
        "transcript": transcript[-max(1, args.limit) :],
    }


def selected_project_keys(config: dict[str, Any], project_key: str | None, working_directory: str | None) -> list[str]:
    if project_key:
        if project_key not in config["projects"]:
            raise GatewayError(f"Unknown remote project: {project_key}")
        return [project_key]
    if working_directory:
        wanted = str(Path(working_directory).resolve()).casefold()
        matches = [
            key
            for key, project in config["projects"].items()
            if str(Path(project["working_directory"]).resolve()).casefold() == wanted
        ]
        if len(matches) != 1:
            raise GatewayError(f"Working directory must match exactly one registered project: {working_directory}")
        return matches
    raise GatewayError("A project key or working directory is required")


def request_sync(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    project_keys = selected_project_keys(config, args.project_key, args.working_directory)
    request_id = uuid.uuid4().hex
    SYNC_REQUESTS.mkdir(parents=True, exist_ok=True)
    SYNC_RESPONSES.mkdir(parents=True, exist_ok=True)
    request_path = SYNC_REQUESTS / f"{request_id}.json"
    response_path = SYNC_RESPONSES / f"{request_id}.json"
    atomic_write_json(
        request_path,
        {"request_id": request_id, "project_keys": project_keys, "requested_at": now_iso()},
    )
    ack_deadline = time.monotonic() + float(args.ack_timeout)
    while time.monotonic() < ack_deadline and not response_path.exists():
        time.sleep(0.25)
    if not response_path.exists():
        raise GatewayError("Listener did not acknowledge the sync request; ensure the gateway task is running")
    response = load_json(response_path, {})
    response_path.unlink(missing_ok=True)
    if not response.get("ok"):
        raise GatewayError(str(response.get("error") or "Listener rejected the sync request"))

    usage_reports: dict[str, Any] = {}
    if getattr(args, "report_usage", False) and not getattr(args, "inspection_report", False):
        for key in project_keys:
            usage_reports[key] = send_codex_usage_report(config, key)
    inspection_reports: dict[str, Any] = {}
    if getattr(args, "inspection_report", False):
        for key in project_keys:
            inspection_reports[key] = send_default_inspection_message(config, key)
    elif getattr(args, "first_inspection_message", False):
        for key in project_keys:
            inspection_reports[key] = send_first_inspection_message(config, key)
    progress_reports: dict[str, Any] = {}
    if getattr(args, "request_only", False):
        for key in project_keys:
            inspection = inspection_reports.get(key) or {}
            if inspection.get("message_id") and not inspection.get("skipped"):
                progress_reports[key] = {"ok": True, "sent": False, "reason": "included-in-inspection"}
            else:
                progress_reports[key] = send_active_task_progress_report(config, key)
    reminder_reports: dict[str, Any] = {}
    for key in project_keys:
        reminder_reports[key] = send_due_reminders(config, key)
    if getattr(args, "request_only", False):
        return {
            "ok": True,
            "request_id": request_id,
            "project_keys": project_keys,
            "status": "accepted",
            "added": response.get("added") or {},
            "usage_reports": usage_reports,
            "inspection_reports": inspection_reports,
            "progress_reports": progress_reports,
            "reminder_reports": reminder_reports,
        }

    deadline = time.monotonic() + float(args.wait_timeout)
    while True:
        active: list[dict[str, Any]] = []
        terminal_failures: list[dict[str, Any]] = []
        counts: dict[str, dict[str, int]] = {}
        for key in project_keys:
            state = load_json(project_runtime(key) / "state.json", {})
            project_counts: dict[str, int] = {}
            for message_id, item in (state.get("messages") or {}).items():
                status_name = str(item.get("status") or "unknown")
                project_counts[status_name] = project_counts.get(status_name, 0) + 1
                attempts = int(item.get("attempts") or 0)
                if status_name in {"pending", "processing"} or (status_name == "failed" and attempts < 3):
                    active.append({"project_key": key, "message_id": message_id, "status": status_name})
                elif status_name == "deferred":
                    terminal_failures.append({"project_key": key, "message_id": message_id,
                                              "error": "Deferred for maintenance; not executed"})
                elif status_name == "provider_failed" or (status_name == "failed" and attempts >= 3):
                    terminal_failures.append({"project_key": key, "message_id": message_id, "error": item.get("error")})
            counts[key] = project_counts
        if not active:
            if terminal_failures:
                raise GatewayError(f"Sync completed with terminal failures: {terminal_failures}")
            return {
                "ok": True,
                "request_id": request_id,
                "project_keys": project_keys,
                "added": response.get("added") or {},
                "counts": counts,
                "usage_reports": usage_reports,
                "inspection_reports": inspection_reports,
                "progress_reports": progress_reports,
                "reminder_reports": reminder_reports,
            }
        if time.monotonic() >= deadline:
            raise GatewayError(f"Timed out waiting for the Feishu queue to drain: {active}")
        time.sleep(1)


def request_reload(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    request_id = uuid.uuid4().hex
    RELOAD_REQUESTS.mkdir(parents=True, exist_ok=True)
    RELOAD_RESPONSES.mkdir(parents=True, exist_ok=True)
    request_path = RELOAD_REQUESTS / f"{request_id}.json"
    response_path = RELOAD_RESPONSES / f"{request_id}.json"
    atomic_write_json(
        request_path,
        {
            "request_id": request_id,
            "wait_timeout": float(args.wait_timeout),
            "requested_at": now_iso(),
            "request_only": bool(getattr(args, "request_only", False)),
        },
    )
    if getattr(args, "request_only", False):
        return {
            "ok": True,
            "request_id": request_id,
            "status": "queued",
            "request_path": str(request_path),
        }
    deadline = time.monotonic() + float(args.wait_timeout) + float(args.ack_timeout)
    while time.monotonic() < deadline and not response_path.exists():
        time.sleep(0.25)
    if not response_path.exists():
        raise GatewayError("Listener did not acknowledge the graceful reload request")
    response = load_json(response_path, {})
    response_path.unlink(missing_ok=True)
    if not response.get("ok"):
        raise GatewayError(str(response.get("error") or "Listener rejected the graceful reload request"))
    return response


def automation_prompt(project_key: str, working_directory: str, report_usage: bool = True) -> str:
    report_text = "并向项目飞书群发送默认三句巡检报告，其中运行中任务的当前进度写在第一句。" if report_usage else "如有任务正在执行，会向项目飞书群报告当前进度。"
    if platform.system() == "Windows":
        script = SCRIPT_DIR / "sync_feishu.ps1"
        report_flag = " -InspectionReport" if report_usage else " -FirstInspectionMessage"
        command = (
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script}\" "
            f"-ProjectKey {project_key} -RequestOnly{report_flag}"
        )
    else:
        command_parts = [
            str(runtime_python_path()),
            str(SCRIPT_DIR / "sync_feishu.py"),
            "--project-key",
            project_key,
            "--request-only",
        ]
        if report_usage:
            command_parts.append("--inspection-report")
        else:
            command_parts.append("--first-inspection-message")
        command = shlex.join(command_parts)
    return (
        f"运行 {command}。"
        f"脚本会按需启动 Listener、请求异步处理积压后立即返回，{report_text}"
        "不要另发桌面总结；仅失败时简短报告。"
    )


def install_hourly_automation(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    project = config["projects"].get(args.project_key)
    if not isinstance(project, dict):
        raise GatewayError(f"Unknown remote project: {args.project_key}")
    if not project_hourly_catch_up_enabled(project):
        raise GatewayError(f"Hourly catch-up is disabled for remote project: {args.project_key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.automation_id):
        raise GatewayError("Automation ID must contain only lowercase letters, digits, and hyphens")
    target = AUTOMATIONS_ROOT / args.automation_id / "automation.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    values = {
        "id": args.automation_id,
        "name": args.name,
        "prompt": automation_prompt(
            args.project_key,
            str(project["working_directory"]),
            bool(getattr(args, "report_usage", True)),
        ),
        "target_thread_id": args.target_thread_id,
        "status": "PAUSED" if getattr(args, "paused", False) else "ACTIVE",
    }
    text = "\n".join(
        [
            "version = 1",
            f"id = {json.dumps(values['id'], ensure_ascii=False)}",
            'kind = "heartbeat"',
            f"name = {json.dumps(values['name'], ensure_ascii=False)}",
            f"prompt = {json.dumps(values['prompt'], ensure_ascii=False)}",
            f"status = {json.dumps(values['status'])}",
            'rrule = "FREQ=HOURLY;INTERVAL=1"',
            'notification_policy = "failed_runs_only"',
            f"target_thread_id = {json.dumps(values['target_thread_id'])}",
            f"created_at = {now_ms}",
            f"updated_at = {now_ms}",
            "",
        ]
    )
    target.write_text(text, encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "automation_id": args.automation_id,
        "path": str(target),
        "target_thread_id": args.target_thread_id,
        "status": values["status"],
        "report_usage": bool(getattr(args, "report_usage", True)),
    }


def request_maintenance(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    assert_config(config)
    keys = selected_project_keys(config, args.project_key, None)
    if args.action == "status":
        state = load_json(project_runtime(keys[0]) / "state.json", {})
        return {"ok": True, "maintenance": state.get("maintenance", {}),
                "notices": state.get("lifecycle_notices", {}),
                "unconfirmed_stops": [
                    {"message_id": mid, "error": row["maintenance_stop_error"]}
                    for mid, row in state.get("messages", {}).items()
                    if row.get("status") == "processing" and row.get("maintenance_stop_error")],
                "recovery_error": state.get("lifecycle_error")}
    if args.action == "enter" and not (args.reason or "").strip():
        raise GatewayError("A maintenance reason is required")
    if args.action == "notify-complete" and not (args.release_id or "").strip():
        raise GatewayError("A verified release ID is required")
    text = Path(args.text_file).read_text(encoding="utf-8-sig") if args.text_file else ""
    request_id = uuid.uuid4().hex
    request_path = SYNC_REQUESTS / f"{request_id}.json"
    response_path = SYNC_RESPONSES / f"{request_id}.json"
    atomic_write_json(request_path, {
        "request_id": request_id, "project_keys": keys, "requested_at": now_iso(),
        "maintenance_action": args.action, "reason": args.reason or "",
        "release_id": args.release_id or "", "text": text,
    })
    deadline = time.monotonic() + args.ack_timeout
    while time.monotonic() < deadline and not response_path.exists():
        time.sleep(0.25)
    if not response_path.exists():
        raise GatewayError(f"Listener has not acknowledged maintenance request {request_id}; request retained")
    response = load_json(response_path, {})
    if not response.get("ok"):
        raise GatewayError(str(response.get("error") or "Maintenance request rejected"))
    if args.action == "notify-complete":
        while time.monotonic() < deadline:
            state = load_json(project_runtime(keys[0]) / "state.json", {})
            notice = state.get("lifecycle_notices", {}).get("complete:" + args.release_id, {})
            if notice.get("status") == "delivered":
                return {"ok": True, "status": "delivered", "request_id": request_id,
                        "remote_message_id": notice.get("remote_message_id")}
            if notice.get("error"):
                raise GatewayError("Completion notice retained for retry: " + str(notice["error"]))
            time.sleep(0.25)
        raise GatewayError("Completion notice queued but delivery not yet confirmed")
    return response


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Feishu mobile gateway for persistent Codex project threads")
    commands = result.add_subparsers(dest="command", required=True)
    maintenance = commands.add_parser("maintenance")
    maintenance.add_argument("action", choices=("enter", "exit", "status", "notify-complete"))
    maintenance.add_argument("--project-key", required=True)
    maintenance.add_argument("--reason")
    maintenance.add_argument("--release-id")
    maintenance.add_argument("--text-file")
    maintenance.add_argument("--ack-timeout", type=float, default=30)
    initialize = commands.add_parser("init-project")
    initialize.add_argument("--project-key", required=True)
    initialize.add_argument("--working-directory", required=True)
    initialize.add_argument("--workspace-mode", choices=("project", "general"), default="project")
    initialize.add_argument("--language", choices=("zh-CN", "en"), default="zh-CN")
    initialize.add_argument("--disable-hourly-catch-up", action="store_true")
    initialize.add_argument("--chat-name", required=True)
    initialize.add_argument("--chat-id")
    initialize.add_argument("--focus", required=True)
    initialize.add_argument("--bootstrap-source-thread-id")
    initialize.add_argument(
        "--permission-mode",
        choices=tuple(sorted(PERMISSION_MODES)),
        default=DEFAULT_PERMISSION_MODE,
    )
    create = commands.add_parser("create-chat")
    create.add_argument("--project-key", required=True)
    create.add_argument("--owner-open-id", required=True)
    welcome = commands.add_parser("send-welcome")
    welcome.add_argument("--project-key", required=True)
    profile = commands.add_parser("set-project-profile")
    profile.add_argument("--project-key", required=True)
    profile.add_argument("--model", required=True)
    profile.add_argument("--reasoning-effort", required=True)
    profile.add_argument("--service-tier")
    permission = commands.add_parser("set-project-permission")
    permission.add_argument("--project-key", required=True)
    permission.add_argument("--permission-mode", choices=tuple(sorted(PERMISSION_MODES)), required=True)
    desktop = commands.add_parser("desktop-context")
    desktop.add_argument("--project-key", required=True)
    desktop.add_argument("--thread-id")
    desktop.add_argument("--limit", type=int, default=20)
    feishu = commands.add_parser("feishu-context")
    feishu.add_argument("--project-key", required=True)
    feishu.add_argument("--limit", type=int, default=20)
    sync = commands.add_parser("sync")
    sync_target = sync.add_mutually_exclusive_group(required=True)
    sync_target.add_argument("--project-key")
    sync_target.add_argument("--working-directory")
    sync.add_argument("--ack-timeout", type=float, default=30)
    sync.add_argument("--wait-timeout", type=float, default=3600)
    sync.add_argument(
        "--request-only",
        action="store_true",
        help="Return after Listener acknowledgement; intended for scheduled inspections",
    )
    sync.add_argument("--report-usage", action="store_true")
    sync.add_argument("--first-inspection-message", action="store_true")
    sync.add_argument(
        "--inspection-report",
        action="store_true",
        help="Send the one-time formal explanation or the default three-line recurring report",
    )
    reload_command = commands.add_parser("reload")
    reload_command.add_argument("--ack-timeout", type=float, default=30)
    reload_command.add_argument("--wait-timeout", type=float, default=3600)
    reload_command.add_argument(
        "--request-only",
        action="store_true",
        help="Queue a graceful reload and return immediately; use from a running remote turn",
    )
    automation = commands.add_parser("install-hourly-automation")
    automation.add_argument("--project-key", required=True)
    automation.add_argument("--automation-id", required=True)
    automation.add_argument("--name", required=True)
    automation.add_argument("--target-thread-id", required=True)
    automation.add_argument(
        "--report-usage",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include the default three-line Feishu report; use --no-report-usage after an explicit user change",
    )
    automation_state = automation.add_mutually_exclusive_group()
    automation_state.add_argument(
        "--paused",
        action="store_true",
        help="Create the scheduled inspection paused instead of using the normal active default",
    )
    automation_state.add_argument(
        "--activate",
        action="store_true",
        help="Compatibility flag; the scheduled inspection is active by default",
    )
    commands.add_parser("status")
    catch_up = commands.add_parser("catch-up")
    catch_up_target = catch_up.add_mutually_exclusive_group(required=True)
    catch_up_target.add_argument("--project-key")
    catch_up_target.add_argument("--working-directory")
    catch_up.add_argument("--ack-timeout", type=float, default=30)
    catch_up.add_argument("--wait-timeout", type=float, default=3600)
    commands.add_parser("run")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "maintenance":
            value = request_maintenance(args)
        elif args.command == "init-project":
            value = init_project(args)
        elif args.command == "create-chat":
            value = create_chat(args)
        elif args.command == "send-welcome":
            value = send_welcome(args)
        elif args.command == "set-project-profile":
            value = set_project_profile(args)
        elif args.command == "set-project-permission":
            value = set_project_permission(args)
        elif args.command == "desktop-context":
            value = desktop_context(args)
        elif args.command == "feishu-context":
            value = feishu_context(args)
        elif args.command == "sync":
            value = request_sync(args)
        elif args.command == "reload":
            value = request_reload(args)
        elif args.command == "install-hourly-automation":
            value = install_hourly_automation(args)
        elif args.command == "status":
            value = status()
        elif args.command == "catch-up":
            value = request_sync(args)
        elif args.command == "run":
            GatewayService(load_config()).run()
            return 0
        else:
            raise AssertionError(args.command)
        print(cli_json_dumps(value))
        return 0
    except Exception as exc:
        print(cli_json_dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
