from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import gateway_common
import install_startup_hook
import listener_control
import reminders
import remote_gateway
import uninstall


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]


class SetupContractTests(unittest.TestCase):
    def test_native_media_permission_is_an_installation_preflight(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        setup = (SKILL_ROOT / "references" / "setup.md").read_text(encoding="utf-8")
        self.assertIn("installation prerequisite", skill)
        self.assertIn("installation preflight gate", setup)
        self.assertIn("im:resource", setup)
        self.assertIn("Do not continue to project binding, welcome, or the success report", setup)

    def test_setup_requires_app_activation_and_real_inbound_message(self):
        setup = (SKILL_ROOT / "references" / "setup.md").read_text(encoding="utf-8")
        self.assertIn("app-version creation, release, enablement", setup)
        self.assertIn("receive a real user message and read history", setup)
        self.assertIn("can only send messages has not passed setup", setup)

    def test_setup_success_requires_a_visible_real_image_before_welcome(self):
        setup = (SKILL_ROOT / "references" / "setup.md").read_text(encoding="utf-8")
        visible = setup.index("user confirms the image is visible")
        welcome = setup.index("Only then send the Feishu welcome message")
        self.assertLess(visible, welcome)

    def test_readmes_disclose_permission_and_real_image_acceptance(self):
        chinese = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        english = (REPO_ROOT / "README_EN.md").read_text(encoding="utf-8")
        self.assertIn("图片/文件和私有文档所需权限", chinese)
        self.assertIn("真实测试图片", chinese)
        self.assertIn("permissions needed for messages, images/files, and private documents", english)
        self.assertIn("real test image", english)

    def test_personal_is_default_and_team_tenant_requires_explicit_targeted_opt_in(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        setup = (SKILL_ROOT / "references" / "setup.md").read_text(encoding="utf-8")
        chinese = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        english = (REPO_ROOT / "README_EN.md").read_text(encoding="utf-8")

        account_gate = setup.index("Before CUA clicks any create-app control")
        app_inventory = setup.index("before creating any app")
        self.assertLess(account_gate, app_inventory)
        self.assertIn("Default to and recommend a personal account for first-time setup", setup)
        self.assertIn("explicitly targets a group under that tenant", setup)
        self.assertIn("team-tenant consent alone is not consent for unrestricted member access", setup)
        self.assertIn("Silence and a broad request to automate setup are not consent", setup)
        self.assertIn("user gives informed consent for that specific tenant", skill)
        self.assertIn("默认并推荐个人版飞书", chinese)
        self.assertIn("明确目标就是接入某个公司飞书账号下的工作群", chinese)
        self.assertIn("确认接受管理员审批和组织可见性", chinese)
        self.assertIn("personal Feishu account is the default and recommended choice", english)
        self.assertIn("only when your request explicitly targets a group under that account", english)

    def test_readmes_set_diy_expectations_without_presenting_examples_as_defaults(self):
        chinese = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        english = (REPO_ROOT / "README_EN.md").read_text(encoding="utf-8")
        self.assertIn("不是完全托管式服务", chinese)
        self.assertIn("这些属于按需 DIY 的扩展，不是默认开启的功能", chinese)
        self.assertIn("not a fully managed service", english)
        self.assertIn("opt-in DIY extensions, not default features", english)
        self.assertTrue(chinese.split("\n\n")[2].startswith("一个可 DIY 的本地 Codex 手机助手"))
        self.assertTrue(english.split("\n\n")[2].startswith("A DIY-friendly mobile assistant"))
        self.assertLess(chinese.index("想在安卓或华为手机上远程使用 Codex"), chinese.index("你可以在飞书里连续"))


class PlatformTests(unittest.TestCase):
    def test_startup_hook_preserves_existing_hooks_and_is_idempotent(self):
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "^compact$",
                        "hooks": [{"type": "command", "command": "keep-me"}],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "also-keep-me"}]}],
            }
        }
        first = install_startup_hook.merged_config(existing, "Windows")
        second = install_startup_hook.merged_config(first, "Windows")
        session_hooks = second["hooks"]["SessionStart"]
        ours = [group for group in session_hooks if install_startup_hook.is_ours(group)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(session_hooks[0]["hooks"][0]["command"], "keep-me")
        self.assertEqual(second["hooks"]["Stop"][0]["hooks"][0]["command"], "also-keep-me")
        self.assertIn("listener_control.py", ours[0]["hooks"][0]["command"])
        self.assertEqual(ours[0]["matcher"], "^(startup|resume)$")

    def test_startup_hook_replaces_only_obsolete_description(self):
        obsolete = {
            "description": "No SessionStart process. Feishu sync starts on explicit request or hourly reconciliation.",
            "hooks": {},
        }
        updated = install_startup_hook.merged_config(obsolete, "Windows")
        self.assertEqual(updated["description"], install_startup_hook.DESCRIPTION)

        custom = {"description": "Keep this operator note.", "hooks": {}}
        preserved = install_startup_hook.merged_config(custom, "Windows")
        self.assertEqual(preserved["description"], "Keep this operator note.")

    def test_startup_hook_uninstall_removes_only_owned_hook(self):
        existing = {
            "description": install_startup_hook.DESCRIPTION,
            "hooks": {
                "SessionStart": [
                    install_startup_hook.hook_group("Windows"),
                    {"matcher": "^compact$", "hooks": [{"type": "command", "command": "keep-me"}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "also-keep-me"}]}],
            },
        }
        updated, removed = install_startup_hook.without_ours(existing)
        self.assertTrue(removed)
        self.assertNotIn("description", updated)
        self.assertEqual(updated["hooks"]["SessionStart"][0]["hooks"][0]["command"], "keep-me")
        self.assertEqual(updated["hooks"]["Stop"][0]["hooks"][0]["command"], "also-keep-me")

    def test_bootstrap_accepts_explicit_codex_cli_outside_path(self):
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "codex"
            executable.write_text("", encoding="utf-8")
            with (
                patch.dict("os.environ", {"CODEX_CLI_PATH": str(executable)}),
                patch.object(bootstrap.shutil, "which", return_value=None),
            ):
                report = bootstrap.preflight()
        self.assertTrue(report["codex_available"])
        self.assertFalse(report["codex_on_path"])
        self.assertEqual(report["codex_path"], str(executable))

    def test_platform_state_roots_preserve_windows_and_add_macos(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Local"}):
            self.assertEqual(gateway_common.user_data_root("Windows"), Path(r"C:\Local"))
        self.assertEqual(
            gateway_common.user_data_root("Darwin"),
            Path.home() / "Library" / "Application Support",
        )

    def test_macos_keychain_round_trip_uses_security_without_printing_secret(self):
        secret = b"private-secret-value"
        encoded = base64.b64encode(secret).decode("ascii")
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(list(command))
            if "find-generic-password" in command and "-w" in command:
                return subprocess.CompletedProcess(command, 0, stdout=encoded + "\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        target = Path("publisher") / "secret.bin"
        with patch.object(gateway_common.subprocess, "run", side_effect=fake_run):
            gateway_common.save_protected(target, secret, system="Darwin")
            loaded = gateway_common.load_protected(target, system="Darwin")
        self.assertEqual(loaded, secret)
        self.assertTrue(any(call[:2] == ["/usr/bin/security", "add-generic-password"] for call in calls))
        self.assertTrue(any(call[:2] == ["/usr/bin/security", "find-generic-password"] for call in calls))

    def test_macos_keychain_denial_has_actionable_error_without_secret(self):
        secret = b"must-not-appear"

        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 36, stdout="", stderr="User interaction is not allowed")

        with patch.object(gateway_common.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(gateway_common.GatewayError) as raised:
                gateway_common.save_protected(Path("publisher") / "secret.bin", secret, system="Darwin")
        self.assertIn("unlock the login keychain", str(raised.exception))
        self.assertNotIn(secret.decode(), str(raised.exception))

    def test_macos_listener_is_demand_start_launch_agent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            python = root / "runtime" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            codex = root / "bin" / "codex"
            codex.parent.mkdir(parents=True)
            codex.write_text("", encoding="utf-8")
            plist = root / "LaunchAgents" / "listener.plist"
            runs: list[list[str]] = []

            def fake_run(command):
                runs.append(list(command))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with (
                patch.object(listener_control, "runtime_python", return_value=python),
                patch.object(listener_control, "codex_cli_candidate", return_value=codex),
                patch.object(listener_control, "mac_plist_path", return_value=plist),
                patch.object(listener_control, "mac_domain", return_value="gui/501"),
                patch.object(listener_control, "mac_registered", return_value=False),
                patch.object(listener_control, "REMOTE_STATE", root / "state"),
                patch.object(listener_control, "_run", side_effect=fake_run),
            ):
                result = listener_control.install_macos()
            payload_text = plist.read_text(encoding="utf-8")
            payload = plistlib.loads(plist.read_bytes())
        self.assertTrue(result["demand_start_only"])
        self.assertIn("<key>RunAtLoad</key>\n\t<false/>", payload_text)
        self.assertIn("supervisor.py", payload_text)
        self.assertEqual(payload["EnvironmentVariables"]["CODEX_CLI_PATH"], str(codex.resolve()))
        self.assertTrue(any("bootstrap" in call for call in runs))

    def test_macos_listener_refuses_install_without_codex_cli(self):
        with tempfile.TemporaryDirectory() as raw:
            python = Path(raw) / "runtime" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            with (
                patch.object(listener_control, "runtime_python", return_value=python),
                patch.object(listener_control, "codex_cli_candidate", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "Codex CLI is missing"):
                    listener_control.install_macos()

    def test_macos_session_start_never_force_restarts_running_listener(self):
        runs: list[list[str]] = []

        def fake_run(command):
            runs.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            patch.object(listener_control.platform, "system", return_value="Darwin"),
            patch.object(listener_control, "mac_registered", return_value=True),
            patch.object(listener_control, "mac_domain", return_value="gui/501"),
            patch.object(listener_control, "_run", side_effect=fake_run),
        ):
            result = listener_control.start_listener()

        self.assertTrue(result["ok"])
        self.assertEqual(
            runs,
            [["launchctl", "kickstart", "gui/501/feishu-codex-remote.listener"]],
        )
        self.assertNotIn("-k", runs[0])

    def test_macos_automation_uses_python_wrapper(self):
        with (
            patch.object(remote_gateway.platform, "system", return_value="Darwin"),
            patch.object(remote_gateway, "runtime_python_path", return_value=Path("/runtime/bin/python")),
        ):
            prompt = remote_gateway.automation_prompt("demo", "/tmp/project", True)
        self.assertIn("sync_feishu.py", prompt)
        self.assertIn("--inspection-report", prompt)
        self.assertNotIn("--first-inspection-message", prompt)
        self.assertNotIn("powershell", prompt.lower())


class FirstInspectionTests(unittest.TestCase):
    def test_first_inspection_message_is_sent_once(self):
        class Client:
            calls: list[tuple[str, str, str]] = []

            def __init__(self, _app_id, _secret):
                pass

            def tenant_info(self):
                return {"tenant_key": "tenant_1"}

            def send_post(self, chat_id, text, request_uuid):
                self.calls.append((chat_id, text, request_uuid))
                return {"data": {"message_id": "om_inspection"}}

        config = {
            "app_id": "cli_demo",
            "expected_tenant_key": "tenant_1",
            "projects": {"demo": {"chat_id": "oc_demo", "working_directory": r"C:\project"}},
        }
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "demo"
            remote_gateway.atomic_write_json(
                runtime / "state.json",
                {"messages": {"om_1": {"status": "pending"}}},
            )
            with (
                patch.object(remote_gateway, "project_runtime", return_value=runtime),
                patch.object(remote_gateway, "load_app_credentials", return_value=("cli_demo", "secret")),
                patch.object(remote_gateway, "FeishuClient", Client),
            ):
                first = remote_gateway.send_first_inspection_message(config, "demo")
                second = remote_gateway.send_first_inspection_message(config, "demo")
        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(len(Client.calls), 1)
        self.assertIn("自动巡检首次运行成功", Client.calls[0][1])
        self.assertIn("发现 1 条待处理消息", Client.calls[0][1])
        self.assertIn("Codex 使用额度", Client.calls[0][1])

    def test_routine_inspection_is_exactly_three_short_lines(self):
        limits = {
            "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 0},
            "secondary": None,
            "spendControlReached": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "demo"
            event_log = runtime / "runs" / "run-1" / "events.jsonl"
            event_log.parent.mkdir(parents=True)
            event_log.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "已完成数据整理。\n正在运行验证。"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            remote_gateway.atomic_write_json(
                runtime / "state.json",
                {
                    "messages": {
                        "om_1": {"status": "pending"},
                        "om_2": {
                            "message_id": "om_2",
                            "status": "processing",
                            "batch_id": "batch-1",
                            "processing_started_at": datetime.now().astimezone().isoformat(),
                            "run_event_log": str(event_log),
                        },
                        "om_3": {"status": "failed"},
                    }
                },
            )
            with (
                patch.object(remote_gateway, "project_runtime", return_value=runtime),
                patch.object(remote_gateway, "codex_rate_limits", return_value=limits),
            ):
                lines = remote_gateway.routine_inspection_text("demo").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("状态：Listener 正常，任务执行中", lines[0])
        self.assertIn("最近进展：已完成数据整理。 正在运行验证。", lines[0])
        self.assertIn("待处理 1，处理中 1，失败 1", lines[0])
        self.assertNotIn("events.jsonl", lines[0])
        self.assertIn("Codex 用量：5小时额度剩余 80%", lines[1])
        self.assertIn("通过对话指令修改巡检", lines[2])

    def test_routine_inspection_uuid_is_idempotent_per_hour(self):
        first = datetime(2026, 8, 12, 10, 5, tzinfo=timezone.utc)
        self.assertEqual(
            remote_gateway.inspection_report_uuid("demo", first),
            remote_gateway.inspection_report_uuid("demo", first + timedelta(minutes=40)),
        )
        self.assertNotEqual(
            remote_gateway.inspection_report_uuid("demo", first),
            remote_gateway.inspection_report_uuid("demo", first + timedelta(hours=1)),
        )

    def test_active_task_progress_uuid_is_idempotent_per_batch_and_hour(self):
        first = datetime(2026, 8, 12, 10, 5, tzinfo=timezone.utc)
        self.assertEqual(
            remote_gateway.active_task_progress_uuid("demo", "batch-1", first),
            remote_gateway.active_task_progress_uuid("demo", "batch-1", first + timedelta(minutes=40)),
        )
        self.assertNotEqual(
            remote_gateway.active_task_progress_uuid("demo", "batch-1", first),
            remote_gateway.active_task_progress_uuid("demo", "batch-1", first + timedelta(hours=1)),
        )
        self.assertLessEqual(len(remote_gateway.active_task_progress_uuid("demo", "batch-1", first)), 50)

    def test_progress_report_honors_hourly_opt_out_before_reading_state(self):
        config = {
            "projects": {
                "demo": {
                    "chat_id": "chat_1",
                    "hourly_catch_up_enabled": False,
                }
            }
        }
        with patch.object(remote_gateway, "active_task_progress_snapshot") as snapshot:
            result = remote_gateway.send_active_task_progress_report(config, "demo")
        self.assertEqual(result["reason"], "hourly-disabled")
        snapshot.assert_not_called()

    def test_english_routine_inspection_is_three_lines(self):
        limits = {
            "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 0},
            "secondary": None,
            "spendControlReached": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "demo"
            remote_gateway.atomic_write_json(runtime / "state.json", {"messages": {}})
            with (
                patch.object(remote_gateway, "project_runtime", return_value=runtime),
                patch.object(remote_gateway, "codex_rate_limits", return_value=limits),
            ):
                lines = remote_gateway.routine_inspection_text("demo", "en").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Status: Listener healthy; pending 0, processing 0, failed 0.")
        self.assertIn("Codex usage: 5 hours: 75% remaining", lines[1])
        self.assertIn("use chat instructions", lines[2])

    def test_usage_failure_is_a_three_line_non_blocking_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "demo"
            remote_gateway.atomic_write_json(runtime / "state.json", {"messages": {}})
            with (
                patch.object(remote_gateway, "project_runtime", return_value=runtime),
                patch.object(remote_gateway, "codex_rate_limits", side_effect=RuntimeError("unsupported")),
            ):
                chinese = remote_gateway.routine_inspection_text("demo").splitlines()
                english = remote_gateway.routine_inspection_text("demo", "en").splitlines()
        self.assertEqual(len(chinese), 3)
        self.assertEqual(chinese[1], "Codex 用量：暂时无法读取。")
        self.assertEqual(len(english), 3)
        self.assertEqual(english[1], "Codex usage: temporarily unavailable.")


class UninstallTests(unittest.TestCase):
    def test_preview_identifies_owned_components_and_never_deletes_feishu_content(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            remote_state = root / "remote"
            config_path = remote_state / "config.json"
            automations = root / "automations"
            automation = automations / "demo-inspection"
            automation.mkdir(parents=True)
            (automation / "automation.toml").write_text(
                'prompt = "run sync_feishu.py --project-key demo"\n', encoding="utf-8"
            )
            gateway_common.atomic_write_json(
                config_path,
                {"projects": {"demo": {"working_directory": str(root / "project"), "automation_id": "demo-inspection"}}},
            )
            state = remote_state / "projects" / "demo" / "state.json"
            gateway_common.atomic_write_json(state, {"messages": {"om_1": {"status": "pending"}}})
            with (
                patch.object(uninstall.gateway_common, "CONFIG_PATH", config_path),
                patch.object(uninstall.gateway_common, "REMOTE_STATE", remote_state),
                patch.object(uninstall, "AUTOMATIONS_ROOT", automations),
            ):
                preview = uninstall.build_preview("project", "demo", False)
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["active_messages"][0]["message_id"], "om_1")
        self.assertIn(str(automation), preview["remove"]["automation_directories"])
        self.assertIn("Feishu conversations", preview["preserve"])
        self.assertNotIn("Feishu conversations", preview["remove"])

    def test_apply_refuses_active_work_without_explicit_abandon(self):
        preview = {"active_messages": [{"project_key": "demo", "message_id": "om_1", "status": "processing"}]}
        with self.assertRaisesRegex(RuntimeError, "active messages"):
            uninstall.apply_uninstall(preview, False)

    def test_automation_ownership_requires_project_key_and_sync_script(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            good = root / "good" / "automation.toml"
            bad = root / "bad" / "automation.toml"
            good.parent.mkdir(parents=True)
            bad.parent.mkdir(parents=True)
            good.write_text('prompt = "run sync_feishu.py --project-key demo"\n', encoding="utf-8")
            bad.write_text('prompt = "unrelated task for demo"\n', encoding="utf-8")
            with patch.object(uninstall, "AUTOMATIONS_ROOT", root):
                self.assertEqual(uninstall.owned_automation("demo", {}), good.parent.resolve())
                self.assertIsNone(uninstall.owned_automation("other", {}))

    def test_project_apply_archives_only_target_and_preserves_other_binding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            remote_state = root / "remote"
            config_path = remote_state / "config.json"
            automations = root / "automations"
            automation = automations / "demo-inspection"
            automation.mkdir(parents=True)
            (automation / "automation.toml").write_text(
                'prompt = "run sync_feishu.py --project-key demo"\n', encoding="utf-8"
            )
            gateway_common.atomic_write_json(
                config_path,
                {
                    "projects": {
                        "demo": {"working_directory": str(root / "demo")},
                        "keep": {"working_directory": str(root / "keep")},
                    }
                },
            )
            gateway_common.atomic_write_json(
                remote_state / "projects" / "demo" / "state.json", {"messages": {}}
            )
            with (
                patch.object(uninstall.gateway_common, "CONFIG_PATH", config_path),
                patch.object(uninstall.gateway_common, "REMOTE_STATE", remote_state),
                patch.object(uninstall, "AUTOMATIONS_ROOT", automations),
                patch.object(uninstall.listener_control, "stop_listener", return_value={"ok": True}),
                patch.object(uninstall.listener_control, "start_listener", return_value={"ok": True}),
                patch.object(uninstall, "installed_skill_target", return_value=None),
            ):
                preview = uninstall.build_preview("project", "demo", False)
                result = uninstall.apply_uninstall(preview, False)
            remaining = gateway_common.load_json(config_path, {})["projects"]
            archived_exists = (Path(result["recovery_path"]) / "projects" / "demo" / "state.json").exists()
        self.assertEqual(result["actions"]["bindings_removed"], ["demo"])
        self.assertEqual(list(remaining), ["keep"])
        self.assertFalse(automation.exists())
        self.assertTrue(archived_exists)
        self.assertIsNone(result["remove_installed_skill_after_command"])

    def test_all_apply_archives_projects_and_removes_owned_lifecycle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            remote_state = root / "remote"
            config_path = remote_state / "config.json"
            gateway_common.atomic_write_json(
                config_path,
                {"projects": {"demo": {"working_directory": str(root / "demo")}}},
            )
            gateway_common.atomic_write_json(
                remote_state / "projects" / "demo" / "state.json", {"messages": {}}
            )
            with (
                patch.object(uninstall.gateway_common, "CONFIG_PATH", config_path),
                patch.object(uninstall.gateway_common, "REMOTE_STATE", remote_state),
                patch.object(uninstall, "AUTOMATIONS_ROOT", root / "automations"),
                patch.object(uninstall.listener_control, "stop_listener", return_value={"ok": True}),
                patch.object(uninstall.listener_control, "uninstall_listener", return_value={"ok": True, "removed": True}),
                patch.object(uninstall.install_startup_hook, "uninstall", return_value={"ok": True, "removed": True}),
                patch.object(uninstall, "installed_skill_target", return_value=None),
            ):
                preview = uninstall.build_preview("all", None, False)
                result = uninstall.apply_uninstall(preview, False)
            archived_exists = (Path(result["recovery_path"]) / "projects" / "demo" / "state.json").exists()
            config_exists = config_path.exists()
        self.assertTrue(archived_exists)
        self.assertFalse(config_exists)
        self.assertTrue(result["actions"]["hook"]["removed"])
        self.assertTrue(result["actions"]["launcher"]["removed"])
        self.assertFalse(result["feishu_content_deleted"])


class ReminderTests(unittest.TestCase):
    def test_continuous_reminder_is_hourly_until_completed(self):
        start = datetime(2026, 8, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as raw, patch.object(reminders, "REMOTE_STATE", Path(raw)):
            created = reminders.create("demo", "提交材料", start.isoformat(), "continuous")
            first_due = reminders.due_reminders("demo", start + timedelta(minutes=1))
            reminders.mark_sent("demo", created["reminder_id"], start + timedelta(minutes=1))
            same_hour = reminders.due_reminders("demo", start + timedelta(minutes=30))
            next_hour = reminders.due_reminders("demo", start + timedelta(hours=1, minutes=1))
            reminders.close("demo", created["reminder_id"], "completed")
            after_completion = reminders.due_reminders("demo", start + timedelta(hours=2))
        self.assertEqual(len(first_due), 1)
        self.assertEqual(same_hour, [])
        self.assertEqual(len(next_hour), 1)
        self.assertEqual(after_completion, [])

    def test_reminder_requires_explicit_timezone(self):
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            reminders.parse_start("2026-08-12T10:00:00")


if __name__ == "__main__":
    unittest.main()
