"""Native artifact presentation through the real delivery journal, with fake network."""
from dataclasses import replace
from pathlib import Path
import unittest
import remote_gateway as gateway
import test_delivery_receipts as fixtures

class ArtifactPresentationTests(unittest.TestCase):
    setUp = fixtures.ReplyReceiptTests.setUp
    deliver = fixtures.ReplyReceiptTests.deliver
    restart = fixtures.ReplyReceiptTests.restart

    def configure(self, answer):
        self.answer = answer
        self.final_path.write_text(answer, encoding='utf-8')
        self.routing = replace(self.routing, extract_artifacts=gateway.extract_local_artifacts,
                               render_artifact_labels=gateway.render_local_artifact_labels)

    def test_only_recognized_native_media_links_become_labels(self):
        image = self.root / 'preview.png'
        document = self.root / 'result.pdf'
        other = self.root / 'evidence.zip'
        for path in (image, document, other):
            path.write_bytes(b'synthetic')
        answer = (f'Images: ![Preview]({image.as_posix()})\n'
                  f'[Report]({document.as_posix()})\n'
                  f'[Other file]({other.as_posix()})\n'
                  '[External](https://example.invalid/report.pdf)\n'
                  '[Unknown](missing.pdf)\nPlain explanation stays.')
        self.configure(answer)
        self.deliver()
        sent = self.client.calls[0][2]
        self.assertIn('Images: Preview', sent)
        self.assertIn('Report', sent)
        self.assertNotIn(image.as_posix(), sent)
        self.assertNotIn(document.as_posix(), sent)
        self.assertIn(f'[Other file]({other.as_posix()})', sent)
        self.assertIn('[External](https://example.invalid/report.pdf)', sent)
        self.assertIn('[Unknown](missing.pdf)', sent)
        self.assertIn('Plain explanation stays.', sent)
        self.assertEqual(answer, self.final_path.read_text(encoding='utf-8'))
        self.assertEqual(['reply_post','reply_image','reply_file'], [c[0] for c in self.client.calls])

    def test_frozen_preupgrade_send_plan_is_replayed_without_rewriting_payload_or_uuid(self):
        self.answer = 'Original unchanged reply.'
        self.final_path.write_text(self.answer, encoding='utf-8')
        self.deliver()
        before = list(self.client.calls)
        self.restart()
        self.routing = replace(self.routing, render_artifact_labels=lambda *args: 'changed renderer')
        self.deliver()
        self.assertEqual(before, self.client.calls)
