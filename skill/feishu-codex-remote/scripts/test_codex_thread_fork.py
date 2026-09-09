import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_thread_fork as fork


# A synthetic stdio service; it never starts Codex or reads any real history.
SERVICE = r'''
import json
import pathlib
import sys
import time

scenario = sys.argv[1]
capture = pathlib.Path(sys.argv[2])
for line in sys.stdin:
    request = json.loads(line)
    with capture.open("a", encoding="utf-8") as output:
        output.write(json.dumps(request) + "\n")
    method = request["method"]
    if method == "initialize":
        if scenario == "init_error":
            print(json.dumps({"id": 0, "error": {"code": -32600, "message": "secret-token"}}), flush=True)
            continue
        print(json.dumps({"id": 0, "result": {}}), flush=True)
    elif method == "thread/archive":
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif method in {"thread/fork", "thread/start"}:
        if scenario == "timeout":
            time.sleep(30)
        elif scenario == "eof":
            sys.exit(0)
        elif scenario == "rpc_error":
            print(json.dumps({"id": 1, "error": {"code": -32001, "message": "secret-token", "data": "private-text"}}), flush=True)
        elif scenario == "invalid_json":
            print("private invalid protocol data", flush=True)
        else:
            parent = request["params"].get("threadId")
            thread = {"id": "child-id" if parent else "main-thread", "sessionId": parent or "main-thread", "forkedFromId": parent, "ephemeral": False}
            if scenario == "parent_id":
                thread["id"] = parent
            elif scenario == "missing_id":
                del thread["id"]
            elif scenario == "wrong_parent":
                thread["forkedFromId"] = "other-parent"
            elif scenario == "ephemeral":
                thread["ephemeral"] = True
            print(json.dumps({"method": "thread/started", "params": {"thread": thread}}), flush=True)
            print(json.dumps({"id": 999, "result": {}}), flush=True)
            print(json.dumps({"id": 1, "result": {"thread": thread}}), flush=True)
'''


class ThreadForkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.capture = Path(self.temp.name) / "requests.jsonl"
        self.processes = []
        self.launches = []

    def invoke(self, scenario="success", timeout=5, *, operation="fork", **settings):
        original_popen = subprocess.Popen

        def mock_service(command, **kwargs):
            if command[0] == "mock-codex.exe" and command[-1] == "app-server":
                self.launches.append((command, kwargs.copy()))
                command = [sys.executable, "-u", "-c", SERVICE, scenario, str(self.capture)]
                process = original_popen(command, **kwargs)
                self.processes.append(process)
                return process
            return original_popen(command, **kwargs)

        with patch.object(fork.subprocess, "Popen", side_effect=mock_service):
            if operation == "archive":
                return fork.archive_thread("child-id", "mock-codex.exe", self.temp.name, timeout)
            if operation == "start":
                return fork.start_thread("mock-codex.exe", self.temp.name, timeout, **settings)
            return fork.fork_thread("parent-id", "mock-codex.exe", self.temp.name, timeout)

    def requests(self):
        return [json.loads(line) for line in self.capture.read_text(encoding="utf-8").splitlines()]

    def assert_cleaned(self):
        self.assertEqual(len(self.processes), 1)
        process = self.processes[0]
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_persistent_fork_copies_parent_and_returns_child_without_a_turn(self):
        self.assertEqual(self.invoke(), "child-id")
        requests = self.requests()
        self.assertEqual([item["method"] for item in requests], ["initialize", "initialized", "thread/fork"])
        self.assertEqual(requests[-1]["params"], {"threadId": "parent-id", "excludeTurns": True})
        self.assertNotIn("id", requests[1])
        self.assert_cleaned()

    def test_initialization_error_prevents_fork(self):
        with self.assertRaises(fork.ThreadForkRPCError) as caught:
            self.invoke("init_error")
        self.assertEqual(caught.exception.method, "initialize")
        self.assertFalse(caught.exception.mutation_submitted)
        self.assertEqual([item["method"] for item in self.requests()], ["initialize"])
        self.assert_cleaned()

    def test_archive_uses_metadata_rpc_without_a_model_turn(self):
        self.assertEqual("child-id", self.invoke(operation="archive"))
        self.assertEqual(["initialize", "initialized", "thread/archive"],
                         [row["method"] for row in self.requests()])
        self.assertEqual({"threadId": "child-id"}, self.requests()[-1]["params"])
        self.assert_cleaned()

    def test_archive_rejects_empty_id_without_spawning(self):
        with patch.object(fork.subprocess, "Popen") as spawn, self.assertRaises(ValueError):
            fork.archive_thread("", "mock-codex.exe", self.temp.name)
        spawn.assert_not_called()

    def test_start_creates_persistent_root_without_starting_a_turn(self):
        self.assertEqual(self.invoke(operation="start"), "main-thread")
        requests = self.requests()
        self.assertEqual([item["method"] for item in requests], ["initialize", "initialized", "thread/start"])
        self.assertEqual(requests[-1]["params"], {"cwd": self.temp.name})
        self.assert_cleaned()

    def test_start_reuses_explicit_main_model_and_reasoning_settings(self):
        self.invoke(operation="start", model="main-model", reasoning_effort="high")
        self.assertEqual(self.requests()[-1]["params"], {"cwd": self.temp.name, "model": "main-model"})
        self.assertEqual(self.launches[0][0], [
            "mock-codex.exe", "-c", 'model_reasoning_effort="high"', "app-server",
        ])
        self.assert_cleaned()

    def test_start_failure_is_reported_without_automatic_second_start(self):
        with self.assertRaises(fork.ThreadForkRPCError) as caught:
            self.invoke("rpc_error", operation="start")
        self.assertEqual(caught.exception.method, "thread/start")
        self.assertTrue(caught.exception.mutation_submitted)
        self.assertEqual(sum(item["method"] == "thread/start" for item in self.requests()), 1)
        self.assert_cleaned()

    def test_rpc_error_has_code_but_no_private_response(self):
        with self.assertRaises(fork.ThreadForkRPCError) as caught:
            self.invoke("rpc_error")
        self.assertEqual(caught.exception.code, -32001)
        self.assertEqual(caught.exception.method, "thread/fork")
        self.assertTrue(caught.exception.mutation_submitted)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("private-text", str(caught.exception))
        self.assert_cleaned()

    def test_eof_is_distinct_from_timeout_and_rpc_error(self):
        with self.assertRaises(fork.ThreadForkEOFError) as caught:
            self.invoke("eof")
        self.assertTrue(caught.exception.mutation_submitted)
        self.assert_cleaned()

    def test_timeout_stops_service_and_does_not_retry(self):
        started = time.monotonic()
        with self.assertRaises(fork.ThreadForkTimeoutError) as caught:
            self.invoke("timeout", timeout=0.5)
        self.assertTrue(caught.exception.mutation_submitted)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(sum(item["method"] == "thread/fork" for item in self.requests()), 1)
        self.assert_cleaned()

    def test_invalid_child_or_protocol_is_rejected_without_exposing_raw_data(self):
        for scenario in ("parent_id", "missing_id", "wrong_parent", "ephemeral", "invalid_json"):
            with self.subTest(scenario=scenario):
                self.processes.clear()
                with self.assertRaises(fork.ThreadForkProtocolError) as caught:
                    self.invoke(scenario)
                self.assertNotIn("private invalid protocol data", str(caught.exception))
                self.assertTrue(caught.exception.mutation_submitted)
                self.assert_cleaned()

    def test_spawn_is_hidden_on_windows_and_stderr_is_not_captured(self):
        self.invoke()
        options = self.launches[0][1]
        self.assertFalse(options["shell"])
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(options["startupinfo"].wShowWindow, subprocess.SW_HIDE)
        self.assert_cleaned()

    def test_invalid_timeout_never_launches_a_process(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), patch.object(fork.subprocess, "Popen") as spawn:
                with self.assertRaises(ValueError):
                    fork.fork_thread("parent-id", "mock-codex.exe", self.temp.name, timeout)
                spawn.assert_not_called()

    def test_spawn_failure_is_sanitized(self):
        with patch.object(fork.subprocess, "Popen", side_effect=OSError("secret-token")):
            with self.assertRaises(fork.ThreadForkError) as caught:
                fork.fork_thread("parent-id", "mock-codex.exe", self.temp.name)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertFalse(caught.exception.mutation_submitted)


if __name__ == "__main__":
    unittest.main()
