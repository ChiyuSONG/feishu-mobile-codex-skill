from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import remote_gateway as gateway


class InspectionModeTests(unittest.TestCase):
    def test_new_registrations_and_reinitialization_preserve_modes(self):
        with tempfile.TemporaryDirectory() as raw:
            config = {"projects": {}}
            args = SimpleNamespace(
                project_key="first", working_directory=raw, workspace_mode="project",
                language="zh-CN", disable_hourly_catch_up=False, chat_name="test",
                chat_id="", focus="", bootstrap_source_thread_id="", permission_mode="full-access",
            )
            with (
                patch.object(gateway, "CONFIG_PATH", Path(raw) / "config.json"),
                patch.object(gateway, "load_config", return_value=config),
                patch.object(gateway, "load_json", return_value={}),
            ):
                first = gateway.init_project(args)["project"]
                self.assertEqual(first["inspection_report_mode"], "full")
                first["custom_setting"] = "keep"
                original = copy.deepcopy(first)
                args.project_key = "second"
                second = gateway.init_project(args)["project"]
                self.assertEqual(second["inspection_report_mode"], "status")
                self.assertEqual(config["projects"]["first"], original)
                second["inspection_report_mode"] = "full"  # Explicit customization.
                self.assertEqual(gateway.init_project(args)["project"]["inspection_report_mode"], "full")
                args.project_key = "first"
                self.assertEqual(gateway.init_project(args)["project"]["custom_setting"], "keep")
                args.project_key = "third"
                self.assertEqual(gateway.init_project(args)["project"]["inspection_report_mode"], "status")
                saved = json.loads((Path(raw) / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["projects"]["third"]["inspection_report_mode"], "status")

    def test_legacy_registration_keeps_full_and_new_registration_is_status(self):
        with tempfile.TemporaryDirectory() as raw:
            config = {"projects": {"legacy": {"working_directory": raw}}}
            args = SimpleNamespace(
                project_key="new", working_directory=raw, workspace_mode="project",
                language="en", disable_hourly_catch_up=False, chat_name="test",
                chat_id="", focus="", bootstrap_source_thread_id="", permission_mode="full-access",
            )
            with (
                patch.object(gateway, "CONFIG_PATH", Path(raw) / "config.json"),
                patch.object(gateway, "load_config", return_value=config),
                patch.object(gateway, "load_json", return_value={}),
            ):
                self.assertEqual(gateway.init_project(args)["project"]["inspection_report_mode"], "status")
                self.assertNotIn("inspection_report_mode", config["projects"]["legacy"])
                args.project_key = "legacy"
                self.assertEqual(gateway.init_project(args)["project"]["inspection_report_mode"], "full")

    def test_status_only_is_one_line_and_never_reads_usage_idle_or_active(self):
        for language in ("zh-CN", "en"):
            for progress in (None, {"elapsed": "12m", "detail": "Testing changes"}):
                with (
                    self.subTest(language=language, progress=progress),
                    patch.object(gateway, "inspection_counts", return_value={"pending": 2, "processing": int(bool(progress))}),
                    patch.object(gateway, "active_task_progress_snapshot", return_value=progress),
                    patch.object(gateway, "codex_rate_limits") as usage,
                ):
                    text = gateway.routine_inspection_text("demo", language, "status")
                    self.assertEqual(len(text.splitlines()), 1)
                    self.assertIn("2", text)
                    if progress:
                        self.assertIn("12m", text)
                        self.assertIn("Testing changes", text)
                    usage.assert_not_called()

    def test_status_mode_first_inspection_sends_only_routine_and_preserves_dedup(self):
        config = {"expected_tenant_key": "test", "projects": {
            "demo": {"chat_id": "test-chat", "inspection_report_mode": "status"},
        }}
        with (
            patch.object(gateway, "load_app_credentials", return_value=("test", "test")),
            patch.object(gateway, "FeishuClient") as client_type,
            patch.object(gateway, "inspection_counts", return_value={}),
            patch.object(gateway, "active_task_progress_snapshot", return_value=None),
            patch.object(gateway, "codex_rate_limits") as usage,
            patch.object(gateway, "project_runtime") as runtime,
        ):
            client = client_type.return_value
            client.tenant_info.return_value = {"tenant_key": "test"}
            client.send_post.return_value = {"data": {"message_id": "test-message"}}
            for _ in range(2):
                result = gateway.send_default_inspection_message(config, "demo")
                self.assertEqual(len(result["text"].splitlines()), 1)
            sent = client.send_post.call_args_list
            self.assertEqual(sent[0].args[2], sent[1].args[2])
            usage.assert_not_called()
            runtime.assert_not_called()  # No first-inspection marker or explanation.

    def test_status_welcome_does_not_advertise_full_reports(self):
        for language in ("zh-CN", "en"):
            text = gateway.welcome_message({
                "working_directory": ".", "language": language, "inspection_report_mode": "status",
            })
            self.assertNotIn("Token", text)
            self.assertIn("only this project's status" if language == "en" else "只报告本项目状态", text)


if __name__ == "__main__":
    unittest.main()
