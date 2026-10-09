"""Real hidden local process replay through run_codex; no model or cloud calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import remote_gateway as gateway
from provider_failure import ProviderFailureError


class ProviderTransportProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = self.root / "native_fixture.py"
        fixture.write_text(
            "import json, pathlib, sys\n"
            "sys.stdin.buffer.read()\n"
            "pathlib.Path(sys.argv[1]).write_text('saved partial output', encoding='utf-8')\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'synthetic-main'}))\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'saved partial output'}}))\n"
            "print(json.dumps({'type': 'error', 'message': sys.argv[2]}))\n"
            "print(json.dumps({'type': 'turn.failed', 'error': {'message': sys.argv[2]}}))\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        self.fixture = fixture
        runtime = patch.object(gateway, "REMOTE_STATE", self.root / "runtime")
        runtime.start()
        self.addCleanup(runtime.stop)
        self.store = gateway.ProjectStore("synthetic-provider")
        self.project = {"working_directory": str(self.root), "chat_id": "synthetic-chat"}

    def test_failed_native_process_keeps_partial_files_and_original_thread(self):
        for index, (message, kind) in enumerate((
            ("stream disconnected before completion", "transport"),
            ("Error running remote compact task: workspace routing discovery failed", "routing"),
            ("unexpected status 401 Unauthorized: synthetic secret", "authentication"),
        )):
            with self.subTest(kind=kind):
                mid = "synthetic-" + str(index)
                self.store.state["messages"][mid] = {
                    "message_id": mid, "chat_id": "synthetic-chat", "message_type": "text",
                    "content": json.dumps({"text": "synthetic test"}), "create_time": index,
                    "status": "processing", "attempts": 1,
                }
                self.store.save()
                item = self.store.state["messages"][mid]
                commands = []

                def command(_project, final_path, _attachments, _thread_id):
                    commands.append(final_path)
                    return [sys.executable, str(self.fixture), str(final_path), message]

                actual_popen = subprocess.Popen
                with patch.object(gateway, "build_codex_command", side_effect=command), \
                        patch.object(subprocess, "Popen", wraps=actual_popen) as launched, \
                        self.assertRaises(ProviderFailureError) as raised:
                    gateway.run_codex("synthetic-provider", self.project, self.store, item, [])
                self.assertEqual(kind, raised.exception.kind)
                self.assertEqual(1, len(commands))
                self.assertEqual("saved partial output", commands[0].read_text(encoding="utf-8"))
                self.assertEqual("synthetic-main", self.store.thread_id())
                events = raised.exception.event_log
                self.assertEqual(str(events), self.store.state["messages"][mid]["run_event_log"])
                self.assertTrue(events.is_file())
                self.assertEqual("turn.failed", json.loads(events.read_text(encoding="utf-8").splitlines()[-1])["type"])
                self.assertEqual(1, launched.call_count)
                if os.name == "nt":
                    self.assertTrue(launched.call_args.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
