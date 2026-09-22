"""Windowless lifecycle and owned-launcher migration, without live state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_startup_hook as hooks
import listener_control as control
import reload_gateway
import supervisor
import sync_feishu
import bootstrap


class HiddenChildTests(unittest.TestCase):
    def test_runtime_setup_hides_both_creation_and_dependency_install(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(bootstrap, 'REMOTE_STATE', Path(raw)), patch.object(bootstrap, 'runtime_python', return_value=Path(raw) / 'missing-python'), patch.object(bootstrap, 'preflight', return_value={'platform_supported': True, 'python_supported': True}), patch.object(subprocess, 'run') as run:
            self.assertTrue(bootstrap.install()['ok'])
            self.assertEqual(run.call_count, 2)
            if os.name == 'nt':
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_control_windows_children_are_hidden(self):
        with patch.object(control.os, 'name', 'nt'), patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), patch.object(subprocess, 'run') as run:
            control._run(['schtasks.exe', '/Run', '/TN', control.WINDOWS_TASK])
            self.assertEqual(run.call_args.kwargs['creationflags'], 0x08000000)
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_control_other_platforms_keep_portable_launch(self):
        with patch.object(control.os, 'name', 'posix'), patch.object(subprocess, 'run') as run:
            control._run(['launchctl', 'print', 'service'])
            self.assertNotIn('creationflags', run.call_args.kwargs)

    def test_windowless_hook_keeps_unrelated_hooks_and_uninstall(self):
        original = {'hooks': {'SessionStart': [{'hooks': [{'command': 'unrelated'}]}]}}
        result = hooks.merged_config(original, 'Windows')
        self.assertIn('pythonw.exe', result['hooks']['SessionStart'][1]['hooks'][0]['command'])
        self.assertEqual(result, hooks.merged_config(result, 'Windows'))
        remaining, removed = hooks.without_ours(result)
        self.assertTrue(removed)
        self.assertEqual(remaining, original)

    def test_supervisor_preserves_diagnostics_and_child_exit(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(supervisor, 'REMOTE_STATE', Path(raw)), patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(supervisor.run_once(), 7)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stderr'], subprocess.STDOUT)
            self.assertTrue((Path(raw) / 'logs/gateway-console.log').exists())
            if os.name == 'nt':
                self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
                self.assertTrue(run.call_args.args[0][0].endswith('python.exe'))

    def test_sync_keeps_report_modes_and_failure(self):
        with patch.object(sys, 'argv', ['sync', '--project-key', 'fixture', '--inspection-report']), patch.object(sync_feishu, 'start_listener') as start, patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(sync_feishu.main(), 7)
            start.assert_called_once()
            self.assertIn('--inspection-report', run.call_args.args[0])
            self.assertIn('--request-only', run.call_args.args[0])
            if os.name == 'nt':
                self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_reload_keeps_graceful_command_and_failure(self):
        with patch.object(sys, 'argv', ['reload', '--request-only']), patch.object(reload_gateway, 'start_listener') as start, patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(reload_gateway.main(), 7)
            start.assert_called_once()
            self.assertIn('reload', run.call_args.args[0])
            self.assertIn('--request-only', run.call_args.args[0])
            if os.name == 'nt':
                self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_access_denial_is_not_reported_as_success(self):
        with patch.object(control.platform, 'system', return_value='Windows'), patch.object(control, '_run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')):
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                control.start_listener()


@unittest.skipUnless(os.name == 'nt', 'Real Windows PowerShell migration fixtures')
class ScheduledLauncherTests(unittest.TestCase):
    def invoke(self, action, uninstall=False):
        source = Path(__file__).resolve().parent
        ps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        with tempfile.TemporaryDirectory(prefix='launch fixture 中文 ') as raw:
            root = Path(raw)
            for name in ('install_listener_task.ps1', 'uninstall_listener_task.ps1'):
                shutil.copy2(source / name, root / name)
            (root / 'supervisor.py').touch()
            pythonw = root / 'CodexFeishuRemote/.venv/Scripts/pythonw.exe'
            pythonw.parent.mkdir(parents=True)
            pythonw.touch()
            runner = root / 'run_gateway.ps1'
            supervisor_path = root / 'supervisor.py'
            expected_args = f'"{supervisor_path}"'
            if action == 'legacy':
                executable, arguments = str(ps), f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{runner}"'
            elif action == 'silent':
                executable, arguments = str(pythonw), expected_args
            else:
                executable, arguments = str(ps), f'-File "{runner}.foreign"'
            task = {'State': 'Running', 'Actions': [{'Execute': executable, 'Arguments': arguments}]}
            (root / 'task.json').write_text(json.dumps(task), encoding='utf-8')
            q = lambda value: "'" + str(value).replace("'", "''") + "'"
            fixture = """
$ErrorActionPreference='Stop'
$global:fixtureTask = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'task.json') -Raw | ConvertFrom-Json
$global:fixtureChanged=$false
$global:fixtureStopped=$false
$global:fixtureRemoved=$false
function Get-ScheduledTask { param($TaskName,$ErrorAction) $global:fixtureTask }
function Export-ScheduledTask { param($TaskName)
    if ($global:fixtureChanged) { return $global:fixtureXml }
    $c=[System.Security.SecurityElement]::Escape($global:fixtureTask.Actions[0].Execute)
    $a=[System.Security.SecurityElement]::Escape($global:fixtureTask.Actions[0].Arguments)
    '<Task><Triggers><LogonTrigger/></Triggers><Actions><Exec><Command>'+ $c +'</Command><Arguments>'+ $a +'</Arguments></Exec></Actions><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings></Task>'
}
function Register-ScheduledTask { param($TaskName,$Xml,[switch]$Force)
    $global:fixtureChanged=$true
    $global:fixtureXml=$Xml
}
function Stop-ScheduledTask { param($TaskName,$ErrorAction) $global:fixtureStopped=$true }
function Unregister-ScheduledTask { param($TaskName,$Confirm,$ErrorAction) $global:fixtureRemoved=$true }
try {
"""
            script = root / ('uninstall_listener_task.ps1' if uninstall else 'install_listener_task.ps1')
            fixture += '& ' + q(script) + (' -ExpectedRunner ' + q(runner) if uninstall else '') + '\n'
            fixture += """
} finally {
    @{changed=$global:fixtureChanged;stopped=$global:fixtureStopped;removed=$global:fixtureRemoved;xml=$global:fixtureXml} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'result.json') -Encoding UTF8
}
"""
            (root / 'fixture.ps1').write_text(fixture, encoding='utf-8-sig')
            proc = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(root / 'fixture.ps1')], env={**os.environ, 'LOCALAPPDATA': str(root)}, capture_output=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
            result = json.loads((root / 'result.json').read_text(encoding='utf-8-sig'))
            return proc, result, str(pythonw), expected_args

    def test_legacy_and_silent_migration_preserve_running_instance(self):
        import xml.etree.ElementTree as xml
        for action in ('legacy', 'silent'):
            with self.subTest(action=action):
                proc, result, pythonw, arguments = self.invoke(action)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(result['stopped'])
                tree = xml.fromstring(result['xml'])
                self.assertEqual(tree.findtext('Actions/Exec/Command'), pythonw)
                self.assertEqual(tree.findtext('Actions/Exec/Arguments'), arguments)
                self.assertEqual(len(tree.find('Triggers')), 0)
                self.assertEqual(tree.findtext('Settings/MultipleInstancesPolicy'), 'IgnoreNew')

    def test_foreign_action_is_not_replaced(self):
        proc, result, _, _ = self.invoke('foreign')
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(result['changed'])
        self.assertFalse(result['stopped'])

    def test_uninstall_accepts_only_both_exact_owned_actions(self):
        for action in ('legacy', 'silent', 'foreign'):
            with self.subTest(action=action):
                proc, result, _, _ = self.invoke(action, uninstall=True)
                self.assertEqual(proc.returncode == 0, action != 'foreign', proc.stderr)
                self.assertEqual(result['removed'], action != 'foreign')
                self.assertEqual(result['stopped'], action != 'foreign')
