"""Terminal native provider classification; no network, model or user data."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from provider_failure import (
    ProviderCapacityError,
    ProviderFailureError,
    provider_failure_is_deferred,
    provider_failure_reason,
    terminal_capacity_failure,
    terminal_provider_failure,
)


class ProviderFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "events.jsonl"

    def write(self, *events):
        self.log.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    def failure(self, message, **fields):
        return {"type": "turn.failed", "error": {"message": message, **fields}}

    def test_observed_native_routing_failures_including_remote_compaction(self):
        # Terminal message shapes observed in the independent project's retained
        # October outage fixtures; no thread IDs, prompts or domain data copied.
        for message in (
            "workspace routing discovery timed out",
            "Error running remote compact task: workspace routing discovery failed",
        ):
            with self.subTest(message=message):
                self.write(self.failure(message))
                self.assertEqual("routing", terminal_provider_failure(self.log))

    def test_transport_failures_are_separate_from_routing_and_authentication(self):
        for message in (
            "connection refused", "connection reset by peer", "connection aborted",
            "failed to connect to service", "unable to connect to service",
            "network is unreachable", "name resolution failed", "dns lookup failed",
            "temporary failure in name resolution", "TLS handshake failed",
            "SSL handshake failed", "proxy error", "stream disconnected before completion",
            "HTTP status 502", "unexpected status 503", "unexpected status code 504",
            "error sending request for url (https://synthetic.invalid/)", "Codex PC offline",
            "Error running remote compact task: stream disconnected before completion",
        ):
            with self.subTest(message=message):
                self.write(self.failure(message))
                self.assertEqual("transport", terminal_provider_failure(self.log))

    def test_terminal_authentication_failure_is_not_automatically_deferred(self):
        for message in (
            "unexpected status 401 Unauthorized: synthetic credential rejected",
            "unexpected status code 401 Unauthorized",
            "401 Unauthorized", "authentication failed", "your authentication token has expired",
        ):
            with self.subTest(message=message):
                self.write(self.failure(message))
                kind = terminal_provider_failure(self.log)
                self.assertEqual("authentication", kind)
                self.assertFalse(provider_failure_is_deferred(kind))

    def test_recovered_native_errors_and_old_failed_turns_do_not_override_success(self):
        for message in ("stream disconnected", "workspace routing discovery failed", "401 unauthorized"):
            for prefix in ([{"type": "error", "message": message}], [self.failure(message)]):
                with self.subTest(message=message, prefix=prefix):
                    self.write(*prefix, {"type": "turn.completed"}, {"type": "error", "message": message})
                    self.assertIsNone(terminal_provider_failure(self.log))

    def test_latest_terminal_failure_alone_owns_outcome(self):
        self.write(self.failure("stream disconnected"), self.failure("invalid schema"))
        self.assertIsNone(terminal_provider_failure(self.log))
        self.write({"type": "turn.completed"}, self.failure("workspace routing discovery timed out"))
        self.assertEqual("routing", terminal_provider_failure(self.log))

    def test_body_tool_output_and_intermediate_warnings_cannot_classify_failure(self):
        for message in ("stream disconnected", "workspace routing discovery timed out", "401 unauthorized",
                        "Selected model is at capacity", "usage limit"):
            for events in (
                [{"type": "item.completed", "item": {"type": "agent_message", "text": message}}],
                [{"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": message}}],
                [{"type": "error", "message": message}],
                [self.failure("review rejected: user mentions " + message)],
            ):
                with self.subTest(events=events):
                    self.write(*events)
                    self.assertIsNone(terminal_provider_failure(self.log))

    def test_business_timeouts_and_unknown_auth_status_are_not_guessed(self):
        for message in ("local operation timed out", "review failed", "unexpected status 403 Forbidden",
                        "invalid schema", "unexpected status 4010", "network might be unstable"):
            with self.subTest(message=message):
                self.write(self.failure(message))
                self.assertIsNone(terminal_provider_failure(self.log))

    def test_structured_existing_provider_category_has_priority(self):
        for code, expected in (("usage_limit_reached", "rate_limit"), ("rate_limit_exceeded", "rate_limit"),
                               ("rate_limit", "rate_limit"), ("model_at_capacity", "at_capacity")):
            with self.subTest(code=code):
                self.write(self.failure("stream disconnected", code=code))
                self.assertEqual(expected, terminal_provider_failure(self.log))

    def test_capacity_and_native_rate_limit_signatures_remain_compatible(self):
        self.write(self.failure("Selected model is at capacity. Please try again later."))
        self.assertTrue(terminal_capacity_failure(self.log))
        for message in ("usage limit reached", "rate limit reached", "rate_limit_exceeded", "quota exceeded",
                        "You've hit your usage limit. Try again later.", "unexpected status code 429 Too Many Requests"):
            with self.subTest(message=message):
                self.write(self.failure(message))
                self.assertEqual("rate_limit", terminal_provider_failure(self.log))

    def test_legacy_terminal_message_shapes_and_invalid_json_are_safe(self):
        self.log.write_text("invalid\nnull\n[]\n" + json.dumps({
            "type": "turn.failed", "error": "stream disconnected"}) + "\n", encoding="utf-8")
        self.assertEqual("transport", terminal_provider_failure(self.log))
        self.write({"type": "turn.failed", "message": "workspace routing discovery failed"})
        self.assertEqual("routing", terminal_provider_failure(self.log))

    def test_classifier_never_changes_event_log_or_partial_results(self):
        partial = self.root / "final.md"
        partial.write_text("Saved partial output; must remain available.", encoding="utf-8")
        self.write({"type": "item.completed", "item": {"text": "Saved partial output"}},
                   self.failure("stream disconnected"))
        before = {path: hashlib.sha256(path.read_bytes()).digest() for path in (self.log, partial)}
        self.assertEqual("transport", terminal_provider_failure(self.log))
        for path, digest in before.items():
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).digest())

    def test_safe_public_reason_and_exception_do_not_include_provider_payload(self):
        private = "unexpected status 401 Unauthorized: rejected synthetic-secret, url https://synthetic.invalid/token"
        self.write(self.failure(private))
        kind = terminal_provider_failure(self.log)
        error = ProviderFailureError(self.log, kind)
        self.assertEqual(self.log, error.event_log)
        self.assertEqual("authentication", error.kind)
        for text in (str(error), provider_failure_reason(kind), provider_failure_reason(kind, english=True)):
            self.assertNotIn("synthetic-secret", text)
            self.assertNotIn("https://", text)
            self.assertNotIn(str(self.log), text)
        self.assertIn("可能", provider_failure_reason(kind))

    def test_only_existing_wait_kinds_and_network_failures_are_deferred(self):
        for kind in ("at_capacity", "rate_limit", "transport", "routing"):
            with self.subTest(kind=kind):
                self.assertTrue(provider_failure_is_deferred(kind))
        for kind in (None, "", "authentication", "execution_failed", "unknown"):
            with self.subTest(kind=kind):
                self.assertFalse(provider_failure_is_deferred(kind))
        self.assertIsInstance(ProviderCapacityError(self.log), ProviderFailureError)


if __name__ == "__main__":
    unittest.main()
