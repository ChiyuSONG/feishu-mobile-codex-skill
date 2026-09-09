"""Classify a terminal provider event, not text mentioned by a user or model."""
from __future__ import annotations

import json
from pathlib import Path


class ProviderCapacityError(RuntimeError):
    def __init__(self, event_log: Path):
        self.event_log = event_log
        super().__init__("Selected model is at capacity")


def terminal_capacity_failure(event_log: Path) -> bool:
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
            return False
        if event.get("type") != "turn.failed":
            continue
        error = event.get("error")
        message = event.get("message") or (error.get("message", "") if isinstance(error, dict) else error)
        return str(message).strip().lower().startswith("selected model is at capacity")
    return False
