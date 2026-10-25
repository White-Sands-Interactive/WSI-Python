"""Events emitted while a session acts, or while the platform connection changes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    """One action, retry, failure, or connection change.

    ``disconnected`` and ``reconnected`` have no session id. The other events
    do. ``type`` is ``action``, ``retry``, ``turnFailed``, ``disconnected``,
    or ``reconnected``.
    """

    type: str
    session_id: str | None = None
    action: str | None = None
    args: dict[str, Any] | None = None
    error: str | None = None
