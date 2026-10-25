"""Minimal MCP server and client used by one player session.

The hosted platform calls the server over the shared socket. The offline loop
uses the client against an in-memory pair. No socket is required for that path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, is_dataclass, asdict
from typing import Any, Callable

from whitesands.agent_player.memory import view_with_done
from whitesands.agent_player.protocol import MCP_PROTOCOL_VERSION, SDK_VERSION
from whitesands.agent_player.reflect import ReflectedPlayer
from whitesands.agent_player.turn_gate import TurnGate

EnvelopeSender = Callable[[dict[str, Any]], None]
MessageHandler = Callable[[dict[str, Any]], None]


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), separators=(",", ":"), ensure_ascii=False)


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if hasattr(value, "__dict__"):
        return {
            key: to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def rpc_response(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
    }
    if msg_id is not None:
        body["id"] = msg_id
    return body


def rpc_request(msg_id: int, method: str, params: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def rpc_notify(method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def json_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    body = {"ok": True} if payload is None else payload
    return {
        "isError": is_error,
        "content": [{"type": "text", "text": dumps(body)}],
    }


def tool_error(message: str) -> dict[str, Any]:
    return json_result({"error": message}, is_error=True)


class _QueuedTransport:
    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._pending: list[dict[str, Any]] = []
        self._started = False
        self._closed = False

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler
        if self._started:
            self._flush()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Transport already closed")
        self._started = True
        if self._handler is not None:
            self._flush()

    async def close(self) -> None:
        self._closed = True
        self._handler = None

    def _flush(self) -> None:
        handler = self._handler
        if handler is None:
            return
        queued = self._pending
        self._pending = []
        for message in queued:
            handler(message)

    def _deliver(self, message: dict[str, Any]) -> None:
        if self._closed:
            return
        if not self._started or self._handler is None:
            self._pending.append(message)
            return
        try:
            self._handler(message)
        except Exception:
            # The server handler reports its own failures as JSON-RPC errors.
            return


class InMemoryTransport(_QueuedTransport):
    """Linked pair used by the offline platform. No socket."""

    def __init__(self) -> None:
        super().__init__()
        self._peer: InMemoryTransport | None = None

    @staticmethod
    def create_linked_pair() -> tuple[InMemoryTransport, InMemoryTransport]:
        server = InMemoryTransport()
        client = InMemoryTransport()
        server._peer = client
        client._peer = server
        return server, client

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("Transport closed")
        peer = self._peer
        if peer is None:
            raise RuntimeError("Transport is not linked")
        peer._deliver(message)


class SessionMuxTransport(_QueuedTransport):
    """MCP transport that multiplexes JSON-RPC over the platform socket, keyed by session id."""

    def __init__(self, session_id: str, send_envelope: EnvelopeSender) -> None:
        super().__init__()
        self.session_id = session_id
        self._send_envelope = send_envelope

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("Transport closed")
        self._send_envelope({"type": "mcp", "sessionId": self.session_id, "message": message})

    def receive(self, message: Any) -> None:
        if isinstance(message, dict):
            self._deliver(message)


@dataclass
class ToolPayload:
    is_error: bool
    text: str
    json: Any


def parse_tool_payload(result: Any) -> ToolPayload:
    if not isinstance(result, dict):
        text = "" if result is None else str(result)
        return ToolPayload(False, text, text)
    parts: list[str] = []
    for entry in result.get("content") or []:
        if isinstance(entry, dict) and entry.get("type") == "text" and isinstance(entry.get("text"), str):
            parts.append(entry["text"])
    text = "\n".join(parts)
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    return ToolPayload(bool(result.get("isError")), text, parsed)


class McpClient:
    """Minimal MCP client used by the offline session loop. The hosted platform has its own."""

    def __init__(self) -> None:
        self._transport: InMemoryTransport | SessionMuxTransport | None = None
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._closed = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def connect(self, transport: InMemoryTransport | SessionMuxTransport) -> None:
        self._transport = transport
        transport.on_message(self.deliver)
        await transport.start()
        await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-player", "version": SDK_VERSION},
            },
        )
        await transport.send(rpc_notify("notifications/initialized"))

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        tools: list[dict[str, Any]] = []
        for tool in (result or {}).get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "")
            tools.append(
                {
                    "name": name,
                    "description": tool.get("description") or name,
                    "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                }
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> ToolPayload:
        if not isinstance(arguments, dict):
            arguments = {}
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        return parse_tool_payload(result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        waiters = list(self._pending.values())
        self._pending.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(RuntimeError("mcp client closed"))
        transport = self._transport
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass

    async def _request(self, method: str, params: Any) -> Any:
        if self._closed:
            raise RuntimeError("mcp client closed")
        transport = self._transport
        if transport is None:
            raise RuntimeError("Client not connected")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await transport.send(rpc_request(msg_id, method, params))
            return await future
        finally:
            self._pending.pop(msg_id, None)

    def deliver(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        method = message.get("method")
        has_id = "id" in message and message.get("id") is not None
        if method:
            if not has_id or self._transport is None:
                return
            if method == "ping":
                reply = rpc_response(message["id"], {})
            else:
                reply = rpc_error(message["id"], -32601, f"method not found: {method}")
            self._spawn(self._send_reply(reply))
            return
        if not has_id:
            return
        future = self._take_pending(message["id"])
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(RuntimeError(str(error.get("message") or "mcp error")))
            return
        if "result" in message:
            future.set_result(message["result"])
            return
        future.set_exception(RuntimeError("mcp response missing result"))

    def _take_pending(self, msg_id: Any) -> asyncio.Future[Any] | None:
        future = self._pending.get(msg_id)
        if future is None and isinstance(msg_id, str) and msg_id.isdigit():
            future = self._pending.get(int(msg_id))
            msg_id = int(msg_id)
        return future

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_reply(self, message: dict[str, Any]) -> None:
        transport = self._transport
        if transport is None or self._closed:
            return
        try:
            await transport.send(message)
        except Exception:
            pass


class SessionActionLog:
    def __init__(self, memory: Any, session_id: str, character: str) -> None:
        self._memory = memory
        self._session_id = session_id
        self._character = character

    def record(self, entry: dict[str, Any]) -> None:
        self._memory.record(self._session_id, self._character, entry)

    def done(self) -> list[dict[str, Any]]:
        return self._memory.done(self._session_id)


class SessionMcpServer:
    """In-process MCP server for one player session.

    Tools are see, wait, and every public action. When an action log is set,
    see and wait include what this character already called.
    """

    def __init__(
        self,
        player: ReflectedPlayer,
        gate: TurnGate,
        *,
        name: str | None = None,
        actions: SessionActionLog | None = None,
    ) -> None:
        self._player = player
        self._gate = gate
        self._name = name or "agent-player"
        self._actions = actions
        self._transport: InMemoryTransport | SessionMuxTransport | None = None
        self._closed = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def connect(self, transport: InMemoryTransport | SessionMuxTransport) -> None:
        self._transport = transport
        transport.on_message(self._on_message)
        await transport.start()

    async def close(self) -> None:
        self._closed = True
        transport = self._transport
        if transport is None:
            return
        try:
            await transport.close()
        except Exception:
            pass

    def _on_message(self, message: dict[str, Any]) -> None:
        task = asyncio.create_task(self._handle(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, message: dict[str, Any]) -> None:
        if self._closed or not isinstance(message, dict):
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        has_id = "id" in message and message.get("id") is not None
        if not has_id or method.startswith("notifications/"):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        msg_id = message["id"]
        try:
            if method == "initialize":
                self._reply(msg_id, self._initialize(params))
            elif method == "ping":
                self._reply(msg_id, {})
            elif method == "tools/list":
                self._reply(msg_id, {"tools": self._player.tools})
            elif method == "tools/call":
                self._reply(msg_id, await self._call_tool(params))
            else:
                self._try_send(rpc_error(msg_id, -32601, f"method not found: {method}"))
        except Exception as exc:
            self._try_send(rpc_error(msg_id, -32603, str(exc)))

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = MCP_PROTOCOL_VERSION
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested:
            version = requested
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": self._name, "version": SDK_VERSION},
        }

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return tool_error("missing tool name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        try:
            if name == "see":
                return json_result(await self._viewed())
            if name == "wait":
                await self._gate.wait_for_turn()
                return json_result(await self._viewed())
            action = next((candidate for candidate in self._player.actions if candidate.name == name), None)
            if action is None:
                return tool_error(f"Unknown tool: {name}")
            try:
                result = await action.invoke(args)
                if self._actions is not None:
                    self._actions.record({"action": name, "args": args, "ok": True})
                return json_result(result)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                if self._actions is not None:
                    self._actions.record({"action": name, "args": args, "ok": False, "error": message})
                return tool_error(message)
        except Exception as exc:
            return tool_error(str(exc) or exc.__class__.__name__)

    async def _viewed(self) -> Any:
        view = await self._player.see.invoke({})
        if self._actions is None:
            return view
        return view_with_done(view, self._actions.done())

    def _reply(self, msg_id: Any, result: Any) -> None:
        self._try_send(rpc_response(msg_id, result))

    def _try_send(self, message: dict[str, Any]) -> None:
        if self._closed or self._transport is None:
            return
        self._on_message_send(message)

    def _on_message_send(self, message: dict[str, Any]) -> None:
        transport = self._transport
        if transport is None:
            return
        task = asyncio.create_task(self._send(transport, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, transport: InMemoryTransport | SessionMuxTransport, message: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            await transport.send(message)
        except Exception:
            pass
