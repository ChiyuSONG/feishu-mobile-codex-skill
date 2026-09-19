from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import remote_gateway


class ModelDefaultTests(unittest.TestCase):
    def test_default_is_explicit_before_resume_and_preserves_effort_and_tier(self):
        for model in (None, "", "   "):
            for effort in (None, "", "low", "medium", "high", "xhigh", "max", "ultra"):
                for tier in (None, "", "fast", "priority"):
                    for thread_id in ("", "existing-thread"):
                        with self.subTest(model=model, effort=effort, tier=tier, thread_id=thread_id):
                            project = {"working_directory": str(Path.cwd())}
                            for key, value in (("agent_model", model), ("agent_reasoning_effort", effort), ("agent_service_tier", tier)):
                                if value is not None:
                                    project[key] = value
                            original = copy.deepcopy(project)
                            with patch.object(remote_gateway, "codex_cli_path", return_value=Path("codex.exe")):
                                command = remote_gateway.build_codex_command(project, Path("final.md"), [], thread_id)
                            self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
                            if thread_id:
                                self.assertLess(command.index("--model"), command.index("resume"))
                            self.assertEqual(any(value.startswith("model_reasoning_effort=") for value in command), bool(effort))
                            self.assertEqual(any(value.startswith("service_tier=") for value in command), bool(tier))
                            if effort:
                                self.assertIn("model_reasoning_effort=" + json.dumps(effort), command)
                            if tier:
                                self.assertIn("service_tier=" + json.dumps(tier), command)
                            self.assertEqual("features.fast_mode=true" in command, tier in {"fast", "priority"})
                            self.assertEqual(project, original)

    def test_explicit_model_still_overrides_default(self):
        with patch.object(remote_gateway, "codex_cli_path", return_value=Path("codex.exe")):
            command = remote_gateway.build_codex_command(
                {"working_directory": str(Path.cwd()), "agent_model": "gpt-5.6-sol"},
                Path("final.md"), [], "existing-thread",
            )
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")

    def test_prompt_displays_effective_default(self):
        prompt = remote_gateway.build_prompt(
            "test",
            {"working_directory": str(Path.cwd())},
            {"message_id": "model-default-test", "content": '{"text":"inspect"}'},
            [],
            first_turn=False,
        )
        self.assertIn("远程 Codex 模型: gpt-6-astra", prompt)

    def test_init_persists_default_and_preserves_existing_profile(self):
        profiles = [
            {},
            {"agent_model": "", "agent_reasoning_effort": "ultra", "agent_service_tier": "fast"},
            {"agent_model": "", "agent_reasoning_effort": "", "agent_service_tier": ""},
            {"agent_model": "gpt-5.6-sol", "agent_reasoning_effort": "high", "agent_service_tier": "priority"},
        ]
        for original in profiles:
            with self.subTest(profile=original), tempfile.TemporaryDirectory() as raw:
                target = Path(raw) / "config.json"
                config = {"projects": {"test": copy.deepcopy(original)}}
                args = SimpleNamespace(
                    project_key="test", working_directory=raw, workspace_mode="project",
                    language="zh-CN", disable_hourly_catch_up=True,
                    disable_hourly_progress_reports=True, chat_name="test", chat_id="",
                    focus="", bootstrap_source_thread_id="", permission_mode="full-access",
                )
                with (
                    patch.object(remote_gateway, "CONFIG_PATH", target),
                    patch.object(remote_gateway, "load_config", return_value=config),
                    patch.object(remote_gateway, "load_json", return_value={}),
                    patch.object(remote_gateway, "source_task_profile", return_value={
                        "model": "gpt-6-astra", "reasoning_effort": "high",
                        "service_tier": "priority", "permission_mode": "full-access"}),
                    patch.dict(remote_gateway.os.environ, {"LOCALAPPDATA": raw}),
                ):
                    remote_gateway.init_project(args)
                actual = json.loads(target.read_text(encoding="utf-8"))["projects"]["test"]
                self.assertEqual(actual["agent_model"], original.get("agent_model") or "gpt-6-astra")
                for key in ("agent_reasoning_effort", "agent_service_tier"):
                    self.assertEqual(actual[key], original.get(key) or {
                        "agent_reasoning_effort": "high", "agent_service_tier": "priority"}[key])
                self.assertEqual(actual["patrol_model"], "gpt-5.6-terra")
                self.assertEqual(actual["patrol_service_tier"], "default")

    def test_profile_change_without_tier_argument_preserves_priority_switch(self):
        for tier in (None, "", "fast", "priority"):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as raw:
                project = {"working_directory": raw, "agent_reasoning_effort": "ultra"}
                if tier is not None:
                    project["agent_service_tier"] = tier
                config = {"app_id": "test", "expected_tenant_key": "test", "projects": {"test": project}}
                target = Path(raw) / "config.json"
                with (
                    patch.object(remote_gateway, "CONFIG_PATH", target),
                    patch.object(remote_gateway, "load_config", return_value=config),
                ):
                    remote_gateway.set_project_profile(SimpleNamespace(
                        project_key="test", model="gpt-6-astra",
                        reasoning_effort="ultra", service_tier=None,
                    ))
                actual = json.loads(target.read_text(encoding="utf-8"))["projects"]["test"]
                self.assertEqual(actual["agent_reasoning_effort"], "ultra")
                self.assertEqual("agent_service_tier" in actual, tier is not None)
                self.assertEqual(actual.get("agent_service_tier"), tier)


if __name__ == "__main__":
    unittest.main()
