#!/usr/bin/env python3
"""Install, start, and inspect the per-user Listener launcher."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
from typing import Any

from bootstrap import codex_cli_candidate
from gateway_common import REMOTE_STATE


SCRIPT_DIR = Path(__file__).resolve().parent
LABEL = "feishu-codex-remote.listener"
WINDOWS_TASK = "CodexFeishuRemoteGateway"


def runtime_python(system: str | None = None) -> Path:
    current = system or platform.system()
    if current == "Windows":
        return REMOTE_STATE / ".venv" / "Scripts" / "python.exe"
    return REMOTE_STATE / ".venv" / "bin" / "python"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, **options)


def install_windows() -> dict[str, Any]:
    result = _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT_DIR / "install_listener_task.ps1"),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Could not install Windows Listener task")
    return {"ok": True, "platform": "Windows", "launcher": WINDOWS_TASK, "detail": result.stdout.strip()}


def stop_windows() -> dict[str, Any]:
    result = _run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK])
    return {
        "ok": result.returncode == 0 or "cannot find" in (result.stderr + result.stdout).lower(),
        "platform": "Windows",
        "launcher": WINDOWS_TASK,
        "stopped": result.returncode == 0,
    }


def uninstall_windows() -> dict[str, Any]:
    runner = SCRIPT_DIR / "run_gateway.ps1"
    result = _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT_DIR / "uninstall_listener_task.ps1"),
            "-ExpectedRunner",
            str(runner),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Could not uninstall Windows Listener task")
    detail = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
    return {"ok": True, "platform": "Windows", "launcher": WINDOWS_TASK, **detail}


def mac_domain() -> str:
    return f"gui/{os.getuid()}"


def mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def mac_registered() -> bool:
    return _run(["launchctl", "print", f"{mac_domain()}/{LABEL}"]).returncode == 0


def install_macos() -> dict[str, Any]:
    python = runtime_python("Darwin")
    if not python.exists():
        raise RuntimeError(f"Runtime Python is missing: {python}; run bootstrap.py install first")
    codex = codex_cli_candidate()
    if codex is None:
        raise RuntimeError("Codex CLI is missing; open Codex or set CODEX_CLI_PATH before installing the Listener")
    codex = codex.resolve()
    logs = REMOTE_STATE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(SCRIPT_DIR / "supervisor.py")],
        # launchd does not inherit an interactive shell's environment. Persist
        # the executable discovered during setup instead of hoping `codex` is
        # later available on launchd's restricted PATH.
        "EnvironmentVariables": {"CODEX_CLI_PATH": str(codex)},
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "ThrottleInterval": 15,
        "StandardOutPath": str(logs / "launchd.stdout.log"),
        "StandardErrorPath": str(logs / "launchd.stderr.log"),
    }
    path = mac_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    if path.exists() and path.read_bytes() != rendered and mac_registered():
        result = _run(["launchctl", "bootout", mac_domain(), str(path)])
        if result.returncode != 0:
            raise RuntimeError("Could not unload the existing macOS Listener agent")
    path.write_bytes(rendered)
    if not mac_registered():
        result = _run(["launchctl", "bootstrap", mac_domain(), str(path)])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not register the macOS Listener agent")
    return {"ok": True, "platform": "Darwin", "launcher": LABEL, "plist": str(path), "demand_start_only": True}


def stop_macos() -> dict[str, Any]:
    if not mac_registered():
        return {"ok": True, "platform": "Darwin", "launcher": LABEL, "stopped": False}
    result = _run(["launchctl", "kill", "SIGTERM", f"{mac_domain()}/{LABEL}"])
    return {"ok": result.returncode == 0, "platform": "Darwin", "launcher": LABEL, "stopped": result.returncode == 0}


def uninstall_macos() -> dict[str, Any]:
    path = mac_plist_path()
    if not path.exists() and not mac_registered():
        return {"ok": True, "platform": "Darwin", "launcher": LABEL, "removed": False}
    if path.exists():
        payload = plistlib.loads(path.read_bytes())
        arguments = [str(value) for value in payload.get("ProgramArguments") or []]
        expected = str(SCRIPT_DIR / "supervisor.py")
        if expected not in arguments:
            raise RuntimeError("Existing macOS Listener agent is not owned by this Skill; refusing to remove it")
    if mac_registered():
        result = _run(["launchctl", "bootout", mac_domain(), str(path)])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not unload the macOS Listener agent")
    path.unlink(missing_ok=True)
    return {"ok": True, "platform": "Darwin", "launcher": LABEL, "removed": True}


def install_listener() -> dict[str, Any]:
    current = platform.system()
    if current == "Windows":
        return install_windows()
    if current == "Darwin":
        return install_macos()
    raise RuntimeError(f"Unsupported platform: {current}")


def start_listener() -> dict[str, Any]:
    current = platform.system()
    if current == "Windows":
        result = _run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Could not start Listener task")
        return {"ok": True, "platform": current, "launcher": WINDOWS_TASK}
    if current == "Darwin":
        if not mac_registered():
            install_macos()
        # SessionStart may run whenever Codex is opened or resumed.  Do not use
        # launchctl's `-k` option here: it kills an already-running service
        # before restarting it and could interrupt an active remote task.
        result = _run(["launchctl", "kickstart", f"{mac_domain()}/{LABEL}"])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not start macOS Listener agent")
        return {"ok": True, "platform": current, "launcher": LABEL}
    raise RuntimeError(f"Unsupported platform: {current}")


def stop_listener() -> dict[str, Any]:
    current = platform.system()
    if current == "Windows":
        return stop_windows()
    if current == "Darwin":
        return stop_macos()
    raise RuntimeError(f"Unsupported platform: {current}")


def uninstall_listener() -> dict[str, Any]:
    current = platform.system()
    if current == "Windows":
        return uninstall_windows()
    if current == "Darwin":
        return uninstall_macos()
    raise RuntimeError(f"Unsupported platform: {current}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the Feishu Remote Listener launcher")
    parser.add_argument("command", choices=("install", "start", "stop", "uninstall"), nargs="?", default="install")
    args = parser.parse_args()
    if args.command == "start":
        value = start_listener()
    elif args.command == "stop":
        value = stop_listener()
    elif args.command == "uninstall":
        value = uninstall_listener()
    else:
        value = install_listener()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
