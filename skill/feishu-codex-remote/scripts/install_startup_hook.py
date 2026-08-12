#!/usr/bin/env python3
"""Install a user-level Codex SessionStart hook without replacing other hooks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
from typing import Any

from listener_control import runtime_python


SCRIPT_DIR = Path(__file__).resolve().parent
STATUS_MESSAGE = "Starting Feishu Remote Listener"
MATCHER = "^(startup|resume)$"
DESCRIPTION = "Start registered Feishu Remote Codex listeners when Codex starts or resumes."
OBSOLETE_DESCRIPTIONS = {
    "No SessionStart process. Feishu sync starts on explicit request or hourly reconciliation.",
}


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".codex"


def hook_path() -> Path:
    return codex_home() / "hooks.json"


def hook_command(system: str | None = None) -> str:
    parts = [str(runtime_python(system)), str(SCRIPT_DIR / "listener_control.py"), "start"]
    return subprocess.list2cmdline(parts) if (system or platform.system()) == "Windows" else shlex.join(parts)


def hook_group(system: str | None = None) -> dict[str, Any]:
    return {
        "matcher": MATCHER,
        "hooks": [
            {
                "type": "command",
                "command": hook_command(system),
                "timeout": 15,
                "statusMessage": STATUS_MESSAGE,
            }
        ],
    }


def is_ours(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    return any(
        isinstance(handler, dict)
        and handler.get("statusMessage") == STATUS_MESSAGE
        and "listener_control.py" in str(handler.get("command") or "")
        and str(handler.get("command") or "").rstrip().endswith("start")
        for handler in group.get("hooks", [])
    )


def merged_config(existing: dict[str, Any], system: str | None = None) -> dict[str, Any]:
    result = dict(existing)
    if not result.get("description") or result.get("description") in OBSOLETE_DESCRIPTIONS:
        result["description"] = DESCRIPTION
    hooks = dict(result.get("hooks") or {})
    session_start = list(hooks.get("SessionStart") or [])
    replacement = hook_group(system)
    indexes = [index for index, group in enumerate(session_start) if is_ours(group)]
    if indexes:
        session_start[indexes[0]] = replacement
        session_start = [group for index, group in enumerate(session_start) if index == indexes[0] or not is_ours(group)]
    else:
        session_start.append(replacement)
    hooks["SessionStart"] = session_start
    result["hooks"] = hooks
    return result


def install(path: Path | None = None, system: str | None = None) -> dict[str, Any]:
    target = path or hook_path()
    if target.exists():
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Codex hooks file must contain a JSON object: {target}")
    else:
        value = {}
    updated = merged_config(value, system)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return {
        "ok": True,
        "path": str(target),
        "event": "SessionStart",
        "matcher": MATCHER,
        "requires_trust_review": True,
    }


def without_ours(existing: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    result = dict(existing)
    hooks = dict(result.get("hooks") or {})
    session_start = list(hooks.get("SessionStart") or [])
    filtered = [group for group in session_start if not is_ours(group)]
    removed = len(filtered) != len(session_start)
    if filtered:
        hooks["SessionStart"] = filtered
    else:
        hooks.pop("SessionStart", None)
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    if result.get("description") == DESCRIPTION:
        result.pop("description", None)
    return result, removed


def uninstall(path: Path | None = None) -> dict[str, Any]:
    target = path or hook_path()
    if not target.exists():
        return {"ok": True, "path": str(target), "removed": False}
    value = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Codex hooks file must contain a JSON object: {target}")
    updated, removed = without_ours(value)
    if removed:
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    return {"ok": True, "path": str(target), "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Feishu Listener Codex startup hook")
    parser.add_argument("command", choices=("install", "check", "uninstall"), nargs="?", default="check")
    args = parser.parse_args()
    target = hook_path()
    if args.command == "check":
        configured = False
        if target.exists():
            value = json.loads(target.read_text(encoding="utf-8"))
            configured = any(is_ours(group) for group in ((value.get("hooks") or {}).get("SessionStart") or []))
        result = {"ok": True, "path": str(target), "configured": configured}
    elif args.command == "install":
        result = install(target)
    else:
        result = uninstall(target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
