"""Automatic network-flap recovery using local HTTP/WebSocket/protobuf only.

DNS faults are injected only for synthetic fixture hostnames. No machine DNS,
proxy, VPN, credentials, cloud endpoint, model or production setting is changed.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import websockets
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

import gateway_transport as transport
import remote_gateway as gateway
from test_functional_transport import eventually, frame, message_frame

sdk = transport.sdk


class FixtureStopped(Exception):
    pass


class StoppableSignal:
    """Only makes the real permanent recovery loop joinable during teardown."""

    def __init__(self):
        self.event = threading.Event()
        self.stopped = False

    def set(self):
        self.event.set()

    def clear(self):
        self.event.clear()

    def wait(self, timeout=None):
        result = self.event.wait(timeout)
        if self.stopped:
            raise FixtureStopped()
        return result


class NetworkFlapRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.stack.enter_context(patch.object(gateway, "REMOTE_STATE", self.root))
        # Requests/websockets must never route these fixture hosts to a real proxy.
        self.stack.enter_context(patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}))
        self.mode = "online"
        self.history_online = True
        self.dns_failures = self.endpoint_failures = self.history_failures = 0
        self.connections, self.active_connections, self.callbacks = [], set(), []
        self.max_connections = 0
        self.pings = 0
        self.acks = asyncio.Queue()
        self.runner_errors, self.recovery_errors = [], []
        self.stop_runner, self.stop_business = threading.Event(), threading.Event()
        self.runner = self.recovery_thread = self.business = None
        self.client = None
        self.server = await websockets.serve(self.server_handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.ws_url = f"ws://fixture-ws.invalid:{port}/?device_id=fixture&service_id=1"
        self.config = {"ReconnectCount": -1, "ReconnectNonce": 0,
                       "ReconnectInterval": 0.03, "PingInterval": 0.04}
        fixture = self

        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if fixture.mode == "endpoint":
                    fixture.endpoint_failures += 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"code": 0, "data": {
                    "URL": fixture.ws_url, "ClientConfig": fixture.config}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        self.http.daemon_threads = True
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        original_lookup = socket.getaddrinfo

        def fixture_lookup(host, *args, **kwargs):
            if host in {"fixture-endpoint.invalid", "fixture-ws.invalid"}:
                if self.mode == "dns":
                    self.dns_failures += 1
                    raise socket.gaierror(11001, "synthetic DNS temporarily unavailable")
                host = "127.0.0.1"
            return original_lookup(host, *args, **kwargs)

        self.stack.enter_context(patch.object(socket, "getaddrinfo", fixture_lookup))
        original_connect = websockets.connect

        @functools.wraps(original_connect)
        def local_connect(uri, **kwargs):
            return original_connect(uri, ping_timeout=0.2, ping_interval=None, **kwargs)

        self.stack.enter_context(patch.object(sdk.websockets, "connect", local_connect))
        self.loop = asyncio.new_event_loop()
        self.stack.enter_context(patch.object(sdk, "loop", self.loop))

        async def fixture_idle():
            while not self.stop_runner.is_set():
                await asyncio.sleep(0.01)

        self.stack.enter_context(patch.object(sdk, "_select", fixture_idle))
        self.service = gateway.GatewayService.__new__(gateway.GatewayService)
        self.service.log_path = self.root / "gateway.log"
        self.service.config = {"expected_tenant_key": "fixture-tenant"}
        self.service.reconnect_signal = StoppableSignal()
        self.service._recovery = gateway.CatchupRecovery(self.root / "recovery.json")
        self.service.workers, self.service.chat_to_key = {}, {}
        self.service.client = SimpleNamespace(list_messages=self.history)
        for key, chat in (("first", "fixture-chat"), ("second", "fixture-second")):
            project = {"working_directory": str(self.root), "chat_id": chat,
                       "registered_epoch": 1791530000, "hourly_catch_up_enabled": False}
            worker = gateway.ProjectWorker(key, project, self.service.client, self.service.log_path)
            worker.store.set_thread_id("original-" + key)
            self.service.workers[key] = worker
            self.service.chat_to_key[chat] = key
        self.original_workers = dict(self.service.workers)
        self.business = threading.Thread(target=self.stop_business.wait, daemon=True)
        self.business.start()
        first = self.service.workers["first"]
        first._active_main_thread = self.business
        first.store.state["messages"]["already-active"] = {
            "message_id": "already-active", "status": "processing", "attempts": 1,
            "content": {"text": "synthetic long work"}, "create_time": "1791530000000"}
        first.store.save()

    def history(self, chat, _start):
        if not self.history_online or self.mode != "online":
            self.history_failures += 1
            raise socket.gaierror(11001, "synthetic history DNS unavailable")
        return [{"message_id": "offline-" + chat, "chat_id": chat, "msg_type": "text",
                 "create_time": "1791540000000", "body": {"content": '{"text":"synthetic offline task"}'},
                 "sender": {"sender_type": "user", "id": "fixture-user"}}]

    def receive(self, event):
        message = event.event.message
        self.callbacks.append(message.message_id)
        self.service.enqueue({"message_id": message.message_id, "chat_id": message.chat_id,
                              "create_time": message.create_time, "message_type": message.message_type,
                              "content": message.content, "sender": {"sender_type": "user"}},
                             "fixture-tenant")

    async def server_handler(self, connection):
        self.connections.append(connection)
        self.active_connections.add(connection)
        self.max_connections = max(self.max_connections, len(self.active_connections))
        try:
            async for raw in connection:
                value = sdk.Frame()
                value.ParseFromString(raw)
                if value.method == sdk.FrameType.CONTROL.value:
                    self.pings += 1
                    await connection.send(frame(sdk.MessageType.PONG.value).SerializeToString())
                else:
                    await self.acks.put(json.loads(value.payload))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.active_connections.discard(connection)

    def start_fixture(self):
        dispatcher = (EventDispatcherHandler.builder("", "")
                      .register_p2_im_message_receive_v1(self.receive).build())
        self.client = transport.Client("fixture-app", "fixture-secret", event_handler=dispatcher,
                                      domain=f"http://fixture-endpoint.invalid:{self.http.server_port}",
                                      log_level=sdk.LogLevel.CRITICAL)
        # Only this isolated fixture shortens the SDK/server cadence and Pong budget.
        self.client._configure(sdk.ClientConfig(self.config))
        self.client.on_reconnecting = self.service.on_ws_reconnecting
        self.client.on_reconnected = self.service.on_ws_reconnected
        self.service.ws_client = self.client
        self.service.ws_loop = self.loop

        def recover():
            try:
                self.service.reconcile_reconnects()
            except FixtureStopped:
                pass
            except BaseException as exc:
                self.recovery_errors.append(type(exc).__name__)

        self.recovery_thread = threading.Thread(target=recover, daemon=True)
        self.service._components = {"reconnect-catch-up": self.recovery_thread}
        self.service.request_catch_up()  # The normal, one-time Listener startup intent.
        self.recovery_thread.start()

        def run():
            asyncio.set_event_loop(self.loop)
            try:
                self.client.start()  # Exercise production startup, not a manual repair/probe.
            except BaseException as exc:
                self.runner_errors.append(type(exc).__name__)
            finally:
                self.loop.run_until_complete(self.client.close())
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()

        self.runner = threading.Thread(target=run, daemon=True)
        self.runner.start()

    async def exercise_flap(self, mode, *, at_start=False):
        if at_start:
            self.mode, self.history_online = mode, False
        self.start_fixture()
        if not at_start:
            await eventually(lambda: self.client.inspect()["healthy"] and not self.service.recovery().pending())
            previous = self.client.inspect()
            self.mode, self.history_online = mode, False
            await self.connections[-1].close()
        else:
            previous = {"generation": 0, "pong_received_at": 0}
        failures = lambda: self.dns_failures if mode == "dns" else self.endpoint_failures
        await eventually(lambda: failures() >= 2 and self.history_failures >= 2)
        self.assertTrue(self.runner.is_alive())
        self.assertTrue(self.recovery_thread.is_alive())
        self.assertFalse(self.client.inspect()["healthy"])
        self.assertEqual(set(self.service.recovery().pending()), {"first", "second"})
        failed_history = self.history_failures
        restored_at = time.time()
        # The only recovery action is restoring the synthetic network. No start,
        # reconnect request, health probe, history call or signal is sent here.
        self.mode = "online"
        await eventually(lambda: self.client.inspect()["healthy"])
        status = self.client.inspect()
        self.assertGreater(status["generation"], previous["generation"])
        self.assertGreater(status["pong_received_at"], max(restored_at, previous["pong_received_at"]))
        await eventually(lambda: self.history_failures > failed_history)
        self.assertTrue(self.service.recovery().pending())
        self.history_online = True
        await eventually(lambda: not self.service.recovery().pending())
        for key, worker in self.service.workers.items():
            self.assertIs(worker, self.original_workers[key])
            self.assertEqual(worker.store.thread_id(), "original-" + key)
            self.assertEqual(worker.store.state["messages"]["offline-" + worker.project["chat_id"]]["status"], "pending")
        self.assertIs(self.service.workers["first"]._active_main_thread, self.business)
        self.assertTrue(self.business.is_alive())
        self.assertEqual(self.service.workers["first"].store.state["messages"]["already-active"]["status"], "processing")
        # Prove the recovered connection dispatches a real protobuf event and
        # ACKs its durable inbox write; a duplicate history ID stays one row.
        message_id = "offline-fixture-chat"
        await self.connections[-1].send(message_frame(message_id).SerializeToString())
        self.assertEqual((await asyncio.wait_for(self.acks.get(), 2))["code"], 200)
        self.assertEqual(self.callbacks, [message_id])
        self.assertEqual(set(self.service.workers["first"].store.state["messages"]), {"already-active", message_id})
        self.assertEqual(self.max_connections, 1)
        self.assertTrue(self.client._ping_task and not self.client._ping_task.done())
        self.assertEqual(self.runner_errors + self.recovery_errors, [])

    async def test_startup_dns_failure_recovers_without_manual_poke(self):
        await self.exercise_flap("dns", at_start=True)

    async def test_running_dns_flap_recovers_without_manual_poke(self):
        await self.exercise_flap("dns")

    async def test_endpoint_unavailability_recovers_without_manual_poke(self):
        await self.exercise_flap("endpoint")

    async def asyncTearDown(self):
        self.mode, self.history_online = "online", True
        if hasattr(self, "service"):
            self.service.reconnect_signal.stopped = True
            self.service.reconnect_signal.set()
        self.stop_runner.set()
        self.stop_business.set()
        for thread in (self.runner, self.recovery_thread, self.business):
            if thread is not None:
                await asyncio.to_thread(thread.join, 3)
                self.assertFalse(thread.is_alive(), "Isolated fixture thread did not exit")
        self.server.close()
        await self.server.wait_closed()
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        self.http_thread.join(timeout=1)
        self.stack.close()
        self.directory.cleanup()


if __name__ == "__main__":
    unittest.main()
