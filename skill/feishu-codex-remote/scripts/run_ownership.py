"""Durable model-run ownership; restart never equates a dead listener with a dead task.

Windows uses GetProcessTimes on the queried handle. Darwin uses the public XNU
proc_bsdinfo start timeval; Linux uses boot_id and /proc start ticks. No signals,
process-age thresholds, model retries or new background services are involved.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import sys
import uuid

from gateway_common import message_text
from gateway_lifecycle import timestamp as now_iso
from task_contract import prepared_result, restore_result


def _windows_snapshot(pid):
    from ctypes import wintypes
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = dll.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return {"state": "absent" if error == 87 else "unknown", "error": error}
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not dll.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return {"state": "unknown", "error": ctypes.get_last_error()}
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        wait = dll.WaitForSingleObject(handle, 0)
        return {"state": {258: "alive", 0: "exited"}.get(wait, "unknown"),
                "identity": {"pid": pid, "created": str(created), "platform": "win32"}}
    finally:
        dll.CloseHandle(handle)


class _DarwinBSDInfo(ctypes.Structure):
    # Apple xnu/bsd/sys/proc_info.h, PROC_PIDTBSDINFO (3).
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")] + [
        ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def _darwin_snapshot(pid):
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                               ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    info = _DarwinBSDInfo()
    size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        error = ctypes.get_errno()
        return {"state": "absent" if error == errno.ESRCH else "unknown", "error": error}
    if info.pid != pid or not info.start_sec:
        return {"state": "unknown", "error": "invalid_process_identity"}
    return {"state": "exited" if info.status == 5 else "alive", "identity": {
        "pid": pid, "created": f"{info.start_sec}:{info.start_usec}", "platform": "darwin"}}


def _linux_snapshot(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"state": "exited" if fields[0] in {"Z", "X"} else "alive", "identity": {
            "pid": pid, "created": boot + ":" + fields[19], "platform": "linux"}}
    except FileNotFoundError:
        return {"state": "absent"}


def process_snapshot(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return {"state": "absent"}
        if os.name == "nt":
            return _windows_snapshot(pid)
        if sys.platform == "darwin":
            return _darwin_snapshot(pid)
        if sys.platform.startswith("linux"):
            return _linux_snapshot(pid)
        return {"state": "unknown", "error": "unsupported_process_identity"}
    except (OSError, ValueError, TypeError, IndexError) as exc:
        return {"state": "unknown", "error": type(exc).__name__}


def current_identity():
    return process_snapshot(os.getpid()).get("identity") or {"pid": os.getpid(), "created": ""}


def identity_state(identity):
    if not isinstance(identity, dict) or not identity.get("created"):
        return "unknown"
    observed = process_snapshot(identity.get("pid"))
    if observed["state"] in {"absent", "unknown"}:
        return observed["state"]
    if observed.get("identity") != identity:
        return "replaced"
    return observed["state"]


def is_branch(row):
    return message_text(row.get("content")).lstrip().startswith("#")


def finished_result(record):
    """Require a complete native terminal event from this exact recorded run."""
    event_log = Path(record["event_log"])
    thread_id = ""
    terminal = None
    with event_log.open("rb") as source:
        for line in source:
            stripped = line.strip()
            if not stripped.startswith(b"{"):
                # stderr shares this file with native JSONL events.
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                if b'"type"' in stripped:
                    raise ValueError("incomplete_native_event")
                continue
            if not isinstance(event, dict) or not event.get("type"):
                continue
            if not line.endswith(b"\n"):
                raise ValueError("incomplete_native_event")
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = str(event.get("thread_id") or "")
            if kind == "turn.started":
                terminal = None
            elif kind in {"turn.completed", "turn.failed"}:
                terminal = kind
    if terminal != "turn.completed" or not thread_id:
        raise ValueError("native_completion_unconfirmed")
    expected = record.get("thread_id")
    if expected and thread_id != expected:
        raise ValueError("native_thread_mismatch")
    final_path = Path(record["final_path"])
    answer = final_path.read_text(encoding="utf-8").strip()
    if not answer:
        raise ValueError("empty_final_artifact")
    return prepared_result(answer, thread_id, final_path, record["message_ids"])


class RunOwnershipStore:
    """Mixin using the existing JSON store lock and atomic save only."""
    def claim_run(self, message_ids):
        record = {"version": 1, "run_id": uuid.uuid4().hex, "message_ids": list(message_ids),
                  "owner": current_identity(), "phase": "claimed", "claimed_at": now_iso()}
        for mid in message_ids:
            row = self.state["messages"][mid]
            row["run_ownership"] = dict(record)
            row.pop("run_recovery", None)
            row.pop("run_recovery_error", None)
        return record

    def _update_run(self, message_ids, **fields):
        with self.lock:
            rows = self.state["messages"]
            record = dict(rows[message_ids[0]].get("run_ownership") or {})
            if not record or record.get("message_ids") != list(message_ids):
                raise ValueError("run source identity mismatch")
            record.update(fields)
            for mid in message_ids:
                if rows[mid].get("run_ownership", {}).get("run_id") != record["run_id"]:
                    raise ValueError("run claim changed")
            for mid in message_ids:
                rows[mid]["run_ownership"] = dict(record)
            self.save()

    def prepare_run(self, message_ids, event_log, thread_id):
        with self.lock:
            if not self.state["messages"][message_ids[0]].get("run_ownership"):
                self.claim_run(message_ids)
            self._update_run(message_ids, phase="launch_pending", event_log=str(event_log),
                             final_path=str(Path(event_log).with_name("final.md")), thread_id=thread_id)

    def record_child(self, message_ids, process):
        if not message_ids:
            return
        snapshot = process_snapshot(process.pid)
        self._update_run(message_ids, phase="running", child=snapshot.get("identity") or {
            "pid": int(process.pid), "created": ""}, child_identity_error=snapshot.get("error"))

    def begin_process(self, message_ids):
        if not message_ids:
            return
        with self.lock:
            if not self.state["messages"][message_ids[0]].get("run_ownership"):
                self.claim_run(message_ids)
            self._update_run(message_ids, phase="launch_pending")

    def record_child_exit(self, message_ids, returncode):
        if not message_ids:
            return
        self._update_run(message_ids, phase="exited", returncode=returncode, exited_at=now_iso())

    def settle_run(self, message_ids):
        with self.lock:
            rows = self.state["messages"]
            if all(rows[mid].get("status") != "processing" for mid in message_ids):
                record = rows[message_ids[0]].get("run_ownership")
                if record:
                    self._update_run(message_ids, phase="handled")
                for mid in message_ids:
                    rows[mid].pop("run_recovery", None)
                self.save()

    def reconcile_runs(self, active_message_ids=()):
        """Nonblocking observation from the existing control cycle, never a model call."""
        with self.lock:
            rows = self.state.get("messages", {})
            active = set(active_message_ids)
            checked = set()
            changed = False
            for mid, row in rows.items():
                if row.get("status") != "processing" or mid in active or mid in checked:
                    continue
                record = row.get("run_ownership") or {}
                ids = record.get("message_ids") or (row.get("prepared_reply") or {}).get("message_ids") or [mid]
                checked.update(ids)
                if any(value in active for value in ids):
                    continue
                group = [rows.get(value) for value in ids]
                if (mid not in ids or any(not value or value.get("status") != "processing" for value in group)
                        or (record and any(value.get("run_ownership") != record for value in group))):
                    state, reason = "outcome_unknown", "inconsistent_run_claim"
                    group = [row]
                elif record and record.get("owner") == current_identity() and not row.get("run_recovery"):
                    continue  # The current process still owns its normal completion path.
                else:
                    state, reason = "outcome_unknown", "missing_run_identity"
                    child_state = identity_state(record.get("child")) if record else "unknown"
                    owner_state = identity_state(record.get("owner")) if record else "unknown"
                    if child_state == "alive":
                        state, reason = "waiting", "original_child_alive"
                    elif record and owner_state == "alive":
                        state, reason = "waiting", "original_listener_alive"
                    elif record and child_state == "unknown" and record.get("phase") in {"running", "launch_pending"}:
                        reason = "child_identity_unconfirmed"
                    else:
                        try:
                            receipt = row.get("prepared_reply")
                            if receipt:
                                if any(value.get("prepared_reply") != receipt for value in group):
                                    raise ValueError("inconsistent_prepared_batch")
                                restore_result(receipt, ids)
                            elif record.get("phase") == "claimed" and owner_state in {"absent", "exited", "replaced"}:
                                if any(value.get("branch_state") == "fork_outcome_unknown" for value in group):
                                    raise ValueError("branch_fork_outcome_unknown")
                                receipt = None  # Durable proof that model launch was never submitted.
                            elif record and (child_state in {"absent", "exited", "replaced"}
                                             or (record.get("phase") == "exited" and record.get("returncode") == 0)):
                                receipt = finished_result(record)
                            else:
                                raise ValueError(reason)
                            if receipt:
                                # Native completion proves an available result,
                                # not that outcome/recovery review has accepted it.
                                field = "prepared_reply" if row.get("prepared_reply") else "recovered_model_result"
                                for value in group:
                                    value[field] = receipt
                            state, reason = "ready", "retained_result" if receipt else "model_not_submitted"
                        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                            reason = str(exc)
                for value in group:
                    if value.get("run_recovery") != state or value.get("run_recovery_error") != reason:
                        value.update(run_recovery=state, run_recovery_error=reason)
                        changed = True
            if changed:
                self.save()
            return changed

    def take_recovered_run(self, *, branch=False, message_id=None):
        with self.lock:
            if not self.admission_open or self.pause_requested():
                return []
            rows = self.state.get("messages", {})
            candidates = sorted((row for row in rows.values() if row.get("status") == "processing"
                                 and is_branch(row) == branch),
                                key=lambda row: (int(row.get("create_time") or 0), row["message_id"]))
            if message_id is not None:
                candidates = [row for row in candidates if row["message_id"] == message_id]
            if not candidates or candidates[0].get("run_recovery") != "ready":
                return []
            first = candidates[0]
            record = first.get("run_ownership") or {}
            ids = record.get("message_ids") or (first.get("prepared_reply") or {}).get("message_ids") or [first["message_id"]]
            if any(rows[mid].get("run_recovery") != "ready" for mid in ids):
                return []
            if not branch and any(row["message_id"] not in ids for row in candidates):
                return []
            for mid in ids:
                rows[mid]["run_recovery"] = "resuming"
            self.save()
            return [dict(rows[mid]) for mid in ids]
