#!/usr/bin/env python3
"""Cross-platform supervisor for the long-lived Feishu listener."""

from __future__ import annotations

from datetime import datetime
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


def main() -> int:
    while True:
        log("supervisor starting gateway")
        result = subprocess.run([sys.executable, str(GATEWAY), "run"], check=False)
        log(f"gateway exited with code {result.returncode}; restarting in 15 seconds")
        time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
