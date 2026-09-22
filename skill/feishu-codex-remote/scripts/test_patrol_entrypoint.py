"""Contract tests of the actual PS wrapper, with no live task or Feishu writes.

Only Task Scheduler commands and the remote gateway are replaced by fixtures.
The unmodified production wrapper executes in real Windows PowerShell.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent
PS = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


@unittest.skipUnless(os.name == 'nt', 'Windows wrapper integration')
class PatrolEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.TemporaryDirectory(prefix="isolated patrol runtime ")
        cls.addClassCleanup(cls.runtime.cleanup)
        venv = Path(cls.runtime.name) / "CodexFeishuRemote/.venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                       check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)

    def run_wrapper(self, state='Running', triggers='', args=(), exitcode=0, deny=False):
        with tempfile.TemporaryDirectory(prefix='patrol fixture 中文 ') as tmp:
            root = Path(tmp)
            shutil.copy2(SCRIPTS / 'sync_feishu.ps1', root / 'sync_feishu.ps1')
            # All evidence and fake child output are confined to this temporary directory.
            (root / 'remote_gateway.py').write_text(
                'import json,sys\nfrom pathlib import Path\n'
                'p=Path(__file__).with_name("child.json")\n'
                'p.write_text(json.dumps(sys.argv[1:]),encoding="utf-8")\n'
                'print(json.dumps({"ok":True,"status":"accepted"}))\n'
                f'sys.exit({exitcode})\n', encoding='utf-8')
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            runner = (
                "$ErrorActionPreference='Stop'\n"
                f"$global:fixtureState={quote(state)}\n"
                f"$global:fixtureLog={quote(root / 'calls.txt')}\n"
                "function Get-ScheduledTask { param($TaskName,$ErrorAction)\n"
                " Add-Content -LiteralPath $global:fixtureLog -Value ('get:'+$TaskName)\n"
                + (" throw 'fixture access denied'\n" if deny else
                   " [pscustomobject]@{State=$global:fixtureState}\n") +
                "}\nfunction Export-ScheduledTask { param($TaskName)\n"
                f" {quote('<Task><Triggers>' + triggers + '</Triggers></Task>')}\n"
                "}\nfunction Start-ScheduledTask { param($TaskName)\n"
                " Add-Content -LiteralPath $global:fixtureLog -Value ('start:'+$TaskName)\n"
                " $global:fixtureState='Running'\n}\n"
                f"& {quote(root / 'sync_feishu.ps1')} -ProjectKey 'fixture-project' "
                + ' '.join(args) + '\nexit $LASTEXITCODE\n'
            )
            (root / 'runner.ps1').write_text(runner, encoding='utf-8-sig')
            proc = subprocess.run([str(PS), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                                   'Bypass', '-File', str(root / 'runner.ps1')],
                                  capture_output=True, encoding='utf-8', errors='replace',
                                  env={**os.environ, 'LOCALAPPDATA': self.runtime.name},
                                  creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
            calls = (root / 'calls.txt').read_text().splitlines() if (root / 'calls.txt').exists() else []
            child = json.loads((root / 'child.json').read_text()) if (root / 'child.json').exists() else None
            return proc, calls, child

    def test_running_only_reconciles_without_another_start(self):
        proc, calls, args = self.run_wrapper(args=('-RequestOnly',))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, ['get:CodexFeishuRemoteGateway'])
        self.assertEqual(args, ['sync','--project-key','fixture-project','--wait-timeout','3600','--request-only'])
        self.assertEqual(json.loads(proc.stdout)['status'], 'accepted')

    def test_stopped_starts_existing_task_then_reconciles(self):
        proc, calls, args = self.run_wrapper(state='Ready', args=('-RequestOnly',))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, ['get:CodexFeishuRemoteGateway','start:CodexFeishuRemoteGateway','get:CodexFeishuRemoteGateway'])
        self.assertIn('--request-only', args)

    def test_permission_denial_is_not_bypassed(self):
        proc, calls, args = self.run_wrapper(deny=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('fixture access denied', proc.stderr)
        self.assertEqual(calls, ['get:CodexFeishuRemoteGateway'])
        self.assertIsNone(args)

    def test_automatic_triggers_are_rejected_before_start_or_sync(self):
        for trigger in ('<BootTrigger/>','<LogonTrigger/>','<CalendarTrigger/>'):
            with self.subTest(trigger=trigger):
                proc, calls, args = self.run_wrapper(state='Ready', triggers=trigger)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn('demand-start-only', proc.stderr)
                self.assertEqual(calls, ['get:CodexFeishuRemoteGateway'])
                self.assertIsNone(args)

    def test_legacy_usage_request_is_always_nonblocking(self):
        proc, _, args = self.run_wrapper(args=('-ReportUsage',))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(args[-2:], ['--request-only','--report-usage'])

    def test_manual_sync_keeps_drain_mode_and_timeout(self):
        proc, _, args = self.run_wrapper(args=('-WaitTimeoutSeconds','17'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(args[-2:], ['--wait-timeout','17'])
        self.assertNotIn('--request-only', args)

    def test_gateway_failure_exit_is_preserved(self):
        proc, _, args = self.run_wrapper(args=('-RequestOnly',), exitcode=7)
        self.assertEqual(proc.returncode, 7)
        self.assertIsNotNone(args)

    def test_inspection_report_retains_nonblocking_and_report_mode(self):
        proc, _, args = self.run_wrapper(args=('-InspectionReport',))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(args[-2:], ['--request-only', '--inspection-report'])

    def test_first_inspection_flag_is_preserved(self):
        proc, _, args = self.run_wrapper(args=('-RequestOnly', '-FirstInspectionMessage'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(args[-2:], ['--request-only', '--first-inspection-message'])


if __name__ == '__main__':
    unittest.main()
