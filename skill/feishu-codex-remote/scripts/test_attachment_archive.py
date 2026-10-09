"""Attachment integrity and immutable publication using local files and HTTP."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.request

import attachment_archive as archive
import gateway_common as common


@contextmanager
def local_download(body, *, declared_length=None, content_type="application/octet-stream"):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if declared_length is None else declared_length))
            self.send_header("Content-Disposition", 'attachment; filename="synthetic.bin"')
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    # Bypass machine proxy configuration for the local synthetic HTTP fixture.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with patch.object(common, "API_BASE", f"http://127.0.0.1:{server.server_port}"), \
                patch.object(common.urllib.request, "urlopen", side_effect=opener.open):
            yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class AttachmentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "message"
        self.identity = archive.resource_identity("synthetic-message", "file", "synthetic-key")
        self.client = common.FeishuClient("synthetic-app", "synthetic-not-a-credential")
        self.client.tenant_token = MagicMock(return_value="synthetic-not-a-token")

    def receipt(self, identity=None):
        return archive.receipt_path(self.directory, identity or self.identity)

    def download(self, key="synthetic-key"):
        return self.client.download_resource("synthetic-message", "file", key, self.directory)

    def assert_unpublished(self):
        self.assertEqual([], list(self.directory.iterdir()) if self.directory.exists() else [])

    def test_real_http_download_has_verified_receipt_and_reuses_cache_offline(self):
        body = b"synthetic complete attachment\x00\xff"
        with local_download(body) as requests:
            target = self.download()
        self.assertEqual(1, len(requests))
        self.assertEqual(body, target.read_bytes())
        receipt = json.loads(self.receipt().read_text(encoding="utf-8"))
        self.assertEqual(self.identity, receipt["source"])
        self.assertEqual(len(body), receipt["bytes"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), receipt["sha256"])
        self.client.tenant_token.reset_mock()
        with patch.object(self.client, "_open", side_effect=AssertionError("offline cache must not use network")):
            self.assertEqual(target, self.download())
        self.client.tenant_token.assert_not_called()

    def test_real_http_short_read_publishes_nothing(self):
        with local_download(b"short", declared_length=50):
            with self.assertRaises(Exception):
                self.download()
        self.assert_unpublished()

    def test_response_returning_short_body_is_explicitly_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"short"
        response.headers = {"Content-Length": "50", "Content-Type": "application/octet-stream"}
        with patch.object(self.client, "_open", return_value=response), \
                self.assertRaisesRegex(common.GatewayError, "incomplete"):
            self.download()
        self.assert_unpublished()

    def test_response_read_failure_publishes_nothing(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = OSError("synthetic interrupted response")
        with patch.object(self.client, "_open", return_value=response), self.assertRaises(OSError):
            self.download()
        self.assert_unpublished()

    def test_file_fsync_failure_cleans_temporary_and_does_not_publish(self):
        with patch.object(archive.os, "fsync", side_effect=OSError("synthetic disk failure")), \
                self.assertRaises(OSError):
            archive.archive_resource(self.directory, self.identity, b"body", "application/octet-stream", ".bin")
        self.assert_unpublished()

    def test_concurrent_same_content_publication_is_idempotent(self):
        body = b"identical concurrent content"
        barrier = threading.Barrier(4)

        def publish(_):
            barrier.wait(timeout=5)
            return archive.archive_resource(self.directory, self.identity, body, "application/octet-stream", ".bin")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(publish, range(4)))
        self.assertEqual(1, len(set(results)))
        self.assertEqual(body, results[0].read_bytes())
        self.assertEqual(results[0], archive.cached_resource(self.directory, self.identity))
        self.assertEqual([], list(self.directory.glob("*.tmp")))

    def test_same_target_different_content_refuses_overwrite(self):
        target = self.directory / "immutable.bin"
        first, second = b"first body", b"second body"
        archive.publish_attachment(target, first, hashlib.sha256(first).hexdigest())
        with self.assertRaisesRegex(common.GatewayError, "checksum conflict"):
            archive.publish_attachment(target, second, hashlib.sha256(second).hexdigest())
        self.assertEqual(first, target.read_bytes())
        self.assertEqual([], list(self.directory.glob("*.tmp")))

    def test_concurrent_different_content_same_target_has_one_immutable_winner(self):
        target = self.directory / "immutable.bin"
        barrier = threading.Barrier(2)

        def attempt(body):
            barrier.wait(timeout=5)
            try:
                archive.publish_attachment(target, body, hashlib.sha256(body).hexdigest())
                return body
            except common.GatewayError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (b"first candidate", b"second candidate")))
        winners = [result for result in results if isinstance(result, bytes)]
        self.assertEqual(1, len(winners))
        self.assertEqual(1, len([result for result in results if isinstance(result, common.GatewayError)]))
        self.assertEqual(winners[0], target.read_bytes())
        self.assertEqual([], list(self.directory.glob("*.tmp")))

    def test_concurrent_changed_identity_receipt_has_one_winner_and_one_refusal(self):
        # Both complete files can legitimately be ready before either download
        # claims the same resource receipt. The receipt must perform atomic CAS.
        barrier = threading.Barrier(2)
        original = archive.publish_attachment

        def publish(target, body, digest):
            original(target, body, digest)
            if Path(target).suffix == ".bin":
                barrier.wait(timeout=5)

        def attempt(body):
            try:
                return archive.archive_resource(self.directory, self.identity, body, "application/octet-stream", ".bin")
            except common.GatewayError as exc:
                return exc

        with patch.object(archive, "publish_attachment", side_effect=publish), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (b"first body", b"different body")))
        winners = [result for result in results if isinstance(result, Path)]
        failures = [result for result in results if isinstance(result, common.GatewayError)]
        self.assertEqual(1, len(winners), "a resource identity must not publish two different immutable receipts")
        self.assertEqual(1, len(failures))
        self.assertEqual(winners[0], archive.cached_resource(self.directory, self.identity))

    def test_same_bytes_different_mime_and_suffix_reuse_first_committed_receipt(self):
        body = b"identical body with inconsistent response headers"
        barrier = threading.Barrier(2)
        original = archive.publish_attachment

        def publish(target, content, digest):
            original(target, content, digest)
            barrier.wait(timeout=5)

        def attempt(metadata):
            mime, suffix = metadata
            return archive.archive_resource(self.directory, self.identity, body, mime, suffix)

        with patch.object(archive, "publish_attachment", side_effect=publish), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (("image/png", ".png"), ("application/octet-stream", ".bin"))))
        self.assertEqual(results[0], results[1])
        committed = self.receipt().read_bytes()
        receipt = json.loads(committed)
        self.assertEqual(results[0].name, receipt["filename"])
        self.assertIn((receipt["content_type"], results[0].suffix),
                      (("image/png", ".png"), ("application/octet-stream", ".bin")))
        # A later equivalent response cannot rewrite the first response's header
        # choice, or silently change the local path already used by a model run.
        again = archive.archive_resource(self.directory, self.identity, body, "text/plain", ".txt")
        self.assertEqual(results[0], again)
        self.assertEqual(committed, self.receipt().read_bytes())

    def test_wrong_source_receipt_is_rejected_before_network(self):
        self.directory.mkdir()
        self.receipt().write_text(json.dumps({"schema_version": 1, "source": {"wrong": "source"}}), encoding="utf-8")
        with patch.object(self.client, "_open", side_effect=AssertionError("must not redownload")), \
                self.assertRaisesRegex(common.GatewayError, "identity"):
            self.download()

    def test_receipt_path_traversal_is_rejected(self):
        self.directory.mkdir()
        for name in ("../outside.bin", "nested/file.bin", str(self.root / "outside.bin")):
            with self.subTest(name=name):
                self.receipt().write_text(json.dumps({
                    "schema_version": 1, "source": self.identity, "filename": name,
                }), encoding="utf-8")
                with self.assertRaises(common.GatewayError):
                    archive.cached_resource(self.directory, self.identity)

    def test_corrupted_verified_file_never_reaches_caller_or_silent_overwrite(self):
        target = archive.archive_resource(self.directory, self.identity, b"original", "application/octet-stream", ".bin")
        original_receipt = self.receipt().read_bytes()
        target.write_bytes(b"corrupt!")
        with patch.object(self.client, "_open", side_effect=AssertionError("must not silently redownload")), \
                self.assertRaisesRegex(common.GatewayError, "checksum mismatch"):
            self.download()
        self.assertEqual(b"corrupt!", target.read_bytes())
        self.assertEqual(original_receipt, self.receipt().read_bytes())

    def test_legacy_file_is_retained_without_trusting_its_bytes(self):
        self.directory.mkdir()
        legacy = self.directory / "synthetic-key.bin"
        legacy.write_bytes(b"unverified legacy evidence")
        with local_download(b"current validated body"):
            target = self.download()
        self.assertNotEqual(legacy, target)
        self.assertEqual(b"unverified legacy evidence", legacy.read_bytes())
        self.assertEqual(b"current validated body", target.read_bytes())

    def test_long_resource_keys_with_same_prefix_do_not_share_identity(self):
        prefix = "same-prefix-" + "x" * 200
        left = archive.resource_identity("synthetic-message", "file", prefix + "-a")
        right = archive.resource_identity("synthetic-message", "file", prefix + "-b")
        first = archive.archive_resource(self.directory, left, b"left body", "application/octet-stream", ".bin")
        second = archive.archive_resource(self.directory, right, b"right body", "application/octet-stream", ".bin")
        self.assertNotEqual(self.receipt(left), self.receipt(right))
        self.assertNotEqual(first, second)
        self.assertEqual(b"left body", archive.cached_resource(self.directory, left).read_bytes())
        self.assertEqual(b"right body", archive.cached_resource(self.directory, right).read_bytes())

    def test_published_target_must_not_resolve_outside_message_directory(self):
        body = b"outside evidence"
        outside = self.root / "outside.bin"
        outside.write_bytes(body)
        self.directory.mkdir()
        target = self.directory / (hashlib.sha256(body).hexdigest() + ".bin")
        try:
            target.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symbolic link unavailable on this host: {type(exc).__name__}")
        with self.assertRaises(common.GatewayError):
            archive.archive_resource(self.directory, self.identity, body, "application/octet-stream", ".bin")
        self.assertFalse(self.receipt().exists())
        self.assertEqual(body, outside.read_bytes())

    def test_symlink_detection_rejects_publication_without_host_link_privilege(self):
        body = b"synthetic symlink target"
        target = self.directory / (hashlib.sha256(body).hexdigest() + ".bin")
        original = Path.is_symlink

        def is_symlink(path):
            return True if path == target else original(path)

        with patch.object(Path, "is_symlink", new=is_symlink), self.assertRaises(common.GatewayError):
            archive.archive_resource(self.directory, self.identity, body, "application/octet-stream", ".bin")
        self.assert_unpublished()

    def test_resolved_target_escape_rejected_without_host_link_privilege(self):
        body = b"synthetic escaped target"
        target = self.directory / (hashlib.sha256(body).hexdigest() + ".bin")
        outside = self.root / "outside.bin"
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            return outside if path == target else original(path, *args, **kwargs)

        with patch.object(Path, "resolve", new=resolve), self.assertRaises(common.GatewayError):
            archive.archive_resource(self.directory, self.identity, body, "application/octet-stream", ".bin")
        self.assert_unpublished()


if __name__ == "__main__":
    unittest.main()
