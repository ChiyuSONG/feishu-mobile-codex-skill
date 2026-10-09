import subprocess
import sys
import json
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import listener_control
import install_startup_hook
import supervisor



class ListenerControlTests(unittest.TestCase):
    def test_windows_start_uses_existing_demand_task(self):
        with patch.object(listener_control.platform, 'system', return_value='Windows'), patch.object(listener_control, '_run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, patch.object(listener_control, 'request_startup_reconciliation', return_value={'status':'queued'}) as request:
            self.assertTrue(listener_control.start_listener()['ok'])
            run.assert_called_once_with(['schtasks.exe', '/Run', '/TN', 'CodexFeishuRemoteGateway'])
            request.assert_called_once_with()

    def test_session_start_queues_all_bound_chats_including_hourly_opt_out(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(listener_control, "REMOTE_STATE", Path(temp)), patch.object(listener_control, "load_config", return_value={"projects": {
            "patrol": {"chat_id": "fixture-a", "hourly_catch_up_enabled": True},
            "short-questions": {"chat_id": "fixture-b", "hourly_catch_up_enabled": False},
            "inactive": {"chat_id": ""},
        }}):
            result = listener_control.request_startup_reconciliation()
            request = json.loads((Path(temp)/"sync_requests"/(result["request_id"]+".json")).read_text())
            self.assertEqual(result["status"], "queued")
            self.assertEqual(request["project_keys"], ["patrol", "short-questions"])
            self.assertEqual(request["source"], "session-start")
            self.assertNotIn("ok", result)

    def test_session_start_without_registered_chats_does_not_write_a_request(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(listener_control, "REMOTE_STATE", Path(temp)), patch.object(listener_control, "load_config", return_value={}):
            self.assertEqual(listener_control.request_startup_reconciliation()["status"], "no-registered-chats")
            self.assertEqual(list(Path(temp).iterdir()), [])

class ListenerLeaseTests(unittest.TestCase):
    def test_duplicate_is_excluded_and_exception_releases_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "listener.lock"
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with listener_control.listener_lease(path) as first:
                    self.assertTrue(first)
                    with listener_control.listener_lease(path) as second:
                        self.assertFalse(second)
                    raise RuntimeError("fixture")
            with listener_control.listener_lease(path) as recovered:
                self.assertTrue(recovered)

    def test_native_lock_excludes_another_hidden_process(self):
        with tempfile.TemporaryDirectory(prefix="listener lease 中文 ") as temp:
            path = Path(temp) / "listener.lock"
            program = (
                "import sys,json;from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from listener_control import listener_lease\n"
                "with listener_lease(Path(sys.argv[2])) as acquired: print(json.dumps(acquired))"
            )
            def probe():
                opts = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", "-c", program, str(Path(listener_control.__file__).parent), str(path)],
                    capture_output=True, text=True, encoding="utf-8", timeout=30, check=True, **opts)
                return json.loads(result.stdout)
            with listener_control.listener_lease(path) as first:
                self.assertTrue(first)
                self.assertFalse(probe())
            self.assertTrue(probe())
