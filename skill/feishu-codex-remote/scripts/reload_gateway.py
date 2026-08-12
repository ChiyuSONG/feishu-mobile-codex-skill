#!/usr/bin/env python3
"""Cross-platform graceful Listener reload entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from listener_control import start_listener


SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-timeout", type=int, default=3600)
    parser.add_argument("--request-only", action="store_true")
    args = parser.parse_args()
    start_listener()
    command = [
        sys.executable,
        str(SCRIPT_DIR / "remote_gateway.py"),
        "reload",
        "--wait-timeout",
        str(args.wait_timeout),
    ]
    if args.request_only:
        command.append("--request-only")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
