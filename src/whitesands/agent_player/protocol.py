"""Wire protocol between AgentServer and the platform. Version 1.

The platform's copy is internal/protocol in White-Sands-Interactive/Platform.
The Node and C# SDKs speak this same version. A change to the wire format has
to land in every copy. Game code uses AgentServer and does not import this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
SDK_VERSION = "1.0.0"

# How long a disconnected session can be resumed. Shared by both sides.
SESSION_RESUME_GRACE_MS = 30_000

# MCP revision echoed during initialize. Matches the platform client.
MCP_PROTOCOL_VERSION = "2025-06-18"

DEFAULT_PLATFORM_URL = "ws://127.0.0.1:8787"

SEE_DESCRIPTION = (
    "Read the current game view for this player. Prefer wait, which returns this after the game grants a turn."
)
WAIT_DESCRIPTION = "Block until the game calls turn() for this session, then return the current see() view."

DEFAULT_AGENT_INSTRUCTIONS = "\n".join(
    [
        "You are a remote game agent. Tools are provided by the game.",
        "On every turn: call wait first. It blocks until the game grants your turn and then returns the current view from see.",
        "After wait returns, call exactly one action tool (not see, not wait).",
        "The view's done list is what this character has already called in this session, oldest first. Use it when choosing the next action.",
        "If an action tool returns an error, retry once with a corrected action. A second failure ends the turn as failed.",
    ]
)


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class Attempt:
    action: str
    args: dict[str, Any]
    error: str


@dataclass
class TurnReport:
    ok: bool
    error: str | None = None
    action: str | None = None
    args: dict[str, Any] | None = None
    result: Any = None
    attempts: int = 0
    attempt_details: list[Attempt] = field(default_factory=list)
    usage: TokenUsage | None = None
    connection_lost: bool = False


def turn_report_from_wire(data: dict[str, Any]) -> TurnReport:
    """Read a turn_result object. Wire fields stay camelCase."""
    usage_raw = data.get("usage")
    usage = None
    if isinstance(usage_raw, dict):
        usage = TokenUsage(
            int(usage_raw.get("promptTokens") or 0),
            int(usage_raw.get("completionTokens") or 0),
        )
    details: list[Attempt] = []
    for item in data.get("attemptDetails") or []:
        if not isinstance(item, dict):
            continue
        args = item.get("args")
        details.append(
            Attempt(
                action=str(item.get("action") or ""),
                args=dict(args) if isinstance(args, dict) else {},
                error=str(item.get("error") or ""),
            )
        )
    args = data.get("args")
    return TurnReport(
        ok=bool(data.get("ok")),
        error=data.get("error") if isinstance(data.get("error"), str) else None,
        action=data.get("action") if isinstance(data.get("action"), str) else None,
        args=dict(args) if isinstance(args, dict) else None,
        result=data.get("result"),
        attempts=int(data.get("attempts") or 0),
        attempt_details=details,
        usage=usage,
        connection_lost=bool(data.get("connectionLost")),
    )
