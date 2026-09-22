#!/usr/bin/env python3
"""Cross-platform supervisor for the long-lived Feishu listener."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import time

from gateway_common import REMOTE_STATE


SCRIPT_DIR = Path(__file__).resolve().parent
GATEWAY = SCRIPT_DIR / "remote_gateway.py"
LOG_DIR = REMOTE_STATE / "logs"


def log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "supervisor.log").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {text}\n")


def run_once() -> int:
    logs = REMOTE_STATE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    if os.name == "nt":
        python = python.with_name("python.exe")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with (logs / "gateway-console.log").open("ab") as output:
        return subprocess.run(
            [str(python), str(GATEWAY), "run"], stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, check=False, **options,
        ).returncode


def main() -> int:
    while True:
        log("supervisor starting gateway")
        try:
            code = run_once()
        except OSError as exc:
            code = str(exc)
        log(f"gateway exited with code {code}; restarting in 15 seconds")
        time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
