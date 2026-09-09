"""Create persistent Codex thread metadata without running a model turn.

Protocol: https://developers.openai.com/codex/app-server/
Only initialize, initialized, and thread/fork, thread/start or thread/archive are sent.
No transcript is logged.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any

from history_fork_snapshot import codex_home, snapshot_rollout


DEFAULT_TIMEOUT_SECONDS = 30.0
_SHUTDOWN_GRACE_SECONDS = 2.0
_CREATE_NO_WINDOW = 0x08000000


class ThreadForkError(RuntimeError):
    """A fork could not be confirmed; callers must not substitute the parent ID."""

    mutation_submitted = False


class ThreadForkRPCError(ThreadForkError):
    def __init__(self, method: str, code: int | None, *, projection_ordinals: tuple[int, int] | None = None):
        self.method = method
        self.code = code
        self.projection_ordinals = projection_ordinals
        super().__init__(f"Codex {method} RPC error (code={code})")


class ThreadForkEOFError(ThreadForkError):
    """App-server closed its protocol stream before the response arrived."""


class ThreadForkTimeoutError(ThreadForkError):
    """The configurable deadline for the RPC exchange expired."""


class ThreadForkProtocolError(ThreadForkError):
    """App-server returned an invalid or inconsistent response."""


def _hidden_process_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": _CREATE_NO_WINDOW, "startupinfo": startup}


def _read_messages(stream: Any, messages: queue.Queue) -> None:
    try:
        for line in stream:
            if line.strip():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    messages.put(("invalid", None))
                    return
                messages.put(("message", value))
    except (OSError, ValueError, UnicodeError):
        messages.put(("invalid", None))
    finally:
        messages.put(("eof", None))


def _send(process: subprocess.Popen, message: dict, method: str) -> None:
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        process.stdin.flush()
    except (OSError, ValueError):
        raise ThreadForkEOFError(f"Codex {method}: protocol input closed") from None


def _response(messages: queue.Queue, request_id: int, method: str, deadline: float) -> dict:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ThreadForkTimeoutError(f"Codex {method}: RPC deadline expired")
        try:
            kind, message = messages.get(timeout=remaining)
        except queue.Empty:
            raise ThreadForkTimeoutError(f"Codex {method}: RPC deadline expired") from None
        if kind == "eof":
            raise ThreadForkEOFError(f"Codex {method}: EOF before response")
        if kind == "invalid" or not isinstance(message, dict):
            raise ThreadForkProtocolError(f"Codex {method}: invalid JSON-RPC response")
        if message.get("id") != request_id:
            # Notifications and other request IDs can be interleaved with replies.
            continue
        if "error" in message:
            error = message["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if type(code) is not int:
                code = None
            # Preserve only this pre-creation fault signature, never server text.
            text = error.get("message", "") if isinstance(error, dict) else ""
            mismatch = re.fullmatch(
                r"failed to prepare paginated fork: thread history projection for "
                r"[0-9a-f-]{36} expected ordinal (\d+), got (\d+)",
                text if isinstance(text, str) else "",
            )
            raise ThreadForkRPCError(method, code,
                                     projection_ordinals=tuple(map(int, mismatch.groups())) if mismatch else None)
        result = message.get("result")
        if not isinstance(result, dict):
            raise ThreadForkProtocolError(f"Codex {method}: missing result object")
        return result


def _cleanup(process: subprocess.Popen, reader: threading.Thread) -> None:
    # EOF permits app-server to finish persisting the already acknowledged fork.
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            # Also remove descendants if an unresponsive app-server started them.
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_SHUTDOWN_GRACE_SECONDS,
                    check=False,
                    **_hidden_process_options(),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
    reader.join(timeout=_SHUTDOWN_GRACE_SECONDS)
    process.stdout.close()


def fork_thread(
    parent_thread_id: str,
    codex_path: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Return a persistent child ID, copying the parent's saved history once.

    ``timeout_seconds`` is the total initialize/fork RPC deadline, not a model
    generation timeout. Cleanup may additionally need bounded shutdown grace.
    A timeout after sending fork is ambiguous: it may already have persisted a
    child. This helper never retries or deletes a possibly completed fork.
    """
    if not isinstance(parent_thread_id, str) or not parent_thread_id.strip():
        raise ValueError("parent_thread_id must be a nonempty string")
    try:
        return _thread_rpc(
            "thread/fork", {"threadId": parent_thread_id, "excludeTurns": True}, codex_path, cwd,
            timeout_seconds, parent_thread_id=parent_thread_id,
        )
    except ThreadForkRPCError as exc:
        if exc.method != "thread/fork" or exc.code != -32603 or not exc.projection_ordinals:
            raise
        # Only a known pre-creation rejection permits another request. EOF,
        # timeout and unknown errors must never produce an untracked second fork.
        try:
            snapshot = snapshot_rollout(parent_thread_id, codex_home() / "feishu-fork-snapshots")
        except (OSError, ValueError, sqlite3.Error):
            raise exc from None
        child_id = _thread_rpc(
            "thread/fork", {"threadId": parent_thread_id, "path": str(snapshot), "excludeTurns": True},
            codex_path, cwd, timeout_seconds, parent_thread_id=parent_thread_id,
        )
        # Retain an ambiguous import for reconciliation; successful forks own
        # their persisted rollout. Each concurrent RPC owns a unique snapshot.
        try:
            snapshot.unlink(missing_ok=True)
        except OSError:
            pass
        return child_id


def start_thread(
    codex_path: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Create a persistent empty root for a genuinely uninitialized main lane.

    The caller must serialize this with main-thread initialization and refuse
    to replace missing/corrupt prior state. This helper does not inspect local
    sessions, choose whether bootstrap is appropriate, or retry unknown results.
    Optional model settings should be those already selected for the main lane.
    """
    params = {"cwd": os.fspath(cwd)}
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be nonempty when provided")
        params["model"] = model
    return _thread_rpc(
        "thread/start", params, codex_path, cwd, timeout_seconds,
        reasoning_effort=reasoning_effort,
    )


def archive_thread(
    thread_id: str,
    codex_path: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Archive a finished temporary thread while preserving its history."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id must be a nonempty string")
    return _thread_rpc("thread/archive", {"threadId": thread_id}, codex_path, cwd,
                       timeout_seconds)


def _thread_rpc(
    method: str,
    params: dict[str, Any],
    codex_path: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
    timeout_seconds: float,
    *,
    parent_thread_id: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if not os.fspath(codex_path):
        raise ValueError("codex_path must not be empty")
    command = [os.fspath(codex_path)]
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise ValueError("reasoning_effort must be nonempty when provided")
        # Use the existing Codex CLI config override, not an unverified RPC field.
        command.extend(["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
    command.append("app-server")

    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            command,
            cwd=os.fspath(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            shell=False,
            **_hidden_process_options(),
        )
    except OSError:
        raise ThreadForkError("Could not start Codex app-server") from None

    messages: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=_read_messages, args=(process.stdout, messages), daemon=True,
        name="codex-fork-rpc-reader",
    )
    reader.start()
    mutation_submitted = False
    try:
        _send(process, {
            "id": 0,
            "method": "initialize",
            "params": {"capabilities": {"experimentalApi": bool(params.get("path"))}, "clientInfo": {
                "name": "feishu_thread_fork", "title": "Feishu Thread Fork", "version": "1.0.0",
            }},
        }, "initialize")
        _response(messages, 0, "initialize", deadline)
        _send(process, {"method": "initialized", "params": {}}, "initialized")
        mutation_submitted = True
        _send(process, {
            "id": 1, "method": method, "params": params,
        }, method)
        result = _response(messages, 1, method, deadline)
        if method == "thread/archive":
            return str(params["threadId"])
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ThreadForkProtocolError(f"Codex {method}: missing thread object")
        child_id = thread.get("id")
        if not isinstance(child_id, str) or not child_id.strip() or child_id == parent_thread_id:
            raise ThreadForkProtocolError(f"Codex {method}: missing or non-child thread ID")
        if thread.get("forkedFromId") not in (None, parent_thread_id):
            raise ThreadForkProtocolError(f"Codex {method}: parent provenance mismatch")
        if thread.get("ephemeral") is True:
            raise ThreadForkProtocolError(f"Codex {method}: unexpected ephemeral thread")
        return child_id
    except ThreadForkError as exc:
        exc.mutation_submitted = mutation_submitted
        raise
    finally:
        _cleanup(process, reader)
