"""Per-session loop: wait, read the view, ask the model, one action, retry once."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from whitesands.agent_player.events import AgentEvent
from whitesands.agent_player.logger import log
from whitesands.agent_player.mcp import InMemoryTransport, McpClient, SessionMuxTransport, ToolPayload
from whitesands.agent_player.protocol import Attempt, TokenUsage, TurnReport

Transport = InMemoryTransport | SessionMuxTransport


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]

    @staticmethod
    def from_value(value: ToolCall | dict[str, Any]) -> ToolCall:
        if isinstance(value, ToolCall):
            return value
        arguments = value.get("arguments") or {}
        return ToolCall(str(value["name"]), dict(arguments) if isinstance(arguments, dict) else {})


@dataclass(frozen=True)
class ModelDecision:
    tool_call: ToolCall
    usage: TokenUsage


class ModelClient(Protocol):
    """Ask the model to choose exactly one tool call."""

    async def decide(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> ModelDecision: ...


@dataclass
class SessionLoop:
    """Same wait, act, and retry loop the hosted platform runs. No network."""

    session_id: str
    transport: Transport
    instructions: str
    model: ModelClient
    on_turn_result: Callable[[TurnReport], None]
    goal: str | None = None
    logger: Any = None
    on_usage: Callable[[int], Awaitable[None] | None] | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None
    _client: McpClient | None = field(default=None, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)
    _run_task: Any = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        if self._run_task is not None:
            return
        client = McpClient()
        await client.connect(self.transport)
        self._client = client
        self._run_task = asyncio.create_task(self._guarded())

    async def stop(self) -> None:
        self._stopped = True
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        task = self._run_task
        self._run_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _guarded(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._stopped:
                return
            message = str(exc) or exc.__class__.__name__
            log(self.logger, "error", f"session {self.session_id} crashed: {message}")
            self.on_turn_result(TurnReport(ok=False, error=message, attempts=0))

    async def _run(self) -> None:
        client = self._client
        if client is None:
            return
        listed = await client.list_tools()
        action_tools = [tool for tool in listed if tool["name"] not in ("wait", "see")]
        while not self._stopped:
            wait_result = await self._call_tool("wait", {})
            if self._stopped:
                break
            if wait_result.is_error:
                self.on_turn_result(TurnReport(ok=False, error=wait_result.text, attempts=0))
                continue

            usage = TokenUsage(0, 0)
            attempt_details: list[Attempt] = []
            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": (
                        f"It is your turn. Current view:\n{wait_result.text}\nCall exactly one action tool."
                    ),
                }
            ]
            action = await self._decide(action_tools, messages, usage)
            result: ToolPayload | None = None
            if action.name in ("wait", "see"):
                first_error = f"{action.name} is not an action"
            else:
                self._emit({"event": "action", "action": action.name, "args": action.arguments})
                result = await self._call_tool(action.name, action.arguments)
                if not result.is_error:
                    self.on_turn_result(
                        TurnReport(
                            ok=True,
                            action=action.name,
                            args=action.arguments,
                            result=result.json,
                            attempts=1,
                            usage=usage,
                        )
                    )
                    continue
                first_error = _tool_error_message(result)

            attempt_details.append(Attempt(action.name, action.arguments, first_error))
            log(
                self.logger,
                "warn",
                f"session {self.session_id} action {action.name} failed: {first_error}; retrying once",
            )
            self._emit(
                {
                    "event": "retry",
                    "action": action.name,
                    "args": action.arguments,
                    "error": first_error,
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Called {action.name}({json.dumps(action.arguments, separators=(',', ':'), ensure_ascii=False)})",
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "name": action.name,
                    "content": result.text if result is not None else first_error,
                    "isError": True,
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            f"Action {action.name} failed: {first_error}",
                            "Retry once with a different legal action (or different arguments).",
                            "Do not repeat the same failing call. Do not call wait or see.",
                            f"Current view was:\n{wait_result.text}",
                        ]
                    ),
                }
            )

            retry = await self._decide(action_tools, messages, usage)
            if retry.name in ("wait", "see"):
                error = f"Retry returned {retry.name} instead of an action (after: {first_error})"
                self._emit({"event": "turnFailed", "error": error})
                self.on_turn_result(
                    TurnReport(
                        ok=False,
                        error=error,
                        attempts=1,
                        attempt_details=attempt_details,
                        usage=usage,
                    )
                )
                continue

            self._emit({"event": "action", "action": retry.name, "args": retry.arguments})
            result = await self._call_tool(retry.name, retry.arguments)
            if not result.is_error:
                self.on_turn_result(
                    TurnReport(
                        ok=True,
                        action=retry.name,
                        args=retry.arguments,
                        result=result.json,
                        attempts=2,
                        usage=usage,
                    )
                )
                continue

            second_error = _tool_error_message(result)
            attempt_details.append(Attempt(retry.name, retry.arguments, second_error))
            log(self.logger, "warn", f"session {self.session_id} retry {retry.name} failed: {second_error}")
            self._emit(
                {
                    "event": "turnFailed",
                    "action": retry.name,
                    "args": retry.arguments,
                    "error": second_error,
                }
            )
            self.on_turn_result(
                TurnReport(
                    ok=False,
                    error=second_error,
                    attempts=2,
                    attempt_details=attempt_details,
                    usage=usage,
                )
            )

    async def _decide(
        self,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        usage: TokenUsage,
    ) -> ToolCall:
        decision = await self.model.decide(
            instructions=_compose_instructions(self.instructions, self.goal),
            tools=tools,
            messages=messages,
        )
        usage.prompt_tokens += decision.usage.prompt_tokens
        usage.completion_tokens += decision.usage.completion_tokens
        total = decision.usage.prompt_tokens + decision.usage.completion_tokens
        if self.on_usage is not None:
            maybe = self.on_usage(total)
            if maybe is not None:
                await maybe
        return decision.tool_call

    async def _call_tool(self, name: str, args: dict[str, Any]) -> ToolPayload:
        client = self._client
        if client is None:
            raise RuntimeError("Client not connected")
        return await client.call_tool(name, args)

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)


def loop_event_to_agent_event(session_id: str, event: dict[str, Any]) -> AgentEvent:
    name = event.get("event")
    if name == "action":
        return AgentEvent(
            type="action",
            session_id=session_id,
            action=event.get("action") or "unknown",
            args=dict(event.get("args") or {}),
        )
    if name == "retry":
        return AgentEvent(
            type="retry",
            session_id=session_id,
            action=event.get("action") or "unknown",
            args=dict(event.get("args") or {}),
            error=event.get("error") or "action failed",
        )
    action = event.get("action")
    args = event.get("args")
    return AgentEvent(
        type="turnFailed",
        session_id=session_id,
        error=event.get("error") or "turn failed",
        action=action if action else None,
        args=dict(args) if isinstance(args, dict) else None,
    )


def _compose_instructions(instructions: str, goal: str | None) -> str:
    if not goal:
        return instructions
    return f"{instructions}\n\nYour goal: {goal}"


def _tool_error_message(result: ToolPayload) -> str:
    if isinstance(result.json, dict) and "error" in result.json:
        return str(result.json["error"])
    return result.text


def create_scripted_model(script: list[ToolCall | dict[str, Any]]) -> ModelClient:
    """Return predetermined tool calls in order. When the queue is exhausted, always return wait."""
    calls = [ToolCall.from_value(item) for item in script]
    index = {"value": 0}

    class _Scripted:
        async def decide(
            self,
            *,
            instructions: str,
            tools: list[dict[str, Any]],
            messages: list[dict[str, Any]],
        ) -> ModelDecision:
            del instructions, tools, messages
            cursor = index["value"]
            index["value"] = cursor + 1
            call = calls[cursor] if cursor < len(calls) else ToolCall("wait", {})
            return ModelDecision(call, TokenUsage(1, 1))

    return _Scripted()
