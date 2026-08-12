from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import remote_gateway
from gateway_common import FeishuClient, GatewayError, message_text, resource_keys


def write_rollout(path: Path, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, message in enumerate(messages):
        lines.append(
            json.dumps(
                {
                    "timestamp": f"2026-08-11T00:00:{index:02d}Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": message["role"],
                        "phase": message.get("phase"),
                        "content": [
                            {
                                "type": "input_text" if message["role"] != "assistant" else "output_text",
                                "text": message["text"],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_thread_db(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT, "
            "source TEXT, thread_source TEXT, updated_at INTEGER)"
        )
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


class MessageParsingTests(unittest.TestCase):
    def test_visible_rollout_messages_exclude_injected_context(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_rollout(
                path,
                [
                    {"role": "user", "text": "\ufeff# AGENTS.md instructions\nsecret control text"},
                    {"role": "user", "text": "<environment_context>hidden</environment_context>"},
                    {"role": "user", "text": "真实问题"},
                    {"role": "assistant", "phase": "final_answer", "text": "真实答案"},
                ],
            )
            visible = remote_gateway.visible_rollout_messages(path)
            self.assertEqual([item["text"] for item in visible], ["真实问题", "真实答案"])

    def test_cli_json_is_safe_for_legacy_windows_code_pages(self):
        rendered = remote_gateway.cli_json_dumps({"text": "\ufeff中文"})
        rendered.encode("gbk")
        self.assertIn("\\ufeff", rendered)
        self.assertIn("\\u4e2d\\u6587", rendered)

    def test_text_message(self):
        self.assertEqual(message_text('{"text":"你好"}'), "你好")

    def test_post_and_resources(self):
        raw = '{"content":[[{"tag":"text","text":"说明"},{"tag":"img","image_key":"img_1"}]],"file_key":"file_1"}'
        self.assertIn("说明", message_text(raw))
        self.assertEqual(resource_keys(raw), [("file", "file_1"), ("image", "img_1")])

    def test_server_post_prefers_content_v2_without_duplicate_text(self):
        raw = '{"content":[[{"tag":"text","text":"旧版"}]],"content_v2":[[{"tag":"md","text":"新版"}]]}'
        self.assertEqual(message_text(raw), "新版")

    def test_message_image_upload_uses_official_multipart_shape(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"code":0,"data":{"image_key":"img_1"}}'

        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "preview.png"
            image.write_bytes(b"fake-png")
            client = FeishuClient("app", "secret")
            client._verified_tenant_key = "tenant"
            with (
                patch.object(client, "tenant_token", return_value="tenant-token"),
                patch.object(client, "_open", return_value=Response()) as opened,
            ):
                image_key = client.upload_message_image(image, image.parent)
        request = opened.call_args.args[0]
        self.assertEqual(image_key, "img_1")
        self.assertEqual(request.full_url, "https://open.feishu.cn/open-apis/im/v1/images")
        self.assertIn("multipart/form-data; boundary=", request.get_header("Content-type"))
        self.assertIn(b'name="image_type"', request.data)
        self.assertIn(b"message", request.data)
        self.assertIn(b'filename="preview.png"', request.data)

    def test_message_image_upload_rejects_unverified_tenant_and_outside_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            image = root / "preview.png"
            image.write_bytes(b"inside")
            outside = Path(raw) / "outside.png"
            outside.write_bytes(b"private")
            client = FeishuClient("app", "secret")
            with self.assertRaises(GatewayError):
                client.upload_message_image(image, root)
            client._verified_tenant_key = "tenant"
            with self.assertRaises(GatewayError):
                client.upload_message_image(outside, root)

    def test_image_reply_uses_image_message_content(self):
        client = FeishuClient("app", "secret")
        with patch.object(client, "request_json", return_value={"code": 0}) as requested:
            client.reply_image("om_1", "img_1", "uuid-1")
        body = requested.call_args.kwargs["body"]
        self.assertEqual(body["msg_type"], "image")
        self.assertEqual(json.loads(body["content"]), {"image_key": "img_1"})


class RoutingTests(unittest.TestCase):
    def test_codex_usage_formats_remaining_percentage_and_reset(self):
        text = remote_gateway.format_codex_usage(
            {
                "primary": {
                    "usedPercent": 33,
                    "windowDurationMins": 10080,
                    "resetsAt": 1786859664,
                },
                "secondary": None,
                "spendControlReached": False,
            }
        )
        self.assertIn("7天额度剩余 67%", text)
        self.assertIn("已用 33%", text)
        self.assertIn("重置", text)

    def test_usage_report_uuid_is_hourly_and_within_feishu_limit(self):
        first = datetime(2026, 8, 11, 7, 5, tzinfo=timezone.utc)
        same_hour = first + timedelta(minutes=40)
        next_hour = first + timedelta(hours=1)
        first_uuid = remote_gateway.usage_report_uuid("demo", first)
        self.assertEqual(first_uuid, remote_gateway.usage_report_uuid("demo", same_hour))
        self.assertNotEqual(first_uuid, remote_gateway.usage_report_uuid("demo", next_hour))
        self.assertLessEqual(len(first_uuid), 50)

    def test_sync_request_only_reports_usage_without_waiting_for_queue(self):
        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {
                "demo": {
                    "working_directory": r"C:\project",
                    "chat_id": "chat_1",
                }
            },
        }
        args = SimpleNamespace(
            project_key="demo",
            working_directory=None,
            ack_timeout=0.1,
            wait_timeout=3600,
            request_only=True,
            report_usage=True,
        )
        with tempfile.TemporaryDirectory() as raw:
            requests = Path(raw) / "requests"
            responses = Path(raw) / "responses"
            responses.mkdir(parents=True)
            (responses / "fixed.json").write_text(
                json.dumps({"ok": True, "added": {"demo": 2}}),
                encoding="utf-8",
            )
            with (
                patch.object(remote_gateway, "load_config", return_value=config),
                patch.object(remote_gateway, "SYNC_REQUESTS", requests),
                patch.object(remote_gateway, "SYNC_RESPONSES", responses),
                patch.object(remote_gateway.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch.object(
                    remote_gateway,
                    "send_codex_usage_report",
                    return_value={"message_id": "usage_1", "text": "Codex 用量"},
                ) as report,
            ):
                result = remote_gateway.request_sync(args)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["added"], {"demo": 2})
        self.assertEqual(result["usage_reports"]["demo"]["message_id"], "usage_1")
        report.assert_called_once_with(config, "demo")

    def test_sync_request_sends_default_inspection_report(self):
        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {"demo": {"working_directory": r"C:\project", "chat_id": "chat_1"}},
        }
        args = SimpleNamespace(
            project_key="demo",
            working_directory=None,
            ack_timeout=0.1,
            wait_timeout=3600,
            request_only=True,
            report_usage=False,
            inspection_report=True,
            first_inspection_message=False,
        )
        with tempfile.TemporaryDirectory() as raw:
            requests = Path(raw) / "requests"
            responses = Path(raw) / "responses"
            responses.mkdir(parents=True)
            (responses / "fixed.json").write_text(
                json.dumps({"ok": True, "added": {"demo": 0}}),
                encoding="utf-8",
            )
            with (
                patch.object(remote_gateway, "load_config", return_value=config),
                patch.object(remote_gateway, "SYNC_REQUESTS", requests),
                patch.object(remote_gateway, "SYNC_RESPONSES", responses),
                patch.object(remote_gateway.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch.object(
                    remote_gateway,
                    "send_default_inspection_message",
                    return_value={"message_id": "inspection_1", "kind": "routine"},
                ) as report,
            ):
                result = remote_gateway.request_sync(args)
        self.assertEqual(result["inspection_reports"]["demo"]["message_id"], "inspection_1")
        report.assert_called_once_with(config, "demo")

    def test_project_model_settings_are_explicit_parent_command_options(self):
        with patch.object(remote_gateway, "codex_cli_path", return_value=Path("codex.exe")):
            command = remote_gateway.build_codex_command(
                {
                    "working_directory": r"C:\project",
                    "agent_model": "gpt-5.6-sol",
                    "agent_reasoning_effort": "high",
                    "agent_service_tier": "fast",
                },
                Path("final.md"),
                [],
                "thread-1",
            )
        resume_index = command.index("resume")
        self.assertLess(command.index("--model"), resume_index)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="high"', command[:resume_index])
        self.assertIn('service_tier="fast"', command[:resume_index])
        self.assertIn("features.fast_mode=true", command[:resume_index])

    def test_project_model_settings_are_visible_in_prompt(self):
        prompt = remote_gateway.build_prompt(
            "demo",
            {
                "working_directory": r"C:\project",
                "agent_model": "gpt-5.6-sol",
                "agent_reasoning_effort": "high",
                "agent_service_tier": "fast",
            },
            {"message_id": "om_1", "content": '{"text":"test"}'},
            [],
            first_turn=False,
        )
        self.assertIn("远程 Codex 模型: gpt-5.6-sol", prompt)
        self.assertIn("推理等级: high", prompt)
        self.assertIn("加速档位: fast", prompt)
        self.assertIn("正常额度", prompt)
        self.assertIn("desktop-context --project-key demo", prompt)

    def test_forced_single_marker_is_removed_before_codex(self):
        item = {
            "message_id": "om_star",
            "content": '{"text":"  * 请单独处理"}',
        }
        combined = remote_gateway.combined_batch_item([item])
        self.assertTrue(remote_gateway.forced_single_message(item))
        self.assertEqual(message_text(combined["content"]), "请单独处理")

    def test_forced_single_marker_preserves_doc_override(self):
        item = {
            "message_id": "om_star_doc",
            "content": '{"text":"* /doc 请单独生成文档"}',
        }
        combined = remote_gateway.combined_batch_item([item])
        self.assertEqual(message_text(combined["content"]), "/doc 请单独生成文档")
        self.assertEqual(remote_gateway.explicit_routing_mode(item), "doc")

    def test_combined_batch_keeps_every_message_id(self):
        items = [
            {"message_id": "om_1", "content": '{"text":"第一条"}'},
            {"message_id": "om_2", "content": '{"text":"第二条"}'},
        ]
        combined = remote_gateway.combined_batch_item(items)
        text = message_text(combined["content"])
        self.assertEqual(combined["message_id"], "om_2")
        self.assertIn("om_1", text)
        self.assertIn("第一条", text)
        self.assertIn("om_2", text)
        self.assertIn("第二条", text)

    def test_resume_images_are_scoped_to_resume_subcommand(self):
        with patch.object(remote_gateway, "codex_cli_path", return_value=Path("codex.exe")):
            command = remote_gateway.build_codex_command(
                {"working_directory": r"C:\project"},
                Path("final.md"),
                [Path("screen.png")],
                "thread-1",
            )
        resume_index = command.index("resume")
        thread_index = command.index("thread-1")
        image_index = command.index("--image")
        self.assertLess(resume_index, thread_index)
        self.assertLess(thread_index, image_index)
        self.assertEqual(command[-1], "-")

    def test_welcome_message_preserves_chinese(self):
        text = remote_gateway.welcome_message({"working_directory": r"C:\项目\测试"})
        self.assertIn("已连接，可以直接使用", text)
        self.assertIn("本地项目 `测试`", text)
        self.assertNotIn(r"C:\项目\测试", text)
        self.assertIn("群名可以随时修改", text)
        self.assertIn("自动巡检默认每小时运行一次", text)
        self.assertIn("私有飞书文档", text)
        self.assertIn("Token plan 用量", text)
        self.assertNotIn("heartbeat", text.casefold())
        self.assertNotIn("?", text)

    def test_english_welcome_and_prompt_language_rule(self):
        project = {"working_directory": r"C:\projects\demo", "language": "en"}
        welcome = remote_gateway.welcome_message(project)
        self.assertIn("Connected — you can start now", welcome)
        self.assertIn("Token plan usage", welcome)
        prompt = remote_gateway.build_prompt(
            "demo",
            project,
            {"message_id": "om_demo", "content": json.dumps({"text": "Please inspect the tests."})},
            [],
            first_turn=False,
        )
        self.assertIn("Reply in the language of the user's current message", prompt)

    def test_general_welcome_is_isolated_and_has_no_hourly_catch_up(self):
        text = remote_gateway.welcome_message(
            {
                "working_directory": r"C:\runtime\hello",
                "workspace_mode": "general",
                "hourly_catch_up_enabled": False,
            }
        )
        self.assertIn("General 工作区", text)
        self.assertIn("不会访问其他项目", text)
        self.assertIn("自动巡检当前已关闭", text)
        self.assertNotIn(r"C:\runtime\hello", text)

    def test_hourly_catch_up_project_keys_honors_per_project_opt_out(self):
        workers = {
            "default": SimpleNamespace(project={"working_directory": r"C:\project"}),
            "disabled": SimpleNamespace(
                project={"working_directory": r"C:\runtime\hello", "hourly_catch_up_enabled": False}
            ),
        }
        self.assertEqual(remote_gateway.hourly_catch_up_project_keys(workers), ["default"])

    def test_send_welcome_uses_utf8_text_and_stable_uuid(self):
        class Client:
            calls = []

            def __init__(self, _app_id, _secret):
                pass

            def tenant_info(self):
                return {"tenant_key": "tenant_1"}

            def send_post(self, chat_id, markdown, request_uuid):
                self.calls.append((chat_id, markdown, request_uuid))
                return {"data": {"message_id": "om_welcome"}}

        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {
                "demo": {
                    "chat_id": "oc_1",
                    "working_directory": r"C:\项目\测试",
                }
            },
        }
        with (
            patch.object(remote_gateway, "load_config", return_value=config),
            patch.object(remote_gateway, "load_app_credentials", return_value=("app_1", "secret")),
            patch.object(remote_gateway, "FeishuClient", Client),
        ):
            first = remote_gateway.send_welcome(SimpleNamespace(project_key="demo"))
            second = remote_gateway.send_welcome(SimpleNamespace(project_key="demo"))
        self.assertEqual(first["message_id"], "om_welcome")
        self.assertEqual(Client.calls[0], Client.calls[1])
        self.assertIn("项目", Client.calls[0][1])
        self.assertNotIn("?", Client.calls[0][1])
        self.assertLessEqual(len(Client.calls[0][2]), 50)

    def test_hourly_automation_is_strict_utf8_and_uses_sync_wrapper(self):
        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {"demo": {"working_directory": r"C:\项目\测试"}},
        }
        args = SimpleNamespace(
            project_key="demo",
            automation_id="demo-feishu-sync",
            name="飞书每小时查漏",
            target_thread_id="thread-1",
            report_usage=True,
            activate=False,
            paused=False,
        )
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch.object(remote_gateway, "load_config", return_value=config),
                patch.object(remote_gateway, "AUTOMATIONS_ROOT", Path(raw)),
            ):
                result = remote_gateway.install_hourly_automation(args)
            content = Path(result["path"]).read_bytes().decode("utf-8", errors="strict")
        self.assertIn("飞书每小时查漏", content)
        self.assertIn("sync_feishu.ps1", content)
        self.assertIn("-InspectionReport", content)
        self.assertIn("默认三句巡检报告", content)
        self.assertIn("按需启动 Listener", content)
        self.assertIn("不要另发桌面总结", content)
        self.assertNotIn(r"C:\项目\测试", content)
        self.assertLess(len(remote_gateway.automation_prompt("demo", r"C:\项目\测试", True)), 300)
        self.assertIn("-RequestOnly", content)
        self.assertNotIn("-FirstInspectionMessage", content)
        self.assertIn('rrule = "FREQ=HOURLY;INTERVAL=1"', content)
        self.assertIn('status = "ACTIVE"', content)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertNotIn("?", content)

    def test_hourly_automation_defaults_to_three_line_report(self):
        args = remote_gateway.parser().parse_args(
            [
                "install-hourly-automation",
                "--project-key",
                "demo",
                "--automation-id",
                "demo-feishu-sync",
                "--name",
                "飞书自动巡检",
                "--target-thread-id",
                "thread-1",
            ]
        )
        self.assertTrue(args.report_usage)
        self.assertIn("-InspectionReport", remote_gateway.automation_prompt("demo", r"C:\project"))

    def test_hourly_automation_can_be_explicitly_paused(self):
        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {"demo": {"working_directory": r"C:\project"}},
        }
        args = SimpleNamespace(
            project_key="demo",
            automation_id="demo-feishu-sync",
            name="Feishu hourly catch-up",
            target_thread_id="thread-1",
            report_usage=False,
            activate=False,
            paused=True,
        )
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch.object(remote_gateway, "load_config", return_value=config),
                patch.object(remote_gateway, "AUTOMATIONS_ROOT", Path(raw)),
            ):
                result = remote_gateway.install_hourly_automation(args)
            content = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn('status = "PAUSED"', content)
        self.assertEqual(result["status"], "PAUSED")

    def test_hourly_automation_rejects_project_opt_out(self):
        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {
                "hello": {
                    "working_directory": r"C:\runtime\hello",
                    "workspace_mode": "general",
                    "hourly_catch_up_enabled": False,
                }
            },
        }
        args = SimpleNamespace(
            project_key="hello",
            automation_id="hello-feishu-sync",
            name="Hello hourly catch-up",
            target_thread_id="thread-1",
            report_usage=False,
            activate=False,
            paused=False,
        )
        with patch.object(remote_gateway, "load_config", return_value=config):
            with self.assertRaisesRegex(remote_gateway.GatewayError, "Hourly catch-up is disabled"):
                remote_gateway.install_hourly_automation(args)

    def test_request_reload_request_only_returns_without_waiting_for_ack(self):
        args = SimpleNamespace(wait_timeout=3600, ack_timeout=30, request_only=True)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with (
                patch.object(remote_gateway, "load_config", return_value={"projects": {}}),
                patch.object(remote_gateway, "assert_config"),
                patch.object(remote_gateway, "RELOAD_REQUESTS", root / "requests"),
                patch.object(remote_gateway, "RELOAD_RESPONSES", root / "responses"),
            ):
                result = remote_gateway.request_reload(args)
                request = json.loads(Path(result["request_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "queued")
        self.assertTrue(request["request_only"])

    def test_listener_launcher_is_demand_only_and_sync_enforces_it(self):
        scripts = Path(__file__).parent
        installer = (scripts / "install_listener_task.ps1").read_text(encoding="utf-8")
        sync = (scripts / "sync_feishu.ps1").read_text(encoding="utf-8")
        self.assertNotIn("New-ScheduledTaskTrigger", installer)
        self.assertNotIn("AtLogOn", installer)
        self.assertIn("RemoveChild", installer)
        self.assertIn("TriggerCount", installer)
        self.assertIn("Export-ScheduledTask", sync)
        self.assertIn("demand-start-only", sync)
        self.assertIn("$EffectiveRequestOnly = $RequestOnly -or $ReportUsage", sync)

    def test_explicit_direct_wins(self):
        self.assertFalse(remote_gateway.should_publish_document("/direct test", "x" * 5000))

    def test_explicit_doc_wins(self):
        self.assertTrue(remote_gateway.should_publish_document("/doc test", "short"))

    def test_complex_structure_routes_to_doc(self):
        answer = "```python\na=1\n```\n\n```text\nok\n```"
        self.assertTrue(remote_gateway.should_publish_document("test", answer))

    def test_extract_local_artifacts_only_accepts_explicit_files_inside_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            image = root / "preview image.png"
            image.write_bytes(b"png")
            report = root / "report.pdf"
            report.write_bytes(b"pdf")
            outside = Path(raw) / "private.pdf"
            outside.write_bytes(b"private")
            answer = (
                f"![preview](<{image.as_posix()}>)\n"
                "[report](report.pdf)\n"
                f"[outside](<{outside.as_posix()}>)\n"
                "[web](https://example.com/image.png)"
            )
            self.assertEqual(remote_gateway.extract_local_artifacts(answer, root), [image.resolve(), report.resolve()])

    def test_document_reply_exposes_separate_artifact_fallback(self):
        class Client:
            def __init__(self):
                self.text = ""
                self.images = []

            def reply_post(self, _message_id, markdown, _uuid):
                self.text = markdown

            def upload_message_image(self, path, _allowed_root):
                self.images.append(("upload", Path(path).name))
                return "img_1"

            def reply_image(self, _message_id, image_key, request_uuid):
                self.images.append(("reply", image_key, request_uuid))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image = root / "preview.png"
            image.write_bytes(b"png")
            answer = f"Result\n\n![preview](<{image.as_posix()}>)"
            final_path = root / "final.md"
            final_path.write_text(answer, encoding="utf-8")
            client = Client()
            published = {
                "document_url": "https://example.feishu.cn/docx/doc_1",
                "artifacts": [{"name": "preview.png", "url": "https://example.feishu.cn/file/file_1"}],
            }
            with patch.object(remote_gateway, "publish_document", return_value=published) as mocked:
                link = remote_gateway.reply_complete(
                    client,
                    {"chat_id": "oc_1", "working_directory": str(root)},
                    {"message_id": "om_1", "content": '{"text":"show image"}'},
                    answer,
                    final_path,
                    root / "gateway.log",
                )
            self.assertEqual(link, published["document_url"])
            self.assertIn("preview.png", client.text)
            self.assertEqual(mocked.call_args.args[2], [image.resolve()])
            self.assertEqual(client.images[0], ("upload", "preview.png"))
            self.assertEqual(client.images[1][0:2], ("reply", "img_1"))
            self.assertLessEqual(len(client.images[1][2]), 50)

    def test_chunks_preserve_content(self):
        text = "段落。" * 2000
        parts = remote_gateway.chunks(text, 300)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)

    def test_document_failure_falls_back_to_complete_chunks(self):
        class Client:
            def __init__(self):
                self.parts = []

            def reply_post(self, _message_id, markdown, _uuid):
                self.parts.append(markdown)

            def send_post(self, _chat_id, markdown, _uuid):
                self.parts.append(markdown)

        with tempfile.TemporaryDirectory() as raw:
            final_path = Path(raw) / "final.md"
            log_path = Path(raw) / "gateway.log"
            answer = "完整内容。" * 1200
            final_path.write_text(answer, encoding="utf-8")
            client = Client()
            with patch.object(remote_gateway, "publish_document", side_effect=RuntimeError("test failure")):
                link = remote_gateway.reply_complete(
                    client,
                    {"chat_id": "oc_1"},
                    {"message_id": "om_1", "content": '{"text":"/doc test"}'},
                    answer,
                    final_path,
                    log_path,
                )
            self.assertEqual(link, "")
            self.assertEqual("".join(client.parts), answer)
            self.assertIn("document publish failed", log_path.read_text(encoding="utf-8"))

    def test_reaction_state_machine_marks_only_processing_then_completion(self):
        class Client:
            app_id = "app_1"

            def __init__(self):
                self.events = []

            def add_reaction(self, _message_id, emoji_type):
                self.events.append(("add", emoji_type))
                return {"data": {"reaction_id": f"reaction-{emoji_type}"}}

            def remove_reaction(self, _message_id, reaction_id):
                self.events.append(("remove", reaction_id))
                return {"data": {}}

        with tempfile.TemporaryDirectory() as raw:
            final_path = Path(raw) / "final.md"
            final_path.write_text("完成", encoding="utf-8")
            client = Client()
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                worker = remote_gateway.ProjectWorker("demo", {"working_directory": raw}, client, Path(raw) / "log")
                worker.enqueue({
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "create_time": "1000",
                    "message_type": "text",
                    "content": '{"text":"test"}',
                    "sender": {"sender_type": "user"},
                })
                self.assertEqual(client.events, [])
                item = worker.store.next_pending()
                with (
                    patch.object(remote_gateway, "run_codex", return_value=("完成", "thread-1", final_path)),
                    patch.object(remote_gateway, "reply_complete", side_effect=lambda *_args: client.events.append(("reply", "complete")) or ""),
                ):
                    worker._process(item)
                stored = worker.store.state["messages"]["om_1"]
        self.assertEqual(
            client.events,
            [
                ("add", "Typing"),
                ("reply", "complete"),
                ("remove", "reaction-Typing"),
                ("add", "CheckMark"),
            ],
        )
        self.assertEqual(stored["status"], "completed")
        self.assertFalse(stored["working_reaction_active"])
        self.assertEqual(stored["completion_reaction_type"], "CheckMark")

    def test_failed_processing_clears_typing_without_checkmark(self):
        class Client:
            app_id = "app_1"

            def __init__(self):
                self.events = []

            def add_reaction(self, _message_id, emoji_type):
                self.events.append(("add", emoji_type))
                return {"data": {"reaction_id": f"reaction-{emoji_type}"}}

            def remove_reaction(self, _message_id, reaction_id):
                self.events.append(("remove", reaction_id))
                return {"data": {}}

        with tempfile.TemporaryDirectory() as raw:
            client = Client()
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                worker = remote_gateway.ProjectWorker("demo", {"working_directory": raw}, client, Path(raw) / "log")
                worker.enqueue({
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "create_time": "1000",
                    "message_type": "text",
                    "content": '{"text":"test"}',
                    "sender": {"sender_type": "user"},
                })
                item = worker.store.next_pending()
                with patch.object(remote_gateway, "run_codex", side_effect=RuntimeError("boom")):
                    worker._process(item)
                stored = worker.store.state["messages"]["om_1"]
        self.assertEqual(client.events, [("add", "Typing"), ("remove", "reaction-Typing")])
        self.assertEqual(stored["status"], "failed")
        self.assertFalse(stored["working_reaction_active"])

    def test_batch_processing_replies_once_and_completes_every_source(self):
        class Client:
            app_id = "app_1"

            def __init__(self):
                self.events = []

            def add_reaction(self, message_id, emoji_type):
                self.events.append(("add", message_id, emoji_type))
                return {"data": {"reaction_id": f"reaction-{message_id}-{emoji_type}"}}

            def remove_reaction(self, message_id, reaction_id):
                self.events.append(("remove", message_id, reaction_id))
                return {"data": {}}

        with tempfile.TemporaryDirectory() as raw:
            final_path = Path(raw) / "final.md"
            final_path.write_text("批次完成", encoding="utf-8")
            client = Client()
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                worker = remote_gateway.ProjectWorker("demo", {"working_directory": raw}, client, Path(raw) / "log")
                for message_id, create_time, text in [
                    ("om_1", "1000", "补充一"),
                    ("om_2", "1500", "补充二"),
                ]:
                    worker.enqueue({
                        "message_id": message_id,
                        "chat_id": "oc_1",
                        "create_time": create_time,
                        "message_type": "text",
                        "content": '{"text":"' + text + '"}',
                        "sender": {"sender_type": "user"},
                    })
                items = worker.store.next_pending_batch()
                with (
                    patch.object(remote_gateway, "run_codex_batch", return_value=("批次完成", "thread-1", final_path)),
                    patch.object(
                        remote_gateway,
                        "reply_complete",
                        side_effect=lambda _client, _project, item, *_args: client.events.append(
                            ("reply", item["message_id"])
                        ) or "",
                    ),
                ):
                    worker._process_batch(items)
                stored = worker.store.state["messages"]
        self.assertEqual([event for event in client.events if event[0] == "reply"], [("reply", "om_2")])
        self.assertEqual(stored["om_1"]["status"], "completed")
        self.assertEqual(stored["om_2"]["status"], "completed")
        self.assertEqual(stored["om_1"]["reply_source_message_id"], "om_2")
        self.assertEqual(stored["om_1"]["batch_message_ids"], ["om_1", "om_2"])


class ContextBridgeTests(unittest.TestCase):
    def test_desktop_context_is_read_only_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            config_path = root / "config.json"
            database = root / "state.sqlite"
            project_dir = root / "project"
            other_dir = root / "other"
            project_dir.mkdir()
            other_dir.mkdir()
            desktop_rollout = root / "desktop.jsonl"
            remote_rollout = root / "remote.jsonl"
            other_rollout = root / "other.jsonl"
            guardian_rollout = root / "guardian.jsonl"
            write_rollout(
                desktop_rollout,
                [
                    {"role": "user", "text": "桌面问题"},
                    {"role": "developer", "text": "隐藏规则"},
                    {"role": "assistant", "phase": "commentary", "text": "处理中"},
                    {"role": "assistant", "phase": "final_answer", "text": "桌面答案"},
                ],
            )
            write_rollout(remote_rollout, [{"role": "user", "text": "飞书远程内容"}])
            write_rollout(other_rollout, [{"role": "user", "text": "其他项目秘密"}])
            write_rollout(guardian_rollout, [{"role": "user", "text": "内部审查"}])
            create_thread_db(
                database,
                [
                    ("desktop-1", str(desktop_rollout), str(project_dir), "桌面对话", "vscode", "user", 40),
                    ("remote-1", str(remote_rollout), str(project_dir), "远程线程", "exec", "user", 30),
                    ("other-1", str(other_rollout), str(other_dir), "其他项目", "vscode", "user", 20),
                    ("guard-1", str(guardian_rollout), str(project_dir), "审查", '{"subagent":{"other":"guardian"}}', "subagent", 10),
                ],
            )
            remote_gateway.atomic_write_json(
                config_path,
                {
                    "app_id": "cli_test",
                    "expected_tenant_key": "tenant",
                    "projects": {"demo": {"working_directory": str(project_dir)}},
                },
            )
            state_path = runtime / "projects" / "demo" / "state.json"
            remote_gateway.atomic_write_json(
                state_path,
                {"schema_version": 1, "thread_id": "remote-1", "cursor": 0, "messages": {}},
            )
            args = SimpleNamespace(project_key="demo", thread_id=None, limit=20)
            with (
                patch.object(remote_gateway, "load_config", return_value=remote_gateway.load_json(config_path, {})),
                patch.object(remote_gateway, "REMOTE_STATE", runtime),
                patch.object(remote_gateway, "CODEX_STATE_DB", database),
            ):
                result = remote_gateway.desktop_context(args)
            self.assertFalse(result["automatic_mirroring"])
            self.assertEqual([thread["thread_id"] for thread in result["threads"]], ["desktop-1"])
            self.assertEqual(
                [(message["role"], message["text"]) for message in result["transcript"]],
                [("user", "桌面问题"), ("assistant", "桌面答案")],
            )
            self.assertTrue(state_path.is_file())

    def test_general_desktop_context_allows_only_explicit_bootstrap_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            database = root / "state.sqlite"
            isolated_dir = root / "general-runtime"
            desktop_dir = root / "desktop-general"
            isolated_dir.mkdir()
            desktop_dir.mkdir()
            hello_rollout = root / "hello.jsonl"
            other_rollout = root / "other.jsonl"
            write_rollout(hello_rollout, [{"role": "user", "text": "Hello 历史问题"}])
            write_rollout(other_rollout, [{"role": "user", "text": "其他 General 隐私"}])
            create_thread_db(
                database,
                [
                    ("hello-thread", str(hello_rollout), str(desktop_dir), "Hello", "vscode", "user", 40),
                    ("other-thread", str(other_rollout), str(desktop_dir), "Other", "vscode", "user", 30),
                ],
            )
            config = {
                "app_id": "cli_test",
                "expected_tenant_key": "tenant",
                "projects": {
                    "hello": {
                        "working_directory": str(isolated_dir),
                        "workspace_mode": "general",
                        "bootstrap_source_thread_id": "hello-thread",
                    }
                },
            }
            args = SimpleNamespace(project_key="hello", thread_id=None, limit=20)
            with (
                patch.object(remote_gateway, "load_config", return_value=config),
                patch.object(remote_gateway, "REMOTE_STATE", runtime),
                patch.object(remote_gateway, "CODEX_STATE_DB", database),
            ):
                result = remote_gateway.desktop_context(args)
            self.assertEqual([thread["thread_id"] for thread in result["threads"]], ["hello-thread"])
            self.assertEqual(result["transcript"][0]["text"], "Hello 历史问题")
            self.assertNotIn("其他 General 隐私", json.dumps(result, ensure_ascii=False))

    def test_feishu_context_includes_pending_queue_and_remote_final(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            config_path = root / "config.json"
            database = root / "state.sqlite"
            project_dir = root / "project"
            project_dir.mkdir()
            remote_rollout = root / "remote.jsonl"
            prompt = (
                "你是通过飞书为本地项目提供服务的远程 Codex。\n\n"
                "用户消息：\n继续飞书任务\n\n本地附件：\n- 无"
            )
            write_rollout(
                remote_rollout,
                [
                    {"role": "user", "text": prompt},
                    {"role": "assistant", "phase": "final_answer", "text": "远程答案"},
                ],
            )
            create_thread_db(
                database,
                [("remote-1", str(remote_rollout), str(project_dir), "远程线程", "exec", "user", 40)],
            )
            remote_gateway.atomic_write_json(
                config_path,
                {
                    "app_id": "cli_test",
                    "expected_tenant_key": "tenant",
                    "projects": {"demo": {"working_directory": str(project_dir)}},
                },
            )
            state_path = runtime / "projects" / "demo" / "state.json"
            remote_gateway.atomic_write_json(
                state_path,
                {
                    "schema_version": 1,
                    "thread_id": "remote-1",
                    "cursor": 0,
                    "messages": {
                        "om_pending": {
                            "message_id": "om_pending",
                            "create_time": 1,
                            "content": json.dumps({"text": "尚未处理的问题"}, ensure_ascii=False),
                            "status": "pending",
                        }
                    },
                },
            )
            args = SimpleNamespace(project_key="demo", limit=20)
            with (
                patch.object(remote_gateway, "load_config", return_value=remote_gateway.load_json(config_path, {})),
                patch.object(remote_gateway, "REMOTE_STATE", runtime),
                patch.object(remote_gateway, "CODEX_STATE_DB", database),
            ):
                result = remote_gateway.feishu_context(args)
            self.assertFalse(result["automatic_mirroring"])
            self.assertEqual(result["recent_messages"][0]["status"], "pending")
            self.assertEqual(result["recent_messages"][0]["text"], "尚未处理的问题")
            self.assertEqual(result["transcript"][0]["text"], "继续飞书任务")
            self.assertEqual(result["transcript"][1]["text"], "远程答案")


class StoreTests(unittest.TestCase):
    def test_set_project_profile_persists_model_effort_without_enabling_paid_tier(self):
        config = {
            "app_id": "app_1",
            "expected_tenant_key": "tenant_1",
            "projects": {"demo": {"working_directory": r"C:\project"}},
        }
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "config.json"
            with (
                patch.object(remote_gateway, "CONFIG_PATH", target),
                patch.object(remote_gateway, "load_config", return_value=config),
            ):
                result = remote_gateway.set_project_profile(
                    SimpleNamespace(
                        project_key="demo",
                        model="gpt-5.6-sol",
                        reasoning_effort="high",
                        service_tier=None,
                    )
                )
            stored = remote_gateway.load_json(target, {})
        self.assertEqual(result["agent_model"], "gpt-5.6-sol")
        self.assertEqual(stored["projects"]["demo"]["agent_reasoning_effort"], "high")
        self.assertNotIn("agent_service_tier", stored["projects"]["demo"])

    def test_pending_burst_batches_until_forced_single_barrier(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                for message_id, create_time, text in [
                    ("om_1", "1000", "连续补充一"),
                    ("om_2", "2000", "连续补充二"),
                    ("om_3", "3000", "* 必须单独"),
                    ("om_4", "4000", "后续消息"),
                ]:
                    store.enqueue({
                        "message_id": message_id,
                        "chat_id": "oc_1",
                        "create_time": create_time,
                        "message_type": "text",
                        "content": '{"text":"' + text + '"}',
                        "sender": {"sender_type": "user"},
                    })
                first = store.next_pending_batch()
                second = store.next_pending_batch()
                third = store.next_pending_batch()
        self.assertEqual([item["message_id"] for item in first], ["om_1", "om_2"])
        self.assertEqual([item["message_id"] for item in second], ["om_3"])
        self.assertEqual([item["message_id"] for item in third], ["om_4"])

    def test_merge_window_separates_old_topics(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                for message_id, create_time in [("om_1", "1000"), ("om_2", "302000")]:
                    store.enqueue({
                        "message_id": message_id,
                        "chat_id": "oc_1",
                        "create_time": create_time,
                        "message_type": "text",
                        "content": '{"text":"普通消息"}',
                        "sender": {"sender_type": "user"},
                    })
                batch = store.next_pending_batch(merge_window_seconds=300)
        self.assertEqual([item["message_id"] for item in batch], ["om_1"])

    def test_duplicate_message_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                message = {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "create_time": "1000",
                    "message_type": "text",
                    "content": '{"text":"hello"}',
                    "sender": {"sender_type": "user"},
                }
                self.assertTrue(store.enqueue(message))
                self.assertFalse(store.enqueue(message))
                self.assertEqual(len(store.state["messages"]), 1)

    def test_bot_message_is_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                self.assertFalse(
                    store.enqueue(
                        {
                            "message_id": "om_bot",
                            "sender": {"sender_type": "app"},
                        }
                    )
                )

    def test_system_message_is_ignored_and_advances_cursor(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                self.assertFalse(
                    store.enqueue(
                        {
                            "message_id": "om_system",
                            "create_time": "5000",
                            "message_type": "system",
                            "sender": {},
                        }
                    )
                )
                self.assertEqual(store.state["cursor"], 5)
                self.assertEqual(store.state["messages"], {})

    def test_interrupted_message_is_recovered(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                store.state["messages"]["om_1"] = {
                    "message_id": "om_1",
                    "message_type": "text",
                    "sender": {"sender_type": "user"},
                    "status": "processing",
                }
                store.recover_interrupted()
                self.assertEqual(store.state["messages"]["om_1"]["status"], "pending")

    def test_queue_snapshot_blocks_reload_while_work_is_active(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                store.state["messages"] = {
                    "om_pending": {"message_id": "om_pending", "status": "pending", "attempts": 0},
                    "om_done": {"message_id": "om_done", "status": "completed", "attempts": 1},
                }
                worker = SimpleNamespace(store=store)
                service = object.__new__(remote_gateway.GatewayService)
                service.workers = {"demo": worker}
                snapshot = service.queue_snapshot(["demo"])
                self.assertEqual(snapshot["counts"], {"demo": {"pending": 1, "completed": 1}})
                self.assertEqual(snapshot["active"][0]["message_id"], "om_pending")

    def test_queue_snapshot_reports_terminal_failure_without_treating_it_as_active(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(remote_gateway, "REMOTE_STATE", Path(raw)):
                store = remote_gateway.ProjectStore("demo")
                store.state["messages"] = {
                    "om_failed": {
                        "message_id": "om_failed",
                        "status": "failed",
                        "attempts": 3,
                        "error": "boom",
                    }
                }
                worker = SimpleNamespace(store=store)
                service = object.__new__(remote_gateway.GatewayService)
                service.workers = {"demo": worker}
                snapshot = service.queue_snapshot(["demo"])
                self.assertEqual(snapshot["active"], [])
                self.assertEqual(snapshot["terminal_failures"][0]["error"], "boom")


if __name__ == "__main__":
    unittest.main()
