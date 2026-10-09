"""Real local WebSocket/protobuf transport tests; no cloud or model calls."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import functools
import gc
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import websockets
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

import gateway_transport as transport


sdk = transport.sdk


def frame(kind, *, method=None, payload=b"", message_id="fixture-message"):
    value = sdk.Frame()
    value.SeqID = 0
    value.LogID = 0
    value.method = sdk.FrameType.CONTROL.value if method is None else method
    value.service = 1
    value.payload = payload
    for key, item in ((sdk.HEADER_TYPE, kind), (sdk.HEADER_MESSAGE_ID, message_id),
                      (sdk.HEADER_TRACE_ID, "fixture-trace"), (sdk.HEADER_SUM, "1"),
                      (sdk.HEADER_SEQ, "0")):
        header = value.headers.add()
        header.key, header.value = key, item
    return value


def message_frame(message_id="fixture-message"):
    payload = {
        "schema": "2.0",
        "header": {"event_id": "event-" + message_id, "event_type": "im.message.receive_v1",
                   "create_time": "1791540000000", "tenant_key": "fixture-tenant",
                   "app_id": "fixture-app", "token": ""},
        "event": {
            "sender": {"sender_id": {"open_id": "fixture-user"}, "sender_type": "user",
                       "tenant_key": "fixture-tenant"},
            "message": {"message_id": message_id, "chat_id": "fixture-chat",
                        "create_time": "1791540000000", "message_type": "text",
                        "content": '{"text":"fixture-private-content"}'},
        },
    }
    return frame(sdk.MessageType.EVENT.value, method=sdk.FrameType.DATA.value,
                 payload=json.dumps(payload).encode(), message_id=message_id)


async def eventually(predicate, timeout=3):
    # A test-only deadline, not a production retry/health setting.
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout=timeout)


@contextmanager
def without_timeout_api():
    previous = getattr(asyncio, "timeout", None)
    if previous is not None:
        del asyncio.timeout
    try:
        yield
    finally:
        if previous is not None:
            asyncio.timeout = previous


class FunctionalTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.respond = True
        self.pong_gate = None
        self.pings = 0
        self.connections = []
        self.active_connections = set()
        self.max_active_connections = 0
        self.acks = asyncio.Queue()
        self.callbacks = []
        self.callback = lambda event: self.callbacks.append(event.event.message.message_id)
        self.clients = []
        self.released = []
        self.server = await websockets.serve(self.server_handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/?device_id=fixture-device&service_id=1&ticket=fixture-ticket"
        original_connect = websockets.connect

        @functools.wraps(original_connect)
        def local_connect(uri, **kwargs):
            return original_connect(uri, ping_timeout=0.3, ping_interval=None, **kwargs)

        self.connect_patch = patch.object(sdk.websockets, "connect", local_connect)
        self.connect_patch.start()

    async def asyncTearDown(self):
        for event in self.released:
            event.set()
        if self.pong_gate is not None:
            self.pong_gate.set()
        for client in self.clients:
            await client.close()
        self.server.close()
        await self.server.wait_closed()
        self.connect_patch.stop()

    async def server_handler(self, connection):
        self.connections.append(connection)
        self.active_connections.add(connection)
        self.max_active_connections = max(self.max_active_connections, len(self.active_connections))
        try:
            async for raw in connection:
                received = sdk.Frame()
                received.ParseFromString(raw)
                if received.method == sdk.FrameType.CONTROL.value:
                    self.pings += 1
                    if self.respond:
                        if self.pong_gate is not None:
                            await self.pong_gate.wait()
                        await connection.send(frame(sdk.MessageType.PONG.value).SerializeToString())
                else:
                    await self.acks.put((connection, json.loads(received.payload)))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.active_connections.discard(connection)

    async def make_client(self):
        dispatcher = (EventDispatcherHandler.builder("", "")
                      .register_p2_im_message_receive_v1(lambda event: self.callback(event)).build())
        client = transport.Client("fixture-app", "fixture-secret", event_handler=dispatcher,
                                  log_level=sdk.LogLevel.WARNING)
        # Server-supplied test configuration shortens only the local fixture.
        client._configure(sdk.ClientConfig({"ReconnectCount": -1, "ReconnectNonce": 0,
                                            "ReconnectInterval": 0.01, "PingInterval": 0.05}))
        client._get_conn_url = lambda: self.url
        self.clients.append(client)
        await client._connect()
        return client

    async def test_fresh_application_pong_and_concurrent_probes_coalesce(self):
        client = await self.make_client()
        self.assertFalse(client.inspect()["healthy"])
        self.pong_gate = asyncio.Event()
        probes = [asyncio.create_task(client.probe()) for _ in range(5)]
        await eventually(lambda: self.pings == 1)
        self.assertTrue(all(not task.done() for task in probes))
        self.pong_gate.set()
        results = await asyncio.gather(*probes)
        self.assertTrue(all(result["healthy"] for result in results))
        self.assertEqual(self.pings, 1)
        self.assertEqual(results[0]["verification"], "fresh-application-pong")
        first_pong = results[0]["pong_received_at"]
        next_result = await client.probe()
        self.assertGreater(next_result["pong_received_at"], first_pong)
        self.assertEqual(self.pings, 2)

    async def test_rfc_pong_cannot_hide_missing_application_pong_and_recovery_is_automatic(self):
        client = await self.make_client()
        original_generation = client.inspect()["generation"]
        self.respond = False
        rfc_waiter = await client._conn.ping()
        await asyncio.wait_for(rfc_waiter, 1)
        failed = await client.probe()
        self.assertFalse(failed["healthy"])
        self.assertEqual(failed["error"], "probe-TimeoutError")
        self.respond = True
        await eventually(lambda: client.inspect()["generation"] > original_generation)
        self.assertTrue((await client.probe())["healthy"])
        self.assertEqual(self.max_active_connections, 1)

    async def test_dead_receiver_is_detected_and_replaced_without_manual_probe(self):
        client = await self.make_client()
        old_receiver = client._receive_task
        old_generation = client.inspect()["generation"]
        old_receiver.cancel()
        await asyncio.gather(old_receiver, return_exceptions=True)
        await eventually(lambda: client.inspect()["generation"] > old_generation)
        self.assertIsNot(client._receive_task, old_receiver)
        self.assertTrue((await client.probe())["healthy"])
        await self.connections[-1].send(message_frame().SerializeToString())
        _, ack = await asyncio.wait_for(self.acks.get(), 1)
        self.assertEqual(ack["code"], 200)
        self.assertEqual(self.callbacks, ["fixture-message"])

    async def test_disconnect_reconnects_with_one_consumer_and_existing_callback(self):
        client = await self.make_client()
        reconnected = []
        client.on_reconnected = lambda: reconnected.append(client._generation)
        for index in range(3):
            generation = client._generation
            await self.connections[-1].close()
            await eventually(lambda: client._generation > generation)
            await self.connections[-1].send(message_frame(str(index)).SerializeToString())
            _, ack = await asyncio.wait_for(self.acks.get(), 1)
            self.assertEqual(ack["code"], 200)
        self.assertEqual(self.callbacks, ["0", "1", "2"])
        self.assertEqual(len(reconnected), 3)
        self.assertEqual(self.max_active_connections, 1)

    async def test_failing_reconnect_observers_cannot_block_transport_recovery(self):
        client = await self.make_client()
        calls = []

        def broken_observer(name):
            calls.append(name)
            raise RuntimeError("fixture-secret " + self.url)

        client.on_reconnecting = lambda: broken_observer("reconnecting")
        client.on_reconnected = lambda: broken_observer("reconnected")
        generation = client._generation
        await self.connections[-1].close()
        await eventually(lambda: client._generation > generation)
        self.assertTrue((await client.probe())["healthy"])
        self.assertEqual(calls, ["reconnecting", "reconnected"])
        self.assertIsNone(client._recovery_task)
        self.assertEqual(self.max_active_connections, 1)

    async def test_ack_waits_for_real_durable_callback_while_heartbeat_stays_responsive(self):
        entered, release = threading.Event(), threading.Event()
        self.released.append(release)
        with tempfile.TemporaryDirectory() as directory:
            durable = Path(directory) / "inbox.json"

            def persist(event):
                entered.set()
                release.wait()
                durable.write_text(json.dumps({"message_id": event.event.message.message_id}), encoding="utf-8")

            self.callback = persist
            client = await self.make_client()
            await self.connections[-1].send(message_frame().SerializeToString())
            await eventually(entered.is_set)
            self.assertTrue((await client.probe())["healthy"])
            self.assertTrue(self.acks.empty())
            self.assertFalse(durable.exists())
            release.set()
            _, ack = await asyncio.wait_for(self.acks.get(), 1)
            self.assertEqual(ack["code"], 200)
            self.assertEqual(json.loads(durable.read_text())["message_id"], "fixture-message")

    async def test_slow_old_callback_never_acks_on_new_connection(self):
        entered, release = threading.Event(), threading.Event()
        self.released.append(release)

        def callback(event):
            entered.set()
            release.wait()
            self.callbacks.append(event.event.message.message_id)

        self.callback = callback
        client = await self.make_client()
        await self.connections[-1].send(message_frame("old").SerializeToString())
        await eventually(entered.is_set)
        generation = client._generation
        await self.connections[-1].close()
        await eventually(lambda: client._generation > generation)
        release.set()
        await eventually(lambda: not client._message_tasks)
        self.assertTrue(self.acks.empty())
        self.assertEqual(self.callbacks, ["old"])
        self.assertTrue((await client.probe())["healthy"])

    async def test_periodic_sdk_ping_cadence_detects_failure_and_recovers(self):
        client = await self.make_client()
        generation = client._generation
        self.respond = False
        client._ping_task = asyncio.create_task(client._ping_loop())
        await eventually(lambda: client._generation > generation)
        self.respond = True
        await eventually(lambda: client.inspect()["healthy"])
        self.assertTrue(client._ping_task and not client._ping_task.done())

    async def test_repeated_connect_does_not_leak_lock_or_add_receiver(self):
        client = await self.make_client()
        receiver = client._receive_task
        await asyncio.wait_for(client._connect(), 1)
        self.assertFalse(client._lock.locked())
        self.assertIs(client._receive_task, receiver)
        self.assertEqual(len(self.connections), 1)
        self.assertTrue((await client.probe())["healthy"])

    async def test_old_generation_pong_cannot_change_health_or_server_settings(self):
        client = await self.make_client()
        self.assertTrue((await client.probe())["healthy"])
        old_connection, old_generation = client._conn, client._generation
        await self.connections[-1].close()
        await eventually(lambda: client._generation > old_generation)
        stale_config = json.dumps({"ReconnectCount": 999, "ReconnectNonce": 999,
                                   "ReconnectInterval": 999, "PingInterval": 999}).encode()
        stale = frame(sdk.MessageType.PONG.value, payload=stale_config).SerializeToString()
        await client._dispatch_message(stale, old_connection, old_generation)
        self.assertIsNone(client.inspect()["pong_received_at"])
        self.assertFalse(client.inspect()["healthy"])
        self.assertEqual(client._ping_interval, 0.05)
        self.assertEqual(client._reconnect_count, -1)
        self.assertTrue((await client.probe())["healthy"])

    async def test_cancelling_one_waiter_cannot_cancel_shared_health_repair(self):
        client = await self.make_client()
        generation = client._generation
        self.respond = False
        abandoned = asyncio.create_task(client.probe())
        await eventually(lambda: self.pings == 1)
        abandoned.cancel()
        await asyncio.gather(abandoned, return_exceptions=True)
        # The shared underlying task must still expire and schedule recovery.
        await eventually(lambda: client._generation > generation)
        self.respond = True
        self.assertTrue((await client.probe())["healthy"])

    async def test_without_timeout_api_real_pong_missing_pong_and_waiter_cancellation(self):
        # Simulate 3.10's missing API while exercising the actual socket and
        # protobuf path, including a caller abandoning its shielded probe.
        with without_timeout_api():
            client = await self.make_client()
            self.assertTrue((await client.probe())["healthy"])
            generation = client._generation
            old_pong = client.inspect()["pong_received_at"]
            old_pings = self.pings
            self.respond = False
            abandoned = asyncio.create_task(client.probe())
            await eventually(lambda: self.pings > old_pings)
            owner = client._probe_task
            abandoned.cancel()
            await asyncio.gather(abandoned, return_exceptions=True)
            self.assertFalse(owner.done())
            await eventually(lambda: client._generation > generation)
            self.assertFalse(owner.cancelled())
            self.assertEqual((await owner)["error"], "probe-TimeoutError")
            self.respond = True
            result = await client.probe()
            self.assertTrue(result["healthy"])
            self.assertGreater(result["pong_received_at"], old_pong)
            await self.connections[-1].send(message_frame("after-compat-repair").SerializeToString())
            self.assertEqual((await asyncio.wait_for(self.acks.get(), 1))[1]["code"], 200)
            self.assertEqual(self.callbacks, ["after-compat-repair"])
            self.assertEqual(self.max_active_connections, 1)

    async def test_without_timeout_api_ping_send_uses_the_same_pong_budget(self):
        with without_timeout_api():
            client = await self.make_client()
            generation = client._generation
            entered, cancelled = asyncio.Event(), asyncio.Event()
            original_write = client._write_message

            async def blocked_write(_data):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            client._write_message = blocked_write
            waiting = asyncio.create_task(client.probe())
            await asyncio.wait_for(entered.wait(), 1)
            result = await asyncio.wait_for(waiting, 1)
            self.assertEqual(result["error"], "probe-TimeoutError")
            self.assertTrue(cancelled.is_set())
            client._write_message = original_write
            await eventually(lambda: client._generation > generation)
            self.assertTrue((await client.probe())["healthy"])
            self.assertEqual(self.max_active_connections, 1)

    async def test_completed_pong_exchange_cannot_mark_a_new_connection_healthy(self):
        client = await self.make_client()
        generation = client._generation
        original_wait_for = asyncio.wait_for
        switched = False

        async def switch_after_exchange(awaitable, timeout):
            nonlocal switched
            result = await original_wait_for(awaitable, timeout)
            if asyncio.current_task() is client._probe_task and not switched:
                self.assertIsNotNone(client.inspect()["pong_received_at"])
                switched = True
                # Real PONG has completed the inner exchange. Create a new
                # real socket before wait_for gives control back to its owner.
                await client._disconnect()
                await client._connect()
            return result

        with without_timeout_api(), patch.object(asyncio, "wait_for", new=switch_after_exchange):
            result = await client.probe()
        self.assertTrue(switched)
        self.assertGreater(client._generation, generation)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["error"], "probe-ConnectionClosedException")
        self.assertIsNone(client.inspect()["pong_received_at"])
        self.assertEqual(client.inspect()["error"], "awaiting-application-pong")
        self.assertIsNone(client._recovery_task)
        self.assertTrue((await client.probe())["healthy"])
        self.assertEqual(self.max_active_connections, 1)

    async def test_disabled_probe_budget_does_not_leave_an_unawaited_exchange(self):
        client = await self.make_client()
        generation = client._generation
        client._conn.ping_timeout = None
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", RuntimeWarning)
            result = await client.probe()
            gc.collect()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["error"], "probe-RuntimeError")
        self.assertFalse(any("was never awaited" in str(warning.message) for warning in observed),
                         [str(warning.message) for warning in observed])
        await eventually(lambda: client._generation > generation)
        self.assertTrue((await client.probe())["healthy"])
        self.assertEqual(self.max_active_connections, 1)

    async def test_reconnect_does_not_interrupt_unrelated_long_business_thread(self):
        stop, started = threading.Event(), threading.Event()
        self.released.append(stop)
        business_state = {"thread_id": "persistent-thread", "pending": ["older"], "processing": ["active"]}
        original_state = json.dumps(business_state, sort_keys=True)

        def business():
            started.set()
            stop.wait()

        worker = threading.Thread(target=business, daemon=True)
        worker.start()
        await eventually(started.is_set)
        client = await self.make_client()
        generation = client._generation
        await self.connections[-1].close()
        await eventually(lambda: client._generation > generation)
        self.assertTrue((await client.probe())["healthy"])
        self.assertTrue(worker.is_alive())
        self.assertEqual(json.dumps(business_state, sort_keys=True), original_state)
        stop.set()
        worker.join(timeout=1)

    async def test_logs_exclude_auth_url_payload_and_exception_text(self):
        client = await self.make_client()
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        sdk.logger.addHandler(handler)
        try:
            def fail(_event):
                raise ValueError("fixture-secret " + self.url + " fixture-private-content")
            self.callback = fail
            await self.connections[-1].send(message_frame().SerializeToString())
            _, ack = await asyncio.wait_for(self.acks.get(), 1)
            self.assertEqual(ack["code"], 500)
            sdk.logger.warning(client._fmt_log("connection {} {}", self.url, ValueError("fixture-secret")))
        finally:
            sdk.logger.removeHandler(handler)
        logs = captured.getvalue()
        for secret in ("fixture-secret", "fixture-ticket", "fixture-private-content", self.url):
            self.assertNotIn(secret, logs)
        self.assertIn("ValueError", logs)


class IndependentCoreTests(unittest.TestCase):
    def test_three_file_core_imports_without_gateway_config_or_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "core_vendor" / "v1"
            package.mkdir(parents=True)
            for name in ("gateway_transport.py", "gateway_recovery.py", "gateway_primitives.py"):
                shutil.copyfile(Path(__file__).with_name(name), package / name)
            # No gateway_common or project configuration is packaged. Guard
            # imports and config/credential reads in a fresh interpreter too.
            script = r'''
import builtins, importlib.abc, importlib.util, pathlib, sys, types
class NoGateway(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'gateway_common', 'remote_gateway'}:
            raise AssertionError('Core imported project gateway code')
sys.meta_path.insert(0, NoGateway())
original_open = builtins.open
original_path_open = pathlib.Path.open
def guard(path):
    if isinstance(path, (str, bytes, pathlib.Path)):
        name = str(path).replace(chr(92), '/').rsplit('/', 1)[-1].casefold()
        if name in {'config.json', 'secret.bin', 'credentials.json', '.env'}:
            raise AssertionError('Core read project configuration or credentials')
def guarded_open(path, *args, **kwargs):
    guard(path)
    return original_open(path, *args, **kwargs)
def guarded_path_open(path, *args, **kwargs):
    guard(path)
    return original_path_open(path, *args, **kwargs)
builtins.open = guarded_open
pathlib.Path.open = guarded_path_open
core = pathlib.Path.cwd() / 'core_vendor' / 'v1'
def load_top_level(name):
    spec = importlib.util.spec_from_file_location(name, core / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
gateway_primitives = load_top_level('gateway_primitives')
gateway_recovery = load_top_level('gateway_recovery')
gateway_transport = load_top_level('gateway_transport')
assert gateway_transport.HTTP_TIMEOUT_SECONDS == 60
assert gateway_recovery.load_json is gateway_primitives.load_json
assert gateway_recovery.atomic_write_json is gateway_primitives.atomic_write_json
top_level_trap = types.ModuleType('gateway_primitives')
def reject_top_level(name):
    raise AssertionError('Vendor core used unrelated top-level primitive: ' + name)
top_level_trap.__getattr__ = reject_top_level
sys.modules['gateway_primitives'] = top_level_trap
from core_vendor.v1 import gateway_transport as vendor_transport
from core_vendor.v1 import gateway_recovery as vendor_recovery
from core_vendor.v1 import gateway_primitives as vendor_primitives
assert vendor_transport.HTTP_TIMEOUT_SECONDS == 60
assert vendor_recovery.load_json is vendor_primitives.load_json
assert vendor_recovery.atomic_write_json is vendor_primitives.atomic_write_json
assert vendor_primitives is not gateway_primitives
assert 'gateway_common' not in sys.modules and 'remote_gateway' not in sys.modules
print('Independent three-file core imported top-level and as vendor package without gateway configuration or credentials')
'''
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", script], cwd=root,
                capture_output=True, text=True, encoding="utf-8", check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={**os.environ, "LOCALAPPDATA": str(root / "data"),
                     "CODEX_HOME": str(root / "codex")},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Independent three-file core imported", result.stdout)
            self.assertFalse((root / "data").exists())
            self.assertFalse((root / "codex").exists())

    def test_legacy_common_reexports_preserve_atomic_io_and_failure_evidence(self):
        import gateway_common as common
        import gateway_primitives as primitives
        self.assertIs(common.load_json, primitives.load_json)
        self.assertIs(common.atomic_write_json, primitives.atomic_write_json)
        self.assertEqual(common.HTTP_TIMEOUT_SECONDS, primitives.HTTP_TIMEOUT_SECONDS)
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "nested" / "state.json"
            self.assertEqual(common.load_json(target, {"pending": []}), {"pending": []})
            common.atomic_write_json(target, {"pending": ["original"]})
            with patch.object(primitives.os, "replace", side_effect=OSError("synthetic replace failure")):
                with self.assertRaises(OSError):
                    common.atomic_write_json(target, {"pending": ["replacement"]})
            self.assertEqual(common.load_json(target), {"pending": ["original"]})
            self.assertEqual(list(target.parent.glob(".state.json.*")), [])


class EndpointDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_no_response_times_out_without_blocking_loop(self):
        accepted, release = threading.Event(), threading.Event()

        class SilentEndpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                accepted.set()
                release.wait()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SilentEndpoint)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = transport.Client("fixture-app", "fixture-secret",
                                  domain=f"http://127.0.0.1:{server.server_port}")
        try:
            with patch.object(transport, "HTTP_TIMEOUT_SECONDS", 0.15):
                connecting = asyncio.create_task(client._connect())
                await eventually(accepted.is_set)
                loop_turns = 0
                while not connecting.done():
                    await asyncio.sleep(0.01)
                    loop_turns += 1
                with self.assertRaises(sdk.requests.exceptions.Timeout):
                    await connecting
                self.assertGreater(loop_turns, 3)
                self.assertFalse(client._lock.locked())
                self.assertIsNone(client._conn)
        finally:
            release.set()
            await client.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=1)

    async def test_real_endpoint_timeout_retries_and_recovers_to_application_pong(self):
        release = threading.Event()
        attempts = []

        async def websocket_handler(connection):
            try:
                async for _raw in connection:
                    await connection.send(frame(sdk.MessageType.PONG.value).SerializeToString())
            except websockets.ConnectionClosed:
                pass

        ws_server = await websockets.serve(websocket_handler, "127.0.0.1", 0)
        ws_port = ws_server.sockets[0].getsockname()[1]
        ws_url = f"ws://127.0.0.1:{ws_port}/?device_id=fixture&service_id=1"
        config = {"ReconnectCount": -1, "ReconnectNonce": 0,
                  "ReconnectInterval": 0.01, "PingInterval": 0.05}

        class RecoveringEndpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                attempts.append(1)
                if len(attempts) == 1:
                    release.wait()
                    return
                response = json.dumps({"code": 0, "data": {"URL": ws_url, "ClientConfig": config}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        http_server = ThreadingHTTPServer(("127.0.0.1", 0), RecoveringEndpoint)
        http_server.daemon_threads = True
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        client = transport.Client("fixture-app", "fixture-secret",
                                  domain=f"http://127.0.0.1:{http_server.server_port}",
                                  log_level=sdk.LogLevel.WARNING)
        client._configure(sdk.ClientConfig(config))
        try:
            with patch.object(transport, "HTTP_TIMEOUT_SECONDS", 0.15):
                client._request_repair(None, 0, "fixture-start")
                await eventually(lambda: client._conn is not None)
                self.assertTrue((await client.probe())["healthy"])
                self.assertEqual(len(attempts), 2)
                self.assertEqual(client._ping_interval, config["PingInterval"])
                self.assertEqual(client._reconnect_interval, config["ReconnectInterval"])
        finally:
            release.set()
            await client.close()
            ws_server.close()
            await ws_server.wait_closed()
            await asyncio.to_thread(http_server.shutdown)
            http_server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
