"""Provisioning profiles: fixed patrol, one-time source-task inheritance."""
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing

from gateway_common import GatewayError

PATROL_DEFAULTS = {"model": "gpt-5.6-terra", "reasoning_effort": "medium",
                   "service_tier": "default", "permission_mode": "full-access"}


def source_task_profile(thread_id, working_directory, *, database=None):
    if not thread_id:
        return {}
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    database = Path(database) if database else home / "state_5.sqlite"
    if not database.is_file():
        raise GatewayError("Cannot read source task configuration; confirm the missing remote profile fields")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT rollout_path,cwd FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row or os.path.normcase(str(Path(row[1]).resolve())) != os.path.normcase(str(Path(working_directory).resolve())):
        raise GatewayError("Source task does not match the selected working directory")
    context = None
    with Path(row[0]).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn_context":
                context = event.get("payload") or {}
    if context is None:
        return {}
    result = {}
    for key, names in {"model": ("model",), "reasoning_effort": ("effort", "reasoning_effort"),
                       "service_tier": ("service_tier",)}.items():
        for name in names:
            if context.get(name):
                result[key] = str(context[name])
                break
    sandbox = context.get("sandbox_policy") or {}
    sandbox = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    approval = context.get("approval_policy")
    if sandbox == "danger-full-access" and approval == "never":
        result["permission_mode"] = "full-access"
    elif approval in {"auto-review", "on-request-auto-review"}:
        result["permission_mode"] = "auto-review"
    elif sandbox == "workspace-write" and approval == "never":
        result["permission_mode"] = "project-only-auto"
    return result


def initial_profiles(existing, inherited, overrides):
    """Existing explicit fields win; unresolved C fields require confirmation."""
    result = {}
    missing = []
    for key, default in PATROL_DEFAULTS.items():
        result["patrol_" + key] = str(existing.get("patrol_" + key) or "").strip() or default
        # Permissions are an independent, disclosed product default. Do not
        # accidentally copy a source task's transient sandbox into a new worker.
        # Existing and explicitly requested lower modes remain authoritative.
        source = "full-access" if key == "permission_mode" else inherited.get(key)
        value = next((str(v).strip() for v in (existing.get("agent_" + key), overrides.get(key), source) if v and str(v).strip()), None)
        if value:
            result["agent_" + key] = value
        else:
            missing.append(key)
    if missing:
        raise GatewayError("Source task profile incomplete; confirm " + ", ".join(missing))
    if result["agent_permission_mode"] == "workspace-full-auto":
        result["agent_permission_mode"] = "full-access"
    if result["agent_service_tier"] not in {"default", "fast", "priority"}:
        raise GatewayError("Unsupported source service tier; confirm remote configuration")
    if result["agent_permission_mode"] not in {"full-access", "project-only-auto", "auto-review"}:
        raise GatewayError("Unsupported source permission mode; confirm remote configuration")
    return result
