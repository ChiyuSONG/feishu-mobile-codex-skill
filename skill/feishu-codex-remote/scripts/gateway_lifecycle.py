"""Maintenance and temporary-thread recovery using the gateway's existing store."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import os
import signal
import subprocess
import threading
import time

from gateway_common import GatewayError


RETRY_SECONDS = 30.0


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MaintenancePaused(GatewayError):
    pass


class MaintenanceStopFailed(GatewayError):
    pass


class LifecycleStore:
    def verify_acceptance(self, release_id, acceptance_file):
        if not acceptance_file:
            raise GatewayError("Completion requires an acceptance evidence file")
        path = Path(acceptance_file).resolve()
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            if (evidence.get("release_id") != release_id or not evidence.get("scope")
                    or evidence.get("remaining_work") != [] or evidence.get("conflicts") != []
                    or evidence.get("catch_up_verified") is not True or not evidence.get("checks")):
                raise ValueError("Incomplete acceptance")
            for check in evidence["checks"]:
                artifact = (path.parent / check["artifact"]).resolve()
                if check.get("passed") is not True or hashlib.sha256(artifact.read_bytes()).hexdigest() != check["sha256"]:
                    raise ValueError("Changed or failed evidence")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GatewayError("Acceptance evidence is missing, changed, or not passing") from exc
        with self.lock:
            if evidence.get("maintenance_id") != self.state.get("maintenance", {}).get("id"):
                raise GatewayError("Acceptance belongs to a different maintenance generation")
            for mid in evidence.get("required_message_ids", []):
                if self.state.get("messages", {}).get(mid, {}).get("status") != "completed":
                    raise GatewayError("A request required for this repair is not complete")
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "maintenance_id": evidence.get("maintenance_id")}

    def fail_provider_capacity(self, message_ids, event_log, text, kind="at_capacity"):
        with self.lock:
            rows = [self.state["messages"][mid] for mid in message_ids]
            if any(row.get("status") != "processing" or row.get("run_event_log") != str(event_log)
                   for row in rows):
                raise GatewayError("Capacity result does not own the active run")
            for row in rows:
                row.update(status="pending", provider_wait=kind,
                           attempts=max(0, int(row.get("attempts", 0)) - 1),
                           error_kind=kind, error=text)
                self._queue_lifecycle_notice("provider:" + kind + ":" + row["message_id"], text, row["message_id"])
            self.save()

    def resume_provider_pending(self):
        with self.lock:
            changed = False
            for row in self.state.get("messages", {}).values():
                if row.get("status") == "pending" and row.get("provider_wait"):
                    row.pop("provider_wait", None)
                    changed = True
            if changed:
                self.save()

    def pause_requested(self, message_ids=()):
        with self.lock:
            return bool(self.state.get("maintenance", {}).get("active")) or any(
                self.state.get("messages", {}).get(mid, {}).get("defer_requested")
                or self.state.get("messages", {}).get(mid, {}).get("status") == "deferred"
                for mid in message_ids
            )

    def _queue_lifecycle_notice(self, key, text, message_id=None):
        notices = self.state.setdefault("lifecycle_notices", {})
        previous = notices.get(key)
        if previous:
            if previous["text"] != text or previous.get("message_id") != message_id:
                raise GatewayError("Notification identity already has a different payload")
            return previous
        notice = {
            "text": text, "message_id": message_id, "status": "pending",
            "uuid": "lifecycle-" + hashlib.sha256(
                (self.project_key + "\0" + key).encode()).hexdigest()[:32],
            "created_at": timestamp(), "attempts": 0,
        }
        notices[key] = notice
        return notice

    def _defer(self, row):
        mode = self.state.get("maintenance", {})
        generation = row.get("defer_requested") or mode.get("id")
        if not generation:
            raise GatewayError("No maintenance generation for deferred work")
        if row.get("status") == "processing":
            row["attempts"] = max(0, int(row.get("attempts", 0)) - 1)
        row.update(status="pending", defer_requested=generation, deferred_at=timestamp())
        self._queue_lifecycle_notice(
            "deferred:" + str(generation) + ":" + row["message_id"],
            mode["notice_text"], row["message_id"],
        )

    def enter_maintenance(self, request_id, reason, notice_text):
        if not reason.strip():
            raise GatewayError("A maintenance reason is required")
        with self.lock:
            mode = self.state.get("maintenance", {})
            if not mode.get("active"):
                mode = {"active": True, "id": request_id, "reason": reason,
                        "notice_text": notice_text, "entered_at": timestamp()}
                self.state["maintenance"] = mode
                for key, notice in self.state.get("lifecycle_notices", {}).items():
                    if key.startswith("complete:") and notice.get("status") == "pending":
                        notice["status"] = "cancelled"
            for row in self.state.get("messages", {}).values():
                if row.get("status") == "pending":
                    self._defer(row)
                elif row.get("status") == "processing":
                    row["defer_requested"] = mode["id"]
            self.save()
            return dict(mode)

    def defer_batch(self, message_ids):
        with self.lock:
            for mid in message_ids:
                row = self.state["messages"][mid]
                if row.get("status") not in {"completed", "ignored"}:
                    self._defer(row)
            self.save()

    def exit_maintenance(self):
        with self.lock:
            if any(row.get("status") == "processing" and row.get("defer_requested")
                   for row in self.state.get("messages", {}).values()):
                raise GatewayError("Active work is still stopping; maintenance remains enabled")
            mode = self.state.setdefault("maintenance", {})
            for row in self.state.get("messages", {}).values():
                if row.get("status") == "deferred" or (row.get("status") == "pending" and row.get("defer_requested")):
                    row["status"] = "pending"
                    row.pop("defer_requested", None)
                    row.pop("maintenance_stop_error", None)
            for key, notice in self.state.get("lifecycle_notices", {}).items():
                if key.startswith("deferred:") and notice.get("status") == "pending":
                    notice["status"] = "cancelled"
            if mode.get("active"):
                mode.update(active=False, exited_at=timestamp())
            self.save()
            return dict(mode)

    def queue_completion(self, release_id, text, acceptance_file=None):
        if not release_id.strip() or not text.strip():
            raise GatewayError("Release identity and completion text are required")
        evidence = self.verify_acceptance(release_id, acceptance_file)
        with self.lock:
            if self.pause_requested():
                raise GatewayError("Exit maintenance and verify recovery before notifying completion")
            if evidence["maintenance_id"] != self.state.get("maintenance", {}).get("id"):
                raise GatewayError("Maintenance changed during acceptance verification")
            notice = self._queue_lifecycle_notice("complete:" + release_id, text)
            notice["acceptance"] = evidence
            self.save()
            return dict(notice)


def _stop_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10, check=False,
        )
        if result.returncode and process.poll() is None:
            raise GatewayError("Could not stop the owned Codex process tree")
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            # The root can exit before descendants that ignored SIGTERM.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait(timeout=10)


def run_model_process(command, *, input, store, message_ids, check=False, **kwargs):
    """No model deadline; the polling interval only observes explicit maintenance."""
    with store.lock:
        if store.pause_requested(message_ids):
            raise MaintenancePaused("System upgrade; original request retained")
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, start_new_session=os.name != "nt", **kwargs)
    try:
        remaining_input = input
        while True:
            try:
                process.communicate(input=remaining_input, timeout=1)
                break
            except subprocess.TimeoutExpired:
                remaining_input = None
                if store.pause_requested(message_ids):
                    _stop_process_tree(process)
                    raise MaintenancePaused("System upgrade; original request retained")
        return subprocess.CompletedProcess(command, process.returncode)
    except BaseException:
        try:
            _stop_process_tree(process)
        except Exception as exc:
            raise MaintenanceStopFailed("Owned process has not confirmed stopping") from exc
        raise
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()


class LifecycleWorker:
    def init_lifecycle(self):
        self.lifecycle_signal = threading.Event()
        self.lifecycle_stop = threading.Event()
        self.lifecycle_delivery_lock = threading.Lock()
        self.lifecycle_archive_lock = threading.Lock()

    def start_lifecycle(self):
        self.lifecycle_thread = threading.Thread(
            target=self._lifecycle_loop, name=f"lifecycle-{self.key}", daemon=True)
        self.lifecycle_thread.start()
        self.lifecycle_signal.set()
        return self.lifecycle_thread

    def maintenance_action(self, action, request_id, reason="", release_id="", text="", acceptance_file=None):
        english = str(self.project.get("language") or "").lower().startswith("en")
        if action == "enter":
            notice = (
                "The system is upgrading. Your request is retained and will resume automatically after maintenance."
                if english else
                "系统正在升级，消息已保留，维护结束后会自动继续处理，无需重发。"
            )
            result = self.store.enter_maintenance(request_id, reason, notice)
        elif action == "exit":
            result = self.store.exit_maintenance()
        elif action == "notify-complete":
            default = (
                "The repair has been verified and service has resumed. Retained requests will continue automatically."
                if english else "修复已验收，服务已恢复，已保留的请求将自动继续处理，无需重发。"
            )
            result = self.store.queue_completion(release_id, text or default, acceptance_file)
        else:
            raise GatewayError("Unknown maintenance action")
        self.signal.set()
        self.branch_signal.set()
        self.lifecycle_signal.set()
        return result

    def deliver_lifecycle_notices(self):
        if not self.lifecycle_delivery_lock.acquire(blocking=False):
            return
        try:
            with self.store.lock:
                pending = [(key, dict(row)) for key, row in self.store.state.get("lifecycle_notices", {}).items()
                           if row.get("status") == "pending" and row.get("retry_at", 0) <= time.time()]
            for key, notice in pending:
                with self.store.lock:
                    if self.store.state["lifecycle_notices"][key].get("status") != "pending":
                        continue
                    if key.startswith("complete:") and self.store.pause_requested():
                        continue
                try:
                    parts = self.lifecycle_notice_parts(notice["text"])
                    for index, part in enumerate(parts):
                        with self.store.lock:
                            current = self.store.state["lifecycle_notices"][key]
                            if current.get("status") != "pending":
                                raise GatewayError("Notification superseded while delivering")
                            saved = current.get("delivered_parts", {}).get(str(index))
                        if saved:
                            continue
                        send_uuid = notice["uuid"] if index == 0 else notice["uuid"] + "-" + str(index)
                        if notice.get("message_id"):
                            result = self.client.reply_post(notice["message_id"], part, send_uuid)
                        else:
                            result = self.client.send_post(self.project["chat_id"], part, send_uuid)
                        if not isinstance(result, dict) or not (result.get("data") or {}).get("message_id"):
                            raise GatewayError("Notification delivery has no remote message receipt")
                        with self.store.lock:
                            row = self.store.state["lifecycle_notices"][key]
                            row.setdefault("delivered_parts", {})[str(index)] = result["data"]["message_id"]
                            self.store.save()
                    with self.store.lock:
                        row = self.store.state["lifecycle_notices"][key]
                        if row.get("status") != "pending":
                            continue
                        row.update(status="delivered", delivered_at=timestamp(), error=None,
                                   remote_message_id=row["delivered_parts"]["0"])
                        self.store.save()
                except Exception as exc:
                    with self.store.lock:
                        row = self.store.state["lifecycle_notices"][key]
                        row.update(error=str(exc), retry_at=time.time() + RETRY_SECONDS,
                                   attempts=int(row.get("attempts", 0)) + 1)
                        self.store.save()
        finally:
            self.lifecycle_delivery_lock.release()

    def reconcile_branch_archives(self):
        if not self.lifecycle_archive_lock.acquire(blocking=False):
            return
        try:
            with self.store.lock:
                candidates = [dict(row) for row in self.store.state.get("messages", {}).values()
                              if row.get("branch_thread_id") and row.get("branch_archive_state") != "archived"
                              and row.get("branch_archive_retry_at", 0) <= time.time()
                              and row["message_id"] not in self.active_branches
                              and (row.get("status") == "completed"
                                   or row.get("status") == "failed" and int(row.get("attempts", 0)) >= 3)]
            for row in candidates:
                child = row["branch_thread_id"]
                if child == self.store.thread_id() or child == row.get("branch_parent_thread_id"):
                    self.store.update_message(row["message_id"], branch_archive_error="Refusing to archive a main thread")
                    continue
                try:
                    self._archive_temporary_thread(child)
                    self.store.update_message(row["message_id"], branch_archive_state="archived",
                                              branch_archived_at=timestamp(), branch_archive_error=None)
                except Exception as exc:
                    self.store.update_message(row["message_id"], branch_archive_state="pending",
                                              branch_archive_error=str(exc),
                                              branch_archive_retry_at=time.time() + RETRY_SECONDS)
        finally:
            self.lifecycle_archive_lock.release()

    def _lifecycle_loop(self):
        while not self.lifecycle_stop.is_set():
            self.lifecycle_signal.wait(timeout=RETRY_SECONDS)
            self.lifecycle_signal.clear()
            try:
                self.deliver_lifecycle_notices()
                self.reconcile_branch_archives()
            except Exception as exc:
                # Keep the failure visible without dropping recovery work.
                with self.store.lock:
                    self.store.state["lifecycle_error"] = str(exc)
                    self.store.save()
