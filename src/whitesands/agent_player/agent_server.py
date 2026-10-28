"""One in-process MCP server and one outbound connection to the platform.

Every start() is its own session. The game calls turn() to decide whose turn
it is. The platform chooses the action.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from whitesands.agent_player.errors import ConnectionLostError, TurnFailedError
from whitesands.agent_player.events import AgentEvent
from whitesands.agent_player.logger import log
from whitesands.agent_player.mcp import InMemoryTransport, SessionActionLog, SessionMcpServer
from whitesands.agent_player.memory import SessionMemory, default_memory_path
from whitesands.agent_player.platform_connection import PlatformConnection
from whitesands.agent_player.protocol import (
    DEFAULT_AGENT_INSTRUCTIONS,
    PLATFORM_URL,
    TokenUsage,
    TurnReport,
    turn_report_from_wire,
)
from whitesands.agent_player.reflect import reflect_player
from whitesands.agent_player.turn_gate import TurnGate

DEFAULT_TURN_TIMEOUT_MS = 60_000

EventListener = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class TurnResult:
    """What the agent did on one granted turn."""

    action: str
    args: dict[str, Any]
    result: Any
    attempts: int
    usage: TokenUsage


@dataclass
class StartSession:
    """What an inline platform receives for one player."""

    session_id: str
    session_token: str
    name: str
    tools: list[dict[str, Any]]
    instructions: str
    transport: Any
    on_turn_result: Callable[[TurnReport], None]
    goal: str | None = None
    logger: Any = None
    on_event: Callable[[AgentEvent], None] | None = None
    on_usage: Callable[[int], Any] | None = None


class AgentHandle:
    """One started player. turn() grants that character one chance to act."""

    def __init__(
        self,
        session_id: str,
        turn: Callable[..., Any],
        stop: Callable[[], Any],
        on: Callable[[EventListener], Callable[[], None]],
    ) -> None:
        self.session_id = session_id
        self._turn = turn
        self._stop = stop
        self._on = on

    async def turn(self, *, timeout_ms: int | None = None, signal: Any = None) -> TurnResult:
        """Grant this session one turn.

        Resolves with what the agent did, or raises after a second failed
        action, a timeout, or a lost connection.
        """
        return await self._turn(timeout_ms=timeout_ms, signal=signal)

    async def stop(self) -> None:
        """Stop the remote agent for this session. The AgentServer stays up."""
        await self._stop()

    def on(self, listener: EventListener) -> Callable[[], None]:
        """Subscribe to this session's action, retry, and failure events."""
        return self._on(listener)


class _Session:
    def __init__(
        self,
        session_id: str,
        token: str,
        name: str,
        gate: TurnGate,
        server: SessionMcpServer,
    ) -> None:
        self.id = session_id
        self.token = token
        self.name = name
        self.gate = gate
        self.server = server
        self.listeners: set[EventListener] = set()
        self.mux: Any = None


class AgentServer:
    def __init__(
        self,
        *,
        platform_key: str | None = None,
        inline: Any = None,
        instructions: str | None = None,
        turn_timeout_ms: int | None = None,
        on_event: EventListener | None = None,
        logger: Any = None,
        memory_path: str | None | bool = None,
    ) -> None:
        resolved_key = platform_key if platform_key is not None else os.environ.get("WSI_PLATFORM_KEY")
        if inline is None and not resolved_key:
            raise RuntimeError(
                "Set WSI_PLATFORM_KEY or pass platform_key, unless an inline platform is provided"
            )
        self._platform_key = resolved_key or "inline"
        self._platform_url = PLATFORM_URL
        self._inline = inline
        self._server_instructions = instructions
        self._turn_timeout_ms = turn_timeout_ms
        self._logger = logger
        self._memory_path = memory_path
        self._connection: PlatformConnection | None = None
        self._sessions: dict[str, _Session] = {}
        self._listeners: set[EventListener] = set()
        self._memory: SessionMemory | None = None
        self._started = False
        self._closing = False
        if on_event is not None:
            self._listeners.add(on_event)

    async def __aenter__(self) -> AgentServer:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the outbound platform connection. No-op for an inline platform."""
        if self._inline is not None or self._started:
            self._started = True
            return
        self._connection = PlatformConnection(
            self._platform_url,
            self._platform_key,
            on_envelope=self._handle_envelope,
            on_disconnect=lambda: self._emit(AgentEvent(type="disconnected")),
            on_reconnect=lambda: self._emit(AgentEvent(type="reconnected")),
            on_error=self._on_connection_error,
            sessions=lambda: [
                {"session_id": session.id, "session_token": session.token}
                for session in self._sessions.values()
            ],
            logger=self._logger,
        )
        await self._connection.connect()
        self._started = True

    async def start(
        self,
        instance: object,
        *,
        name: str | None = None,
        goal: str | None = None,
        instructions: str | None = None,
    ) -> AgentHandle:
        """Reflect the player into tools, register a session, and return a handle."""
        await self.connect()
        reflected = reflect_player(instance)
        session_id = str(uuid.uuid4())
        session_token = str(uuid.uuid4())
        gate = TurnGate()
        player_name = name or type(instance).__name__ or "player"
        memory = self._action_memory()
        actions = SessionActionLog(memory, session_id, player_name) if memory is not None else None
        server = SessionMcpServer(
            reflected,
            gate,
            name=f"agent-player:{player_name}",
            actions=actions,
        )
        prompt = _join_instructions(self._server_instructions, instructions)
        session = _Session(session_id, session_token, player_name, gate, server)

        if self._inline is not None:
            server_transport, client_transport = InMemoryTransport.create_linked_pair()
            await server.connect(server_transport)
            self._sessions[session_id] = session
            await self._inline.start_session(
                StartSession(
                    session_id=session_id,
                    session_token=session_token,
                    name=player_name,
                    tools=reflected.tools,
                    instructions=prompt,
                    transport=client_transport,
                    on_turn_result=gate.complete_turn,
                    goal=goal,
                    logger=self._logger,
                    on_event=self._emit,
                )
            )
        else:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Not connected")
            mux = connection.create_transport(session_id)
            await server.connect(mux)
            session.mux = mux
            self._sessions[session_id] = session
            started = connection.begin_wait(session_id, "start")
            body: dict[str, Any] = {
                "type": "start_session",
                "sessionId": session_id,
                "sessionToken": session_token,
                "name": player_name,
                "tools": reflected.tools,
                "instructions": prompt,
            }
            if goal:
                body["goal"] = goal
            connection.send(body)
            await started

        return AgentHandle(
            session_id,
            lambda **kwargs: self._turn(session_id, **kwargs),
            lambda: self._stop_session(session_id),
            lambda listener: self._on_session(session_id, listener),
        )

    def on(self, listener: EventListener) -> Callable[[], None]:
        """Subscribe to every session, plus connection events."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    async def close(self) -> None:
        """Stop every session and close the platform connection."""
        if self._closing:
            return
        self._closing = True
        try:
            for session_id in list(self._sessions):
                await self._stop_session(session_id)
            if self._inline is not None and hasattr(self._inline, "close"):
                await self._inline.close()
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            if self._memory is not None:
                self._memory.close()
                self._memory = None
            self._started = False
        finally:
            self._closing = False

    async def _turn(self, session_id: str, *, timeout_ms: int | None = None, signal: Any = None) -> TurnResult:
        session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"Unknown session {session_id}")
        if timeout_ms is None:
            timeout_ms = self._turn_timeout_ms if self._turn_timeout_ms is not None else DEFAULT_TURN_TIMEOUT_MS
        outcome = await session.gate.run_turn(timeout_ms=timeout_ms, signal=signal)
        if not outcome.ok:
            if outcome.connection_lost:
                raise ConnectionLostError(outcome.error or "Connection to the platform was lost")
            raise TurnFailedError(outcome.error or "turn failed", outcome.attempt_details)
        return TurnResult(
            action=outcome.action or "unknown",
            args=dict(outcome.args or {}),
            result=outcome.result,
            attempts=outcome.attempts or 1,
            usage=outcome.usage or TokenUsage(),
        )

    def _on_session(self, session_id: str, listener: EventListener) -> Callable[[], None]:
        session = self._sessions.get(session_id)
        if session is None:
            return lambda: None
        session.listeners.add(listener)

        def unsubscribe() -> None:
            session.listeners.discard(listener)

        return unsubscribe

    def _emit(self, event: AgentEvent) -> None:
        for listener in list(self._listeners):
            listener(event)
        if event.type in ("disconnected", "reconnected") or not event.session_id:
            return
        session = self._sessions.get(event.session_id)
        if session is None:
            return
        for listener in list(session.listeners):
            listener(event)

    def _on_connection_error(self, error: BaseException) -> None:
        log(self._logger, "error", str(error))
        if isinstance(error, ConnectionLostError):
            for session in self._sessions.values():
                session.gate.fail_turn(error)

    def _handle_envelope(self, envelope: dict[str, Any]) -> None:
        kind = envelope.get("type")
        session_id = envelope.get("sessionId")
        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if kind == "mcp":
            if session is not None and session.mux is not None:
                session.mux.receive(envelope.get("message"))
            return
        if kind == "turn_result":
            if session is not None:
                session.gate.complete_turn(turn_report_from_wire(envelope))
            return
        if kind == "session_event":
            self._emit_wire_event(envelope)
            return
        if kind == "error":
            log(self._logger, "error", str(envelope.get("message") or ""))

    def _emit_wire_event(self, envelope: dict[str, Any]) -> None:
        session_id = str(envelope.get("sessionId") or "")
        name = envelope.get("event")
        action = envelope.get("action")
        args = envelope.get("args") if isinstance(envelope.get("args"), dict) else None
        if name == "action":
            self._emit(
                AgentEvent(
                    type="action",
                    session_id=session_id,
                    action=str(action or "unknown"),
                    args=dict(args or {}),
                )
            )
            return
        if name == "retry":
            self._emit(
                AgentEvent(
                    type="retry",
                    session_id=session_id,
                    action=str(action or "unknown"),
                    args=dict(args or {}),
                    error=str(envelope.get("error") or "action failed"),
                )
            )
            return
        self._emit(
            AgentEvent(
                type="turnFailed",
                session_id=session_id,
                error=str(envelope.get("error") or "turn failed"),
                action=str(action) if action else None,
                args=dict(args) if args else None,
            )
        )

    async def _stop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.gate.reset()
        if self._inline is not None:
            await self._inline.stop_session(session_id)
        elif self._connection is not None:
            connection = self._connection
            waiter = connection.begin_wait(session_id, "stop")
            try:
                connection.send({"type": "stop_session", "sessionId": session_id})
            except Exception:
                connection.cancel_wait(session_id, "stop")
            try:
                await waiter
            except Exception:
                pass
        try:
            await session.server.close()
        except Exception:
            pass

    def _action_memory(self) -> SessionMemory | None:
        if self._memory_path is False:
            return None
        if self._memory is None:
            path = self._memory_path if isinstance(self._memory_path, str) else default_memory_path()
            self._memory = SessionMemory(path)
        return self._memory


def _join_instructions(server_instructions: str | None, start_instructions: str | None) -> str:
    base = DEFAULT_AGENT_INSTRUCTIONS if server_instructions is None else server_instructions
    parts = [part for part in (base, start_instructions) if part]
    return "\n\n".join(parts)
