#!/usr/bin/env python3
"""Preview and remove Feishu Remote Codex components without touching Feishu content."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import gateway_common
import install_startup_hook
import listener_control


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
AUTOMATIONS_ROOT = CODEX_HOME / "automations"


def installed_skill_target() -> Path | None:
    candidates = {
        (Path.home() / ".agents" / "skills" / "feishu-codex-remote").resolve(),
        (CODEX_HOME / "skills" / "feishu-codex-remote").resolve(),
    }
    current = SKILL_ROOT.resolve()
    return current if current in candidates else None


def load_config() -> dict[str, Any]:
    value = gateway_common.load_json(gateway_common.CONFIG_PATH, {})
    if not isinstance(value, dict):
        raise ValueError(f"Invalid gateway config: {gateway_common.CONFIG_PATH}")
    return value


def project_runtime(project_key: str) -> Path:
    return gateway_common.REMOTE_STATE / "projects" / project_key


def active_messages(project_key: str) -> list[dict[str, str]]:
    state = gateway_common.load_json(project_runtime(project_key) / "state.json", {})
    result: list[dict[str, str]] = []
    for message_id, item in (state.get("messages") or {}).items():
        status = str((item or {}).get("status") or "")
        if status in {"pending", "processing"}:
            result.append({"project_key": project_key, "message_id": str(message_id), "status": status})
    return result


def _toml_string(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    prefix = key + " = "
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            try:
                value = json.loads(line[len(prefix) :])
            except json.JSONDecodeError:
                return ""
            return str(value)
    return ""


def owned_automation(project_key: str, project: dict[str, Any]) -> Path | None:
    automation_id = str(project.get("automation_id") or "").strip()
    candidates: list[Path] = []
    if automation_id:
        candidates.append(AUTOMATIONS_ROOT / automation_id / "automation.toml")
    if AUTOMATIONS_ROOT.exists():
        candidates.extend(AUTOMATIONS_ROOT.glob("*/automation.toml"))
    expected_script_names = ("sync_feishu.ps1", "sync_feishu.py")
    key_markers = (f"-ProjectKey {project_key}", f"--project-key {project_key}")
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        prompt = _toml_string(candidate, "prompt")
        if any(name in prompt for name in expected_script_names) and any(marker in prompt for marker in key_markers):
            return candidate.parent
    return None


def build_preview(scope: str, project_key: str | None, purge: bool) -> dict[str, Any]:
    config = load_config()
    projects = config.get("projects") or {}
    if not isinstance(projects, dict):
        raise ValueError("Gateway projects registry is invalid")
    if scope == "project":
        if not project_key or project_key not in projects:
            raise ValueError(f"Unknown project binding: {project_key or '<missing>'}")
        selected = {project_key: projects[project_key]}
    else:
        selected = dict(projects)
    active = [item for key in selected for item in active_messages(key)]
    automations = []
    for key, project in selected.items():
        path = owned_automation(key, project)
        if path is not None:
            automations.append(str(path))
    last_project = scope == "all" or len(projects) == len(selected)
    removes = {
        "project_bindings": sorted(selected),
        "automation_directories": sorted(set(automations)),
        "listener_launcher": last_project,
        "session_start_hook": last_project,
        "installed_skill_directory": (
            str(installed_skill_target()) if last_project and installed_skill_target() else None
        ),
    }
    preserves = ["Feishu groups", "Feishu conversations", "generated Feishu documents"]
    if purge:
        removes["local_runtime_and_credentials"] = [
            str(gateway_common.REMOTE_STATE),
            str(gateway_common.PUBLISH_STATE),
        ]
    else:
        preserves.append("local recovery copy of configuration, queues, logs, and credentials")
    return {
        "ok": True,
        "mode": "preview",
        "scope": scope,
        "project_key": project_key,
        "active_messages": active,
        "remove": removes,
        "preserve": preserves,
        "requires_apply_and_confirm": True,
        "purge_local_data": purge,
    }


def recovery_root() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return gateway_common.REMOTE_STATE / "recovery" / f"uninstall-{stamp}"


def _archive(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return True


def _remove_automation(path: str) -> bool:
    target = Path(path).resolve()
    root = AUTOMATIONS_ROOT.resolve()
    if target.parent != root or not (target / "automation.toml").is_file():
        raise ValueError(f"Unsafe Automation target: {target}")
    shutil.rmtree(target)
    return True


def apply_uninstall(preview: dict[str, Any], abandon_active: bool) -> dict[str, Any]:
    active = preview["active_messages"]
    if active and not abandon_active:
        raise RuntimeError(f"Refusing to uninstall with active messages: {active}")
    scope = str(preview["scope"])
    purge = bool(preview["purge_local_data"])
    config = load_config()
    projects = config.get("projects") or {}
    selected = list(preview["remove"]["project_bindings"])
    recovery = recovery_root()
    actions: dict[str, Any] = {"automations_removed": [], "bindings_removed": []}

    # Stop the shared launcher before changing its registry. Restart it only if
    # another binding remains.
    try:
        actions["listener_stopped"] = listener_control.stop_listener()
    except Exception as exc:
        actions["listener_stop_warning"] = str(exc)

    if not purge:
        recovery.mkdir(parents=True, exist_ok=True)
        if gateway_common.CONFIG_PATH.exists():
            shutil.copy2(gateway_common.CONFIG_PATH, recovery / "config.before.json")

    for path in preview["remove"]["automation_directories"]:
        if _remove_automation(path):
            actions["automations_removed"].append(path)

    if scope == "project":
        key = selected[0]
        if not purge:
            _archive(project_runtime(key), recovery / "projects" / key)
        elif project_runtime(key).exists():
            shutil.rmtree(project_runtime(key))
        projects.pop(key, None)
        config["projects"] = projects
        gateway_common.atomic_write_json(gateway_common.CONFIG_PATH, config)
        actions["bindings_removed"].append(key)
    else:
        if not purge:
            _archive(gateway_common.REMOTE_STATE / "projects", recovery / "projects")
            if gateway_common.CONFIG_PATH.exists():
                gateway_common.CONFIG_PATH.unlink()
        actions["bindings_removed"].extend(selected)

    if preview["remove"]["session_start_hook"]:
        actions["hook"] = install_startup_hook.uninstall()
    if preview["remove"]["listener_launcher"]:
        actions["launcher"] = listener_control.uninstall_listener()
    elif projects:
        actions["listener_restarted"] = listener_control.start_listener()

    if purge:
        if scope != "all":
            raise ValueError("--purge-local-data is supported only with --scope all")
        shutil.rmtree(gateway_common.REMOTE_STATE, ignore_errors=True)
        gateway_common.delete_protected(gateway_common.SECRET_PATH)
        tokens = gateway_common.PUBLISH_STATE / "tokens.bin"
        gateway_common.delete_protected(tokens)
        shutil.rmtree(gateway_common.PUBLISH_STATE, ignore_errors=True)
        actions["local_data_purged"] = True
        recovery_path = None
    else:
        recovery_path = str(recovery)

    return {
        "ok": True,
        "mode": "applied",
        "scope": scope,
        "actions": actions,
        "recovery_path": recovery_path,
        "remove_installed_skill_after_command": preview["remove"]["installed_skill_directory"],
        "feishu_content_deleted": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Safely uninstall Feishu Remote Codex")
    result.add_argument("--scope", choices=("project", "all"), default="all")
    result.add_argument("--project-key")
    result.add_argument("--apply", action="store_true")
    result.add_argument("--confirm", action="store_true")
    result.add_argument("--purge-local-data", action="store_true")
    result.add_argument("--abandon-active", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.purge_local_data and args.scope != "all":
            raise ValueError("--purge-local-data requires --scope all")
        preview = build_preview(args.scope, args.project_key, args.purge_local_data)
        if args.apply:
            if not args.confirm:
                raise ValueError("--apply requires --confirm after reviewing the preview")
            value = apply_uninstall(preview, args.abandon_active)
        else:
            value = preview
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
