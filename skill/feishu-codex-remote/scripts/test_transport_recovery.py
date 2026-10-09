"""Listener transport recovery without live Feishu or model calls."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import contextmanager

import remote_gateway as gateway


class ReconnectRecoveryTests(unittest.TestCase):
    def service(self, root):
        service = gateway.GatewayService.__new__(gateway.GatewayService)
        service.log_path = root / "gateway.log"
        service._recovery = gateway.CatchupRecovery(root / "catchup-recovery.json")
        service.reconnect_signal = threading.Event()
        service.config = {"expected_tenant_key": "fixture"}
        service.workers = {}
        return service

    def test_callback_is_nonblocking_and_requests_history_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            service.catch_up = Mock()
            service.on_ws_reconnecting()
            service.on_ws_reconnected()
            self.assertTrue(service.reconnect_signal.is_set())
            service.catch_up.assert_not_called()
            self.assertIn("catch-up requested", service.log_path.read_text())

    def test_reconnect_recovers_opted_out_project_without_duplicate_or_thread_change(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gateway, "REMOTE_STATE", Path(temp)):
            root = Path(temp)
            service = self.service(root)
            source = [
                {"message_id": "one", "chat_id": "fixture-chat", "msg_type": "text",
                 "create_time": "1791538731427", "body": {"content": '{"text":"fixture question"}'},
                 "sender": {"sender_type": "user", "id": "fixture-user"}},
                {"message_id": "two", "chat_id": "fixture-chat", "msg_type": "text",
                 "create_time": "1791538733707", "body": {"content": '{"text":"*"}'},
                 "sender": {"sender_type": "user", "id": "fixture-user"}},
            ]
            service.client = SimpleNamespace(list_messages=Mock(return_value=source))
            project = {"chat_id": "fixture-chat", "registered_epoch": 1791530000,
                       "hourly_catch_up_enabled": False, "working_directory": str(root)}
            worker = gateway.ProjectWorker("fixture-project", project, service.client, service.log_path)
            worker.store.set_thread_id("original-thread")
            service.workers = {"fixture-project": worker}
            self.assertEqual(gateway.hourly_catch_up_project_keys(service.workers), [])
            self.assertEqual(service.catch_up(), {"fixture-project": 2})
            self.assertEqual(service.catch_up(), {"fixture-project": 0})
            self.assertEqual(worker.store.thread_id(), "original-thread")
            self.assertEqual(set(worker.store.state["messages"]), {"one", "two"})
            self.assertTrue(all(m["status"] == "pending" for m in worker.store.state["messages"].values()))
            self.assertTrue(worker.signal.is_set())

    def test_failed_reconnect_reconciliation_remains_observable_and_worker_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            service.catch_up = Mock(side_effect=[RuntimeError("fixture offline"), {}])
            service.workers = {"fixture": Mock()}
            service.recovery().request(["fixture"])
            service.reconnect_signal = Mock()
            service.reconnect_signal.wait.side_effect = [True, True, StopIteration]
            with self.assertRaises(StopIteration):
                service.reconcile_reconnects()
            self.assertEqual(service.catch_up.call_count, 2)
            self.assertIn("reconnect catch-up failed", service.log_path.read_text())

    def test_actual_run_wires_supported_sdk_callbacks(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            service.start_workers = Mock()
            service.catch_up = Mock()
            ws_client = Mock()
            ws_client.start.side_effect = StopIteration
            with (patch.object(gateway, "load_app_credentials", return_value=("fixture-app", "fixture-secret")),
                  patch.object(gateway.threading, "Thread"),
                  patch("gateway_transport.Client", return_value=ws_client)):
                with self.assertRaises(StopIteration):
                    service.run()
            self.assertEqual(ws_client.on_reconnected, service.on_ws_reconnected)
            self.assertEqual(ws_client.on_reconnecting, service.on_ws_reconnecting)

    def test_duplicate_run_never_reads_credentials_or_starts_workers(self):
        @contextmanager
        def busy():
            yield False
        with tempfile.TemporaryDirectory() as temp:
            with (patch.object(gateway, "REMOTE_STATE", Path(temp)),
                  patch.object(gateway, "listener_lease", busy),
                  patch.object(gateway, "load_config") as config,
                  patch.object(gateway, "GatewayService") as service):
                self.assertEqual(gateway.main(["run"]), 0)
                config.assert_not_called()
                service.assert_not_called()


class SharedConnectionCheckTests(unittest.TestCase):
    def test_disconnected_socket_is_not_healthy_even_when_loop_is_running(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gateway, "REMOTE_STATE", Path(temp)):
            service = gateway.GatewayService.__new__(gateway.GatewayService)
            service.log_path = Path(temp)/"log"
            service.ws_client = SimpleNamespace(_conn=None)
            service.ws_loop = SimpleNamespace(is_running=lambda: True)
            status = service.transport_status()
            self.assertFalse(status["connected"])
            self.assertTrue(status["event_loop_running"])

    def test_open_socket_requires_a_running_sdk_loop(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gateway, "REMOTE_STATE", Path(temp)):
            service = gateway.GatewayService.__new__(gateway.GatewayService)
            service.log_path = Path(temp)/"log"
            service.ws_client = SimpleNamespace(_conn=SimpleNamespace(state=SimpleNamespace(name="OPEN")))
            for running in (False, True):
                service.ws_loop = SimpleNamespace(is_running=lambda: running)
                # OPEN + a running loop is insufficient without a fresh PONG.
                self.assertFalse(service.transport_status()["connected"])

    def test_history_success_with_dead_realtime_never_acknowledges_healthy(self):
        import json
        with tempfile.TemporaryDirectory() as temp, patch.object(gateway, "REMOTE_STATE", Path(temp)):
            root = Path(temp)
            service = gateway.GatewayService.__new__(gateway.GatewayService)
            service.log_path = root/"log"
            service.workers = {"fixture": SimpleNamespace(signal=threading.Event())}
            service.catch_up = Mock(return_value={"fixture": 2})
            service.ws_client = SimpleNamespace(_conn=None)
            service.ws_loop = SimpleNamespace(is_running=lambda: True)
            requests, responses = root/"requests", root/"responses"
            gateway.atomic_write_json(requests/"req.json", {"project_keys": ["fixture"]})
            with patch.object(gateway, "SYNC_REQUESTS", requests), patch.object(gateway, "SYNC_RESPONSES", responses), patch.object(gateway.time, "sleep", side_effect=InterruptedError):
                with self.assertRaises(InterruptedError):
                    service.process_sync_requests()
            response = json.loads((responses/"req.json").read_text())
            self.assertFalse(response["ok"])
            self.assertEqual(response["added"], {"fixture": 2})
            self.assertFalse(response["transport"]["connected"])
            self.assertIn("realtime", response["error"])
