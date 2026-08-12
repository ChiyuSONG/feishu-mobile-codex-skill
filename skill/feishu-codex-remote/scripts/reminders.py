#!/usr/bin/env python3
"""Small append-only reminder store checked by automatic inspection."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any
import uuid

from gateway_common import REMOTE_STATE


def now() -> datetime:
    return datetime.now().astimezone()


def reminder_path(project_key: str) -> Path:
    return REMOTE_STATE / "projects" / project_key / "reminders.jsonl"


def append_event(project_key: str, event: dict[str, Any]) -> None:
    path = reminder_path(project_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"recorded_at": now().isoformat(timespec="seconds"), **event}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def events(project_key: str) -> list[dict[str, Any]]:
    path = reminder_path(project_key)
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            value = json.loads(raw)
            if isinstance(value, dict):
                values.append(value)
    return values


def active_reminders(project_key: str) -> list[dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for event in events(project_key):
        reminder_id = str(event.get("reminder_id") or "")
        if not reminder_id:
            continue
        kind = event.get("event")
        if kind == "created":
            active[reminder_id] = dict(event)
            active[reminder_id]["last_sent_at"] = None
        elif reminder_id in active and kind == "sent":
            active[reminder_id]["last_sent_at"] = event.get("sent_at")
        elif kind in {"completed", "cancelled"}:
            active.pop(reminder_id, None)
    return list(active.values())


def parse_start(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("start_at must include an explicit UTC offset")
    return parsed


def due_reminders(project_key: str, at: datetime | None = None) -> list[dict[str, Any]]:
    current = at or now()
    due: list[dict[str, Any]] = []
    for reminder in active_reminders(project_key):
        if parse_start(str(reminder["start_at"])) > current:
            continue
        last_sent = reminder.get("last_sent_at")
        if reminder.get("mode") == "once" and last_sent:
            continue
        if last_sent:
            sent_at = datetime.fromisoformat(str(last_sent))
            if sent_at.astimezone().strftime("%Y%m%d%H") == current.astimezone().strftime("%Y%m%d%H"):
                continue
        due.append(reminder)
    return due


def create(project_key: str, text: str, start_at: str, mode: str = "continuous") -> dict[str, Any]:
    parse_start(start_at)
    reminder_id = uuid.uuid4().hex[:12]
    append_event(
        project_key,
        {
            "event": "created",
            "reminder_id": reminder_id,
            "text": text.strip(),
            "start_at": start_at,
            "mode": mode,
        },
    )
    return {"ok": True, "reminder_id": reminder_id, "start_at": start_at, "mode": mode, "text": text.strip()}


def close(project_key: str, reminder_id: str, event: str) -> dict[str, Any]:
    matches = [item for item in active_reminders(project_key) if item["reminder_id"] == reminder_id]
    if len(matches) != 1:
        raise ValueError(f"Active reminder not found: {reminder_id}")
    append_event(project_key, {"event": event, "reminder_id": reminder_id})
    return {"ok": True, "reminder_id": reminder_id, "event": event}


def mark_sent(project_key: str, reminder_id: str, sent_at: datetime | None = None) -> None:
    append_event(
        project_key,
        {"event": "sent", "reminder_id": reminder_id, "sent_at": (sent_at or now()).isoformat(timespec="seconds")},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("--project-key", required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--start-at", required=True, help="ISO 8601 with UTC offset")
    add.add_argument("--mode", choices=("continuous", "once"), default="continuous")
    listing = commands.add_parser("list")
    listing.add_argument("--project-key", required=True)
    for name in ("complete", "cancel"):
        close_parser = commands.add_parser(name)
        close_parser.add_argument("--project-key", required=True)
        close_parser.add_argument("--reminder-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "add":
            value = create(args.project_key, args.text, args.start_at, args.mode)
        elif args.command == "list":
            value = {"ok": True, "reminders": active_reminders(args.project_key)}
        else:
            value = close(args.project_key, args.reminder_id, "completed" if args.command == "complete" else "cancelled")
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
