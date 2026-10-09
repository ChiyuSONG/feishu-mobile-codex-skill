"""Recovery commit tests: failed requests remain desired, not yet durable."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import gateway_primitives
from gateway_recovery import CatchupRecovery


class RecoveryCommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "recovery.json"
        self.initial = {"revision": 7, "pending": {"existing": 6, "target": 7},
                        "opaque": {"preserved": ["fixture"]}}
        gateway_primitives.atomic_write_json(self.path, self.initial)
        self.recovery = CatchupRecovery(self.path)
        self.initial_bytes = self.path.read_bytes()

    def assert_initial_file_intact(self):
        self.assertEqual(self.path.read_bytes(), self.initial_bytes)
        self.assertEqual(CatchupRecovery(self.path).state, self.initial)
        self.assertEqual(list(self.path.parent.glob(".recovery.json.*")), [])

    def publication_failure(self):
        # Write and close the real temporary file; fail only its final publish.
        def reject(source, destination):
            self.assertEqual(Path(destination), self.path)
            self.assertTrue(Path(source).is_file())
            json.loads(Path(source).read_text(encoding="utf-8"))
            raise OSError("synthetic atomic publication failure")
        return patch.object(gateway_primitives.os, "replace", side_effect=reject)

    def test_request_publish_failure_retains_desired_intent_but_not_durable_success(self):
        with self.publication_failure():
            with self.assertRaisesRegex(OSError, "synthetic atomic publication failure"):
                self.recovery.request(["new", "target"])
        desired = {**self.initial, "revision": 8,
                   "pending": {"existing": 6, "target": 8, "new": 8}}
        self.assertEqual(self.recovery.state, desired)
        self.assert_initial_file_intact()
        # Existing reconciliation can complete this in-memory request once
        # publication works; its commit must persist other desired work too.
        self.recovery.complete("new", 8)
        expected = {**desired, "pending": {"existing": 6, "target": 8}}
        self.assertEqual(CatchupRecovery(self.path).state, expected)
        self.assertEqual(self.recovery.state, expected)
        self.recovery.complete("target", 8)
        self.assertEqual(CatchupRecovery(self.path).pending(), {"existing": 6})

    def test_complete_publish_failure_retains_intent_then_retries(self):
        previous_state = self.recovery.state
        with self.publication_failure():
            with self.assertRaisesRegex(OSError, "synthetic atomic publication failure"):
                self.recovery.complete("target", 7)
        self.assertEqual(self.recovery.state, self.initial)
        self.assertEqual(self.recovery.pending(), self.initial["pending"])
        self.assert_initial_file_intact()
        self.assertIs(self.recovery.state, previous_state)
        self.recovery.complete("target", 7)
        expected = {**self.initial, "pending": {"existing": 6}}
        self.assertEqual(self.recovery.state, expected)
        self.assertEqual(CatchupRecovery(self.path).state, expected)
        self.assertEqual(previous_state, self.initial)

    def test_failed_scan_request_failure_keeps_desired_work_for_existing_loop(self):
        with self.publication_failure():
            with self.assertRaisesRegex(OSError, "synthetic atomic publication failure"):
                self.recovery.failed("new")
        self.assertEqual(self.recovery.pending(), {"existing": 6, "target": 7, "new": 8})
        self.assertEqual(self.recovery.state["revision"], 8)
        self.assert_initial_file_intact()
        self.recovery.complete("new", 8)
        expected = {**self.initial, "revision": 8}
        self.assertEqual(self.recovery.state, expected)
        self.assertEqual(CatchupRecovery(self.path).state, expected)

    def test_first_request_failure_has_desired_work_but_no_durable_file(self):
        self.path.unlink()
        self.recovery = CatchupRecovery(self.path)
        with self.publication_failure():
            with self.assertRaisesRegex(OSError, "synthetic atomic publication failure"):
                self.recovery.request(["first"])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.recovery.state, {"revision": 1, "pending": {"first": 1}})
        self.assertEqual(CatchupRecovery(self.path).state, {"revision": 0, "pending": {}})
        self.assertEqual(list(self.path.parent.glob(".recovery.json.*")), [])
        self.recovery.complete("first", 1)
        self.assertEqual(CatchupRecovery(self.path).state, {"revision": 1, "pending": {}})

    def test_new_desired_revision_survives_completion_of_older_inflight_scan(self):
        scan_started = threading.Event()
        finish_scan = threading.Event()
        errors = []

        def scan():
            try:
                with self.recovery.group_lock("target"):
                    scanned_revision = self.recovery.pending()["target"]
                    scan_started.set()
                    if not finish_scan.wait(5):
                        raise AssertionError("fixture scan did not finish")
                    self.recovery.complete("target", scanned_revision)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=scan)
        thread.start()
        try:
            self.assertTrue(scan_started.wait(5))
            with self.publication_failure():
                with self.assertRaisesRegex(OSError, "synthetic atomic publication failure"):
                    self.recovery.request(["target"])
        finally:
            finish_scan.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.recovery.pending(), {"existing": 6, "target": 8})
        self.assertEqual(self.recovery.state["revision"], 8)
        self.assert_initial_file_intact()
        self.recovery.complete("target", 8)
        self.assertEqual(CatchupRecovery(self.path).pending(), {"existing": 6})

    def test_completion_changes_memory_only_after_atomic_publication_returns(self):
        replace = gateway_primitives.os.replace
        previous_state = copy.deepcopy(self.recovery.state)
        observed = []

        def publish(source, destination):
            self.assertEqual(self.recovery.state, previous_state)
            self.assertEqual(self.path.read_bytes(), self.initial_bytes)
            self.assertNotEqual(json.loads(Path(source).read_text(encoding="utf-8")),
                                previous_state)
            replace(source, destination)
            self.assertEqual(self.recovery.state, previous_state)
            observed.append(True)

        with patch.object(gateway_primitives.os, "replace", side_effect=publish):
            self.recovery.complete("target", 7)
        self.assertEqual(observed, [True])
        self.assertEqual(CatchupRecovery(self.path).state, self.recovery.state)
        self.assertEqual(self.recovery.state["opaque"], self.initial["opaque"])

    def test_request_retry_after_failed_publication_keeps_monotonic_revision(self):
        with self.publication_failure():
            with self.assertRaisesRegex(OSError, "synthetic atomic publication failure"):
                self.recovery.request(["new"])
        self.assertEqual(self.recovery.state["revision"], 8)
        self.assert_initial_file_intact()
        self.recovery.request(["new"])
        expected = {**self.initial, "revision": 9,
                    "pending": {**self.initial["pending"], "new": 9}}
        self.assertEqual(self.recovery.state, expected)
        self.assertEqual(CatchupRecovery(self.path).state, expected)
        self.recovery.complete("new", 8)
        self.assertEqual(self.recovery.state, expected)
        self.assertEqual(CatchupRecovery(self.path).state, expected)

    def test_request_input_failure_does_not_partially_mutate_intent(self):
        def incomplete_keys():
            yield "new"
            raise ValueError("synthetic interrupted key iteration")

        with self.assertRaisesRegex(ValueError, "synthetic interrupted key iteration"):
            self.recovery.request(incomplete_keys())
        self.assertEqual(self.recovery.state, self.initial)
        self.assert_initial_file_intact()


if __name__ == "__main__":
    unittest.main()
