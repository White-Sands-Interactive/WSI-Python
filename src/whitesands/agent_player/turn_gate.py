"""Coordinates game-side turn() with the agent's wait tool.

turn() releases wait, then resolves when the platform reports the turn finished.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from whitesands.agent_player.errors import TurnTimeoutError
from whitesands.agent_player.protocol import TurnReport


class AbortSignal:
    """Cancel a turn() wait. abort() unblocks the wait with reason."""

    def __init__(self) -> None:
        self.aborted = False
        self.reason: BaseException | None = None
        self._listeners: list[Callable[[], None]] = []

    def abort(self, reason: BaseException | str | None = None) -> None:
        if self.aborted:
            return
        self.aborted = True
        if isinstance(reason, BaseException):
            self.reason = reason
        elif isinstance(reason, str) and reason:
            self.reason = RuntimeError(reason)
        else:
            self.reason = RuntimeError("turn aborted")
        for listener in list(self._listeners):
            listener()

    def add_abort_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove


def _abort_error(signal: Any) -> BaseException:
    reason = getattr(signal, "reason", None)
    if isinstance(reason, BaseException):
        return reason
    if isinstance(reason, str) and reason:
        return RuntimeError(reason)
    return RuntimeError("turn aborted")


class TurnGate:
    def __init__(self) -> None:
        self._wait_future: asyncio.Future[None] | None = None
        self._turn_future: asyncio.Future[TurnReport] | None = None
        self._released = False

    async def wait_for_turn(self) -> None:
        """Called by the wait tool. Blocks until turn() releases this session."""
        if self._released:
            self._released = False
            return
        if self._wait_future is None or self._wait_future.done():
            self._wait_future = asyncio.get_running_loop().create_future()
        await self._wait_future
        self._wait_future = None
        self._released = False

    async def run_turn(self, *, timeout_ms: int | None = None, signal: Any = None) -> TurnReport:
        """Unblock wait and wait for the platform's turn report."""
        if self._turn_future is not None and not self._turn_future.done():
            raise RuntimeError("turn() already in progress for this session")
        if signal is not None and getattr(signal, "aborted", False):
            raise _abort_error(signal)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[TurnReport] = loop.create_future()
        self._turn_future = future
        timeout_handle: asyncio.TimerHandle | None = None
        remove_abort: Callable[[], None] | None = None

        def reject(error: BaseException) -> None:
            if not future.done():
                future.set_exception(error)

        if timeout_ms is not None:
            def on_timeout() -> None:
                reject(TurnTimeoutError(f"turn() timed out after {timeout_ms}ms"))

            timeout_handle = loop.call_later(timeout_ms / 1000, on_timeout)

        if signal is not None:
            def on_abort() -> None:
                reject(_abort_error(signal))

            if getattr(signal, "aborted", False):
                reject(_abort_error(signal))
            else:
                add = getattr(signal, "add_abort_listener", None)
                if callable(add):
                    remove_abort = add(on_abort)

        self._release_wait()
        try:
            return await future
        finally:
            if timeout_handle is not None:
                timeout_handle.cancel()
            if remove_abort is not None:
                remove_abort()
            if self._turn_future is future:
                self._turn_future = None

    def complete_turn(self, outcome: TurnReport) -> None:
        """Platform finished one action cycle (success or second failure)."""
        future = self._turn_future
        if future is None or future.done():
            return
        future.set_result(outcome)

    def fail_turn(self, error: BaseException) -> None:
        """Fail an in-flight turn, for example when the connection cannot be resumed."""
        future = self._turn_future
        if future is None or future.done():
            return
        future.set_exception(error)

    def reset(self) -> None:
        self._release_wait()
        self.complete_turn(TurnReport(ok=False, error="session stopped"))
        self._wait_future = None
        self._released = False

    def _release_wait(self) -> None:
        self._released = True
        future = self._wait_future
        self._wait_future = None
        if future is not None and not future.done():
            future.set_result(None)
