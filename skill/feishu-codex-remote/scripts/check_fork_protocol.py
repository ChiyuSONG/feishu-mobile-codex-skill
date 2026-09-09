"""Opt-in native metadata canary: synthetic data, isolated Codex home, no model."""
import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from codex_thread_fork import _thread_rpc, archive_thread


def check(codex_path):
    with tempfile.TemporaryDirectory(prefix="feishu-fork-protocol-") as temp:
        home = Path(temp) / "home"
        home.mkdir()
        parent = str(uuid.uuid4())
        source = home / "synthetic.jsonl"
        stamp = "2026-01-01T00:00:00Z"
        records = [
            {"timestamp": stamp, "type": "session_meta", "payload": {
                "id": parent, "timestamp": stamp, "cwd": temp,
                "originator": "codex_cli_rs", "cli_version": "0.153.4", "source": "cli",
                "model_provider": "openai", "history_mode": "legacy",
            }},
            {"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text",
                "text": "Synthetic protocol fixture. No model turn requested."}],
            }},
        ]
        source.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf-8")
        original = source.read_bytes()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            child = _thread_rpc(
                "thread/fork", {"threadId": parent, "path": str(source), "excludeTurns": True},
                codex_path, temp, 30, parent_thread_id=parent,
            )
            if child == parent or source.read_bytes() != original:
                raise RuntimeError("Native fork changed source identity or data")
            archive_thread(child, codex_path, temp)
        return "Native import and archive passed; synthetic context, isolated home, zero model turns."


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, help="Exact installed Codex executable to validate")
    print(check(parser.parse_args().codex))
