from __future__ import annotations

import base64
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import feishu_publish


class FeishuPublishTests(unittest.TestCase):
    def test_effective_scopes_upgrades_legacy_configuration(self):
        scopes = feishu_publish.effective_scopes({"scopes": "drive:drive offline_access"}).split()
        self.assertEqual(scopes.count("drive:drive"), 1)
        self.assertIn("docx:document", scopes)
        self.assertIn("offline_access", scopes)

    def test_markdown_local_artifact_placeholder_has_no_local_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp = root / "temp"
            temp.mkdir()
            image = root / "preview.png"
            image.write_bytes(b"png")
            source = root / "final.md"
            source.write_text(f"![preview](<{image.as_posix()}>)", encoding="utf-8")
            prepared = feishu_publish.markdown_without_local_artifacts(source, [image], temp)
            text = prepared.read_text(encoding="utf-8")
            self.assertIn("preview.png", text)
            self.assertNotIn(image.as_posix(), text)
            self.assertNotIn("![preview]", text)

    def test_markdown_conversion_preserves_images_but_not_file_links(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp = root / "temp"
            temp.mkdir()
            image = root / "preview.png"
            report = root / "report.pdf"
            image.write_bytes(b"png")
            report.write_bytes(b"pdf")
            source = root / "final.md"
            source.write_text(
                f"![preview](<{image.as_posix()}>)\n\n[report](<{report.as_posix()}>)",
                encoding="utf-8",
            )
            prepared = feishu_publish.markdown_without_local_artifacts(
                source,
                [image, report],
                temp,
                preserve_local_images=True,
            )
            text = prepared.read_text(encoding="utf-8")
            self.assertIn(f"![preview](<{image.as_posix()}>)", text)
            self.assertNotIn(report.as_posix(), text)
            self.assertIn("report.pdf", text)

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc is required for the DOCX media smoke test")
    def test_markdown_conversion_packages_real_image_in_docx(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image = root / "preview.png"
            image.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            )
            source = root / "final.md"
            source.write_text(f"![preview](<{image}>)", encoding="utf-8")
            output = feishu_publish.convert_for_reading(source, "Image smoke", root, [image])
            with zipfile.ZipFile(output) as archive:
                self.assertTrue(any(name.startswith("word/media/") for name in archive.namelist()))
                self.assertIn(b"<w:drawing>", archive.read("word/document.xml"))

    def test_embed_document_image_uploads_updates_and_verifies(self):
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "preview.png"
            image.write_bytes(b"png")
            responses = [
                {},
                {"data": {"block": {"block_id": "block_1", "image": {"token": "file_1"}}}},
            ]
            with (
                patch.object(feishu_publish, "create_media_block", return_value="block_1"),
                patch.object(feishu_publish, "upload_document_media", return_value="file_1"),
                patch.object(feishu_publish, "request_json", side_effect=responses) as request,
            ):
                result = feishu_publish.embed_document_artifact("user-token", "doc_1", image)
            self.assertTrue(result["verified"])
            self.assertEqual(result["mode"], "embedded")
            self.assertEqual(request.call_args_list[0].kwargs["body"], {"replace_image": {"token": "file_1"}})

    def test_create_file_block_uses_nested_file_block_id(self):
        responses = [
            {"data": {"items": [{"block_id": "doc_1", "children": ["text_1"]}], "has_more": False}},
            {"data": {"children": [{"block_type": 33, "children": ["file_block_1"]}]}},
        ]
        with patch.object(feishu_publish, "request_json", side_effect=responses):
            block_id = feishu_publish.create_media_block("user-token", "doc_1", "file")
        self.assertEqual(block_id, "file_block_1")

    def test_publish_falls_back_to_private_file_when_docx_scope_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "final.md"
            source.write_text("result", encoding="utf-8")
            image = root / "preview.png"
            image.write_bytes(b"png")
            args = SimpleNamespace(
                source=[str(source)], embed=[str(image)], title="Result",
                folder_token=None, flat=False, allow_sensitive=False,
            )
            history = root / "history.jsonl"
            with (
                patch.object(feishu_publish, "access_token", return_value=("user-token", {}, {"tenant_key": "tenant"})),
                patch.object(feishu_publish, "ensure_folder", side_effect=[("root", "https://f/root"), ("target", "https://f/target")]),
                patch.object(feishu_publish, "convert_for_reading", return_value=source),
                patch.object(feishu_publish, "upload_import_source", return_value={"token": "doc_1", "url": "https://f/docx/doc_1"}),
                patch.object(feishu_publish, "embed_document_artifact", side_effect=feishu_publish.PublishError("missing docx scope")),
                patch.object(feishu_publish, "upload_regular_file", return_value={"file_token": "file_1", "url": "https://f/file/file_1"}),
                patch.object(feishu_publish, "harden_link_permissions", return_value={"link_share_entity": "closed"}),
                patch.object(feishu_publish, "list_files", return_value=[{"token": "doc_1"}, {"token": "file_1"}]),
                patch.object(feishu_publish, "HISTORY_PATH", history),
            ):
                result = feishu_publish.publish(args)
            self.assertTrue(result["content_validation"]["delivered"])
            self.assertEqual(result["content_validation"]["embedded"], 0)
            self.assertEqual(result["content_validation"]["separate_files"], 1)
            self.assertEqual(result["embedded_artifacts"][0]["url"], "https://f/file/file_1")


if __name__ == "__main__":
    unittest.main()
