"""Typed failures raised by AgentServer."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from whitesands.agent_player.protocol import Attempt


class PlatformAuthError(Exception):
    """The platform rejected the key."""


class ProtocolVersionError(Exception):
    """This SDK and the platform disagree on the protocol."""


class TurnFailedError(Exception):
    """The action and the retry both failed."""

    def __init__(self, message: str, attempts: list[Attempt] | None = None) -> None:
        super().__init__(message)
        self.attempts = list(attempts or [])


class TurnTimeoutError(Exception):
    """The platform did not finish within the turn timeout."""


class ConnectionLostError(Exception):
    """The socket dropped and the session could not be resumed."""

    def __init__(self, message: str = "Connection to the platform was lost") -> None:
        super().__init__(message)
