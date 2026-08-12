#!/usr/bin/env python3
"""Cross-platform automatic inspection entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from listener_control import start_listener


SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--wait-timeout", type=int, default=3600)
    parser.add_argument("--request-only", action="store_true")
    parser.add_argument("--report-usage", action="store_true")
    parser.add_argument("--first-inspection-message", action="store_true")
    parser.add_argument("--inspection-report", action="store_true")
    args = parser.parse_args()
    start_listener()
    command = [
        sys.executable,
        str(SCRIPT_DIR / "remote_gateway.py"),
        "sync",
        "--project-key",
        args.project_key,
        "--wait-timeout",
        str(args.wait_timeout),
    ]
    if args.request_only or args.report_usage or args.inspection_report:
        command.append("--request-only")
    if args.report_usage:
        command.append("--report-usage")
    if args.first_inspection_message:
        command.append("--first-inspection-message")
    if args.inspection_report:
        command.append("--inspection-report")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
