"""Outbound WebSocket to the platform.

The game never listens on a public URL. After the first successful hello, a
dropped socket reconnects and resumes sessions for 30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable

from whitesands.agent_player.errors import ConnectionLostError, PlatformAuthError, ProtocolVersionError
from whitesands.agent_player.logger import log
from whitesands.agent_player.mcp import SessionMuxTransport
from whitesands.agent_player.protocol import PROTOCOL_VERSION, SDK_VERSION, SESSION_RESUME_GRACE_MS

HEARTBEAT_S = 15
PONG_TIMEOUT_S = 10
MAX_RECONNECT_ATTEMPTS = 8

SessionWaiter = tuple[asyncio.Future[dict[str, Any]], asyncio.TimerHandle]


class PlatformConnection:
    def __init__(
        self,
        url: str,
        platform_key: str,
        *,
        on_envelope: Callable[[dict[str, Any]], None],
        on_disconnect: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        sessions: Callable[[], list[dict[str, str]]] | None = None,
        logger: Any = None,
    ) -> None:
        self._url = url
        self._platform_key = platform_key
        self._on_envelope = on_envelope
        self._on_disconnect = on_disconnect
        self._on_reconnect = on_reconnect
        self._on_error = on_error
        self._sessions = sessions
        self._logger = logger
        self.client_id = str(uuid.uuid4())
        self._ws: Any = None
        self._account_name: str | None = None
        self._closed = False
        self._ever_connected = False
        self._reconnecting = False
        self._reconnect_attempt = 0
        self._reconnect_task: asyncio.Task[Any] | None = None
        self._reader: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._pending: dict[str, SessionWaiter] = {}
        self._outbox: list[dict[str, Any]] = []
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_pong: str | None = None
        self._pong = asyncio.Event()

    @property
    def connected(self) -> bool:
        return _is_open(self._ws)

    async def connect(self) -> str:
        if self._closed:
            raise RuntimeError("Platform connection is closed")
        if self.connected and self._account_name:
            return self._account_name
        account = await self._open_socket()
        self._ever_connected = True
        self._reconnect_attempt = 0
        return account

    def send(self, envelope: dict[str, Any]) -> None:
        if self.connected:
            self._spawn(self._send_locked(envelope))
            return
        if envelope.get("type") in ("mcp", "ping"):
            self._outbox.append(envelope)
            return
        raise RuntimeError("Not connected to platform")

    def begin_wait(self, session_id: str, kind: str, timeout_ms: int = 15_000) -> asyncio.Future[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        key = f"session:{session_id}:{kind}"

        def on_timeout() -> None:
            self._pending.pop(key, None)
            if not future.done():
                future.set_exception(TimeoutError(f"Timed out waiting for session {kind}"))

        handle = loop.call_later(timeout_ms / 1000, on_timeout)
        self._pending[key] = (future, handle)
        return future

    def cancel_wait(self, session_id: str, kind: str) -> None:
        self._settle(f"session:{session_id}:{kind}", error=RuntimeError("platform connection closed"))

    def create_transport(self, session_id: str) -> SessionMuxTransport:
        return SessionMuxTransport(session_id, self.send)

    async def close(self) -> None:
        self._closed = True
        self._stop_heartbeat()
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        self._reconnecting = False
        self._fail_waiters(RuntimeError("platform connection closed"))
        ws = self._ws
        self._ws = None
        if ws is not None:
            await _close_ws(ws)
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass

    async def _open_socket(self) -> str:
        connect = _import_connect()
        loop = asyncio.get_running_loop()
        hello: asyncio.Future[str] = loop.create_future()
        ws = await connect(self._url, ping_interval=None, ping_timeout=None)
        self._ws = ws
        self._reader = asyncio.create_task(self._read_loop(ws, hello))
        try:
            await ws.send(
                _dump(
                    {
                        "type": "hello",
                        "platformKey": self._platform_key,
                        "clientId": self.client_id,
                        "protocolVersion": PROTOCOL_VERSION,
                        "sdkVersion": SDK_VERSION,
                    }
                )
            )
            account = await hello
        except Exception:
            await _close_ws(ws)
            if self._ws is ws:
                self._ws = None
            reader = self._reader
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()
                try:
                    await reader
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        self._account_name = account
        self._start_heartbeat()
        self._flush_outbox()
        return account

    async def _read_loop(self, ws: Any, hello: asyncio.Future[str]) -> None:
        try:
            async for raw in ws:
                self._handle_message(ws, hello, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_socket_close(exc):
                self._report_error(exc)
                if not hello.done():
                    hello.set_exception(exc)
        finally:
            self._stop_heartbeat()
            if self._ws is ws:
                self._ws = None
            if not hello.done():
                hello.set_exception(RuntimeError("platform connection closed before hello"))
            if self._closed or not self._ever_connected:
                self._fail_waiters(RuntimeError("platform connection closed"))
                return
            if self._reconnecting:
                return
            if self._on_disconnect is not None:
                self._on_disconnect()
            self._schedule_reconnect()

    def _handle_message(self, ws: Any, hello: asyncio.Future[str], raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._report_error(exc)
            return
        if not isinstance(envelope, dict):
            return
        kind = envelope.get("type")
        if not hello.done() and kind in ("hello_ok", "hello_err"):
            if kind == "hello_err":
                message = str(envelope.get("message") or "hello failed")
                error: BaseException
                if envelope.get("code") == "PROTOCOL":
                    error = ProtocolVersionError(message)
                else:
                    error = PlatformAuthError(message)
                self._closed = True
                hello.set_exception(error)
                _abort(ws)
                return
            hello.set_result(str(envelope.get("accountName") or ""))
            return
        if kind == "pong":
            if self._pending_pong is not None and self._pending_pong == envelope.get("id"):
                self._pending_pong = None
                self._pong.set()
            return
        self._resolve_session_waiter(envelope)
        try:
            self._on_envelope(envelope)
        except Exception as exc:
            self._report_error(exc)

    def _resolve_session_waiter(self, envelope: dict[str, Any]) -> None:
        kind = envelope.get("type")
        session_id = envelope.get("sessionId")
        if not isinstance(session_id, str):
            return
        if kind in ("session_started", "session_stopped"):
            wait_kind = "start" if kind == "session_started" else "stop"
            self._settle(f"session:{session_id}:{wait_kind}", envelope=envelope)
            return
        if kind == "session_resumed":
            self._settle(f"session:{session_id}:resume", envelope=envelope)
            return
        if kind == "resume_err":
            self._settle(
                f"session:{session_id}:resume",
                error=ConnectionLostError(str(envelope.get("message") or "resume failed")),
            )

    def _settle(self, key: str, envelope: dict[str, Any] | None = None, error: BaseException | None = None) -> None:
        entry = self._pending.pop(key, None)
        if entry is None:
            return
        future, handle = entry
        handle.cancel()
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(envelope or {})

    def _schedule_reconnect(self) -> None:
        if self._closed or self._reconnecting:
            return
        if self._reconnect_attempt >= MAX_RECONNECT_ATTEMPTS:
            self._closed = True
            error = ConnectionLostError("Could not reconnect to the platform")
            self._report_error(error)
            self._fail_waiters(error)
            return
        delay = min(SESSION_RESUME_GRACE_MS, 250 * 2**self._reconnect_attempt) / 1000
        self._reconnect_attempt += 1
        self._reconnecting = True
        log(self._logger, "warn", f"platform connection dropped; retrying in {int(delay * 1000)}ms")
        self._reconnect_task = asyncio.create_task(self._reconnect_after(delay))

    async def _reconnect_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._reconnect()
        except asyncio.CancelledError:
            return

    async def _reconnect(self) -> None:
        try:
            await self._open_socket()
            self._ever_connected = True
            self._reconnect_attempt = 0
            self._reconnecting = False
            for session in self._sessions() if self._sessions is not None else []:
                future = self.begin_wait(session["session_id"], "resume")
                self.send(
                    {
                        "type": "resume_session",
                        "sessionId": session["session_id"],
                        "sessionToken": session["session_token"],
                    }
                )
                await future
            if self._on_reconnect is not None:
                self._on_reconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reconnecting = False
            if self._closed:
                return
            if isinstance(exc, (PlatformAuthError, ProtocolVersionError)):
                self._closed = True
                self._report_error(exc)
                self._fail_waiters(exc)
                return
            self._schedule_reconnect()

    def _flush_outbox(self) -> None:
        pending = self._outbox
        self._outbox = []
        for envelope in pending:
            self.send(envelope)

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        self._pending_pong = None
        if task is not None:
            task.cancel()

    async def _heartbeat(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(HEARTBEAT_S)
                if self._closed or not self.connected:
                    continue
                ping_id = str(uuid.uuid4())
                self._pending_pong = ping_id
                self._pong.clear()
                try:
                    self.send({"type": "ping", "id": ping_id})
                except Exception:
                    continue
                try:
                    await asyncio.wait_for(self._pong.wait(), PONG_TIMEOUT_S)
                except TimeoutError:
                    if self._pending_pong == ping_id:
                        log(self._logger, "warn", "platform heartbeat timed out")
                        _abort(self._ws)
        except asyncio.CancelledError:
            return

    async def _send_locked(self, envelope: dict[str, Any]) -> None:
        async with self._send_lock:
            ws = self._ws
            if not _is_open(ws):
                if envelope.get("type") in ("mcp", "ping"):
                    self._outbox.append(envelope)
                return
            try:
                await ws.send(_dump(envelope))
            except Exception:
                if envelope.get("type") in ("mcp", "ping"):
                    self._outbox.append(envelope)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fail_waiters(self, error: BaseException) -> None:
        entries = list(self._pending.items())
        self._pending.clear()
        for _, (future, handle) in entries:
            handle.cancel()
            if not future.done():
                future.set_exception(error)

    def _report_error(self, error: BaseException) -> None:
        if self._on_error is not None:
            self._on_error(error)


def _dump(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)


def _import_connect() -> Any:
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise RuntimeError("Install the websockets package to connect to the platform") from exc
    return connect


def _is_open(ws: Any) -> bool:
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is None:
        return True
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name == "OPEN"
    return "OPEN" in str(state)


def _is_socket_close(exc: BaseException) -> bool:
    return exc.__class__.__name__.startswith("ConnectionClosed")


def _abort(ws: Any) -> None:
    if ws is None:
        return
    transport = getattr(ws, "transport", None)
    if transport is not None:
        try:
            transport.abort()
            return
        except Exception:
            pass
    try:
        ws.close()
    except Exception:
        pass


async def _close_ws(ws: Any) -> None:
    try:
        await ws.close()
    except Exception:
        _abort(ws)
