"""Classify a terminal provider event, not text mentioned by a user or model."""
from __future__ import annotations

import json
from pathlib import Path


class ProviderCapacityError(RuntimeError):
    def __init__(self, event_log: Path, kind="at_capacity"):
        self.event_log = event_log
        self.kind = kind
        super().__init__("Codex rate limit reached" if kind == "rate_limit" else "Selected model is at capacity")


def terminal_provider_failure(event_log: Path):
    # Called only after the native process exits. Intermediate errors may be
    # followed by native retries; the latest terminal event owns the outcome.
    with event_log.open("rb") as handle:
        handle.seek(max(0, event_log.stat().st_size - 65536))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            return None
        if event.get("type") != "turn.failed":
            continue
        error = event.get("error")
        message = event.get("message") or (error.get("message", "") if isinstance(error, dict) else error)
        code = str(error.get("code", "") if isinstance(error, dict) else "").lower()
        message = str(message).strip().lower()
        if code in {"usage_limit_reached", "rate_limit_exceeded", "rate_limit"} or any(
            token in message for token in ("usage limit", "rate limit", "rate_limit", "quota exceeded")):
            return "rate_limit"
        if message.startswith("selected model is at capacity") or code == "model_at_capacity":
            return "at_capacity"
        return None
    return None


def terminal_capacity_failure(event_log: Path) -> bool:
    return terminal_provider_failure(event_log) == "at_capacity"
