#!/usr/bin/env python3
"""Prepare the isolated Python runtime used by Feishu Codex Remote."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from gateway_common import REMOTE_STATE, credential_backend


SCRIPT_DIR = Path(__file__).resolve().parent
REQUIREMENTS = SCRIPT_DIR / "requirements.txt"


def runtime_python(system: str | None = None) -> Path:
    current = system or platform.system()
    if current == "Windows":
        return REMOTE_STATE / ".venv" / "Scripts" / "python.exe"
    return REMOTE_STATE / ".venv" / "bin" / "python"


def codex_cli_candidate() -> Path | None:
    override = os.environ.get("CODEX_CLI_PATH", "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path
    if os.name == "nt":
        candidates = sorted(
            (Path.home() / "AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    discovered = shutil.which("codex")
    return Path(discovered) if discovered else None


def preflight() -> dict[str, object]:
    current = platform.system()
    supported = current in {"Windows", "Darwin"}
    codex = codex_cli_candidate()
    return {
        "ok": supported and sys.version_info >= (3, 10),
        "platform": current,
        "platform_supported": supported,
        "python": sys.version.split()[0],
        "python_supported": sys.version_info >= (3, 10),
        "codex_on_path": bool(shutil.which("codex")),
        "codex_available": codex is not None,
        "codex_path": str(codex) if codex else None,
        "runtime_root": str(REMOTE_STATE),
        "runtime_python": str(runtime_python()),
        "credential_backend": credential_backend() if supported else "unsupported",
    }


def install() -> dict[str, object]:
    report = preflight()
    if not report["platform_supported"]:
        raise RuntimeError(f"Unsupported platform: {report['platform']}")
    if not report["python_supported"]:
        raise RuntimeError("Python 3.10 or newer is required")
    target = runtime_python()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    if not target.exists():
        REMOTE_STATE.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(REMOTE_STATE / ".venv")], check=True, **options)
    subprocess.run(
        [str(target), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
        check=True,
        **options,
    )
    report.update({"ok": True, "installed": True, "runtime_python": str(target)})
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "install"), nargs="?", default="check")
    args = parser.parse_args()
    try:
        value = install() if args.command == "install" else preflight()
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0 if value.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
