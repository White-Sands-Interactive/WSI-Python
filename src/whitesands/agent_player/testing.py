"""In-process platform that replays a script of tool calls.

Same session loop as production. No network and no platform key.
"""

from __future__ import annotations

from typing import Any

from whitesands.agent_player.session_loop import (
    ModelClient,
    ModelDecision,
    SessionLoop,
    ToolCall,
    create_scripted_model,
    loop_event_to_agent_event,
)

__all__ = [
    "ModelClient",
    "ModelDecision",
    "SessionLoop",
    "ToolCall",
    "create_scripted_model",
    "create_test_platform",
    "loop_event_to_agent_event",
]


def create_test_platform(script: list[ToolCall | dict[str, Any]]) -> Any:
    """Build an inline platform. Every session shares the script, in order."""
    model = create_scripted_model(script)
    loops: dict[str, SessionLoop] = {}

    class _TestPlatform:
        async def start_session(self, request: Any) -> None:
            def on_event(event: dict[str, Any]) -> None:
                if request.on_event is not None:
                    request.on_event(loop_event_to_agent_event(request.session_id, event))

            loop = SessionLoop(
                session_id=request.session_id,
                transport=request.transport,
                instructions=request.instructions,
                goal=request.goal,
                model=model,
                logger=request.logger,
                on_usage=request.on_usage,
                on_turn_result=request.on_turn_result,
                on_event=on_event if request.on_event is not None else None,
            )
            loops[request.session_id] = loop
            await loop.start()

        async def stop_session(self, session_id: str) -> None:
            loop = loops.pop(session_id, None)
            if loop is not None:
                await loop.stop()

        async def close(self) -> None:
            running = list(loops.values())
            loops.clear()
            for loop in running:
                await loop.stop()

    return _TestPlatform()
