"""Narrow lark-oapi 1.7.1 transport adapter; business workers remain independent.

Keep the SDK's protobuf protocol and server-supplied reconnect/ping settings.
Private overrides are exercised with real local WebSocket/protobuf exchanges.
HTTP_TIMEOUT_SECONDS is a socket connect/read timeout, not a wall-clock limit.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import http
import inspect as inspect_module
import threading
import time
from urllib.parse import parse_qs, urlparse

from lark_oapi.ws import client as sdk

if __package__:
    from .gateway_primitives import HTTP_TIMEOUT_SECONDS
else:
    from gateway_primitives import HTTP_TIMEOUT_SECONDS


_frame_origin = contextvars.ContextVar("gateway_frame_origin", default=None)


class Client(sdk.Client):
    """SDK-compatible client with fresh application-Pong checks and recovery."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._transport_loop = None
        self._receive_task = None
        self._recovery_task = None
        self._probe_task = None
        self._ping_task = None
        self._message_tasks = set()
        self._stopping = False
        self._generation = 0
        self._pong_event = asyncio.Event()
        self._pong_sequence = 0
        self._last_pong_monotonic = None
        self._last_pong_at = None
        self._last_error = "not-connected"
        self._status_lock = threading.RLock()

    def _fmt_log(self, fmt, *args):
        # The SDK's lifecycle logs include authenticated URLs; exception text
        # may include a URL, response body or credentials. Only retain types.
        safe = [type(value).__name__ if isinstance(value, BaseException)
                else value if isinstance(value, (int, float, bool))
                else "[detail omitted]" for value in args]
        return fmt.format(*safe)

    def _get_conn_url(self):
        """SDK endpoint discovery, with the gateway's existing HTTP timeout.

        Keep assertion-provider support and server configuration parsing. This
        method runs off the asyncio loop; requests' timeout is inactivity-based.
        """
        if sdk.Strings.is_empty(self._app_id) or (
                self._client_assertion_provider is None and sdk.Strings.is_empty(self._app_secret)):
            raise sdk.ClientException(sdk.NO_CREDENTIAL, "missing application credentials")
        headers = dict(self._headers)
        headers.update({"locale": "zh", sdk.USER_AGENT: self._user_agent})
        url = self._domain + sdk.GEN_ENDPOINT_URI
        body = {"AppID": self._app_id}
        if self._client_assertion_provider is not None:
            aud = sdk.extract_aud_from_url(self._domain)
            token = self._client_assertion_provider.retrieve_token(aud)
            if token is None or sdk.Strings.is_empty(token.value):
                raise sdk.ClientException(7101, "empty client assertion")
            body.update(AppSecret="", ClientAssertion=token.value)
            if token.target_info is not None:
                url = sdk.build_proxy_url(token.target_info, sdk.GEN_ENDPOINT_URI)
                headers[sdk.X_TARGET_SERVICE] = aud
        else:
            body["AppSecret"] = self._app_secret
        with sdk.requests.post(url, headers=headers, json=body,
                               timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status_code != http.HTTPStatus.OK:
                raise sdk.ServerException(response.status_code, "endpoint discovery failed")
            result = sdk.JSON.unmarshal(response.content.decode(sdk.UTF_8), sdk.EndpointResp)
        if result.code in (sdk.SYSTEM_BUSY, sdk.INTERNAL_ERROR):
            raise sdk.ServerException(result.code, "endpoint discovery unavailable")
        if result.code != sdk.OK:
            raise sdk.ClientException(result.code, "endpoint discovery rejected")
        if result.data.ClientConfig is not None:
            self._configure(result.data.ClientConfig)
        return result.data.URL

    async def _connect(self):
        self._transport_loop = asyncio.get_running_loop()
        # The upstream early return sits outside its finally and leaks the
        # lock. async-with also keeps concurrent connection attempts singular.
        async with self._lock:
            if self._conn is not None:
                return
            conn_url = await asyncio.to_thread(self._get_conn_url)
            params = parse_qs(urlparse(conn_url).query)
            try:
                connection = await sdk.websockets.connect(conn_url, **sdk._ws_connect_kwargs())
            except sdk.InvalidHandshake as exc:
                sdk._parse_ws_conn_exception(exc)
                raise
            with self._status_lock:
                self._conn = connection
                self._conn_url = conn_url
                self._conn_id = params[sdk.DEVICE_ID][0]
                self._service_id = params[sdk.SERVICE_ID][0]
                self._generation += 1
                generation = self._generation
                self._last_pong_monotonic = None
                self._last_pong_at = None
                self._last_error = "awaiting-application-pong"
            self._pong_event = asyncio.Event()
            self._receive_task = asyncio.create_task(
                self._receive_message_loop(connection, generation), name="feishu-receive")
            self._receive_task.add_done_callback(
                lambda task: self._receiver_finished(task, connection, generation))

    async def _receive_message_loop(self, connection=None, generation=None):
        connection = connection or self._conn
        generation = self._generation if generation is None else generation
        try:
            while not self._stopping and connection is self._conn:
                payload = await connection.recv()
                task = asyncio.create_task(self._dispatch_message(payload, connection, generation))
                self._message_tasks.add(task)
                task.add_done_callback(self._message_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return "receive-" + type(exc).__name__
        return "receive-loop-stopped"

    def _receiver_finished(self, task, connection, generation):
        # A done callback also covers cancellation before the coroutine starts,
        # when its try/finally cannot execute at all.
        try:
            error = task.result() or "receive-loop-stopped"
        except asyncio.CancelledError:
            error = "receive-loop-cancelled"
        except Exception as exc:
            error = "receive-" + type(exc).__name__
        if self._receive_task is task:
            self._receive_task = None
        if not self._stopping and connection is self._conn and generation == self._generation:
            self._request_repair(connection, generation, error)

    async def _dispatch_message(self, payload, connection, generation):
        token = _frame_origin.set((connection, generation))
        try:
            await super()._handle_message(payload)
        finally:
            _frame_origin.reset(token)

    async def _handle_control_frame(self, frame):
        origin = _frame_origin.get()
        if origin != (self._conn, self._generation):
            return
        await super()._handle_control_frame(frame)
        if sdk._get_by_key(frame.headers, sdk.HEADER_TYPE) != sdk.MessageType.PONG.value:
            return
        if origin != (self._conn, self._generation):
            return
        with self._status_lock:
            self._pong_sequence += 1
            self._last_pong_monotonic = time.monotonic()
            self._last_pong_at = time.time()
        self._pong_event.set()

    async def _handle_data_frame(self, frame):
        """Keep SDK ACK semantics, with durable callbacks off the event loop."""
        headers = frame.headers
        message_id = sdk._get_by_key(headers, sdk.HEADER_MESSAGE_ID)
        count = int(sdk._get_by_key(headers, sdk.HEADER_SUM))
        sequence = int(sdk._get_by_key(headers, sdk.HEADER_SEQ))
        kind = sdk.MessageType(sdk._get_by_key(headers, sdk.HEADER_TYPE))
        payload = frame.payload
        if count > 1:
            payload = self._combine(message_id, count, sequence, payload)
            if payload is None:
                return
        if kind != sdk.MessageType.EVENT:
            return
        response = sdk.Response(code=http.HTTPStatus.OK)
        try:
            started = time.monotonic()
            result = await asyncio.to_thread(self._event_handler._do_without_validation, payload)
            header = headers.add()
            header.key = sdk.HEADER_BIZ_RT
            header.value = str(int((time.monotonic() - started) * 1000))
            if result is not None:
                response.data = base64.b64encode(sdk.JSON.marshal(result).encode(sdk.UTF_8))
        except Exception as exc:
            sdk.logger.error("Feishu durable callback failed (%s)", type(exc).__name__)
            response = sdk.Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
        frame.payload = sdk.JSON.marshal(response).encode(sdk.UTF_8)
        await self._write_message(frame.SerializeToString())

    async def _write_message(self, data):
        origin = _frame_origin.get()
        async with self._lock:
            if self._conn is None or (origin is not None and origin != (self._conn, self._generation)):
                raise sdk.ConnectionClosedException("original connection is no longer available")
            await self._conn.send(data)

    def probe_timeout(self):
        """Use the underlying WebSocket's existing Pong budget, without a new clock."""
        timeout = getattr(self._conn, "ping_timeout", inspect_module.signature(
            sdk.websockets.connect).parameters["ping_timeout"].default)
        if timeout is None:
            raise RuntimeError("WebSocket heartbeat timeout is disabled")
        return float(timeout)

    def inspect(self):
        """A cached observation. Only probe() supplies a new functional proof."""
        with self._status_lock:
            connection = self._conn
            receiver = self._receive_task
            state = getattr(getattr(connection, "state", None), "name", "DISCONNECTED")
            receiving = receiver is not None and not receiver.done()
            connected = state == "OPEN" and receiving
            return {
                "healthy": connected and self._last_pong_at is not None and self._last_error is None,
                "connected": connected,
                "state": state,
                "receiver_running": receiving,
                "reconnecting": self._recovery_task is not None and not self._recovery_task.done(),
                "generation": self._generation,
                "pong_received_at": self._last_pong_at,
                "error": self._last_error,
                "verification": "cached-application-pong",
            }

    async def probe(self):
        """Coalesce concurrent callers and require a fresh Feishu application PONG."""
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_once(), name="feishu-probe")
        return await asyncio.shield(self._probe_task)

    async def ensure_health(self):
        return await self.probe()

    async def _probe_once(self):
        connection, generation = self._conn, self._generation
        if self._stopping:
            return {**self.inspect(), "healthy": False, "error": "stopped"}
        if connection is None or self._receive_task is None or self._receive_task.done():
            self._request_repair(connection, generation, "receiver-unavailable")
            return {**self.inspect(), "healthy": False, "error": "receiver-unavailable"}
        started = time.monotonic()
        baseline = self._pong_sequence
        event = self._pong_event
        event.clear()

        async def exchange():
            ping = sdk._new_ping_frame(int(self._service_id))
            await self._write_message(ping.SerializeToString())
            while True:
                if connection is not self._conn or generation != self._generation:
                    raise sdk.ConnectionClosedException("connection changed during probe")
                if (self._pong_sequence > baseline and self._last_pong_monotonic is not None
                        and self._last_pong_monotonic >= started):
                    return
                await event.wait()
                event.clear()

        try:
            # Python 3.10-compatible; one existing Pong budget covers both
            # sending PING and receiving PONG. probe() still shields this owner.
            budget = self.probe_timeout()
            await asyncio.wait_for(exchange(), timeout=budget)
            # wait_for may resume this owner after its exchange task completes.
            # A reconnect in that gap cannot inherit the old connection's proof.
            if connection is not self._conn or generation != self._generation:
                raise sdk.ConnectionClosedException("connection changed after probe exchange")
            with self._status_lock:
                self._last_error = None
            return {**self.inspect(), "verification": "fresh-application-pong"}
        except Exception as exc:
            error = "probe-" + type(exc).__name__
            self._request_repair(connection, generation, error)
            return {**self.inspect(), "healthy": False, "error": error,
                    "verification": "fresh-application-pong-failed"}

    def _request_repair(self, connection, generation, error):
        if self._stopping or generation != self._generation or connection is not self._conn:
            return
        with self._status_lock:
            self._last_error = error
        self._pong_event.set()
        if self._auto_reconnect and (self._recovery_task is None or self._recovery_task.done()):
            self._recovery_task = asyncio.create_task(self._repair(), name="feishu-reconnect")

    async def _repair(self):
        current = asyncio.current_task()
        try:
            await self._disconnect()
            if not self._stopping:
                # Retain server ReconnectNonce, ReconnectInterval and
                # ReconnectCount; no second retry ceiling or backoff policy.
                await super()._reconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self._status_lock:
                self._last_error = "reconnect-" + type(exc).__name__
            sdk.logger.error("Feishu reconnect failed (%s)", type(exc).__name__)
        finally:
            if self._recovery_task is current:
                self._recovery_task = None

    async def _disconnect(self):
        async with self._lock:
            connection = self._conn
            with self._status_lock:
                self._conn = None
                self._conn_url = ""
                self._conn_id = ""
                self._service_id = ""
            self._pong_event.set()
            if connection is not None:
                await connection.close()

    async def _ping_loop(self):
        # Replace the SDK's fire-and-forget application ping with a verified
        # exchange on exactly its existing, server-controlled cadence.
        while not self._stopping:
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sdk.logger.error("Feishu heartbeat failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._ping_interval)

    def start(self):
        self._stopping = False
        self._transport_loop = sdk.loop
        try:
            sdk.loop.run_until_complete(self._connect())
        except sdk.ClientException:
            raise
        except Exception:
            if not self._auto_reconnect:
                raise
            self._recovery_task = sdk.loop.create_task(self._repair(), name="feishu-reconnect")
            sdk.loop.run_until_complete(self._recovery_task)
        self._ping_task = sdk.loop.create_task(self._ping_loop(), name="feishu-heartbeat")
        sdk.loop.run_until_complete(sdk._select())

    async def close(self):
        """Stop only this transport, for orderly ownership transfer and tests."""
        self._stopping = True
        tasks = [task for task in (self._ping_task, self._probe_task, self._recovery_task)
                 if task is not None and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        receiver = self._receive_task
        await self._disconnect()
        if receiver is not None:
            await asyncio.gather(receiver, return_exceptions=True)
        if self._message_tasks:
            await asyncio.gather(*list(self._message_tasks), return_exceptions=True)
