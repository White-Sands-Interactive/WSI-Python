"""Start a remote game agent from a player class.

The SDK publishes the class as tools. The model runs on the platform, so the
game process never holds the model key.
"""

from whitesands.agent_player.agent_server import AgentHandle, AgentServer, TurnResult
from whitesands.agent_player.errors import (
    ConnectionLostError,
    PlatformAuthError,
    ProtocolVersionError,
    TurnFailedError,
    TurnTimeoutError,
)
from whitesands.agent_player.events import AgentEvent
from whitesands.agent_player.protocol import Attempt, TokenUsage
from whitesands.agent_player.turn_gate import AbortSignal

__version__ = "1.0.0"

__all__ = [
    "AbortSignal",
    "AgentEvent",
    "AgentHandle",
    "AgentServer",
    "Attempt",
    "ConnectionLostError",
    "PlatformAuthError",
    "ProtocolVersionError",
    "TokenUsage",
    "TurnFailedError",
    "TurnResult",
    "TurnTimeoutError",
]
