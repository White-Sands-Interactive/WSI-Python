import asyncio
import tempfile
from pathlib import Path

from whitesands.agent_player import (
    AbortSignal,
    AgentServer,
    TurnFailedError,
    TurnResult,
    TurnTimeoutError,
)
from whitesands.agent_player.session_loop import SessionLoop, ToolCall
from whitesands.agent_player.testing import create_test_platform, loop_event_to_agent_event


class TinyWorld:
    def __init__(self) -> None:
        self.x = 0
        self.wood = 0
        self.fail_once = True

    def see(self):
        return {"x": self.x, "wood": self.wood}

    def move(self, dx):
        self.x += dx
        return {"x": self.x}

    def chop(self):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("axe slipped")
        self.wood += 1
        return {"wood": self.wood}

    def bad(self):
        raise RuntimeError("always bad")


class Gated:
    agent = {
        "actions": {
            "move": {"description": "Move", "args": {"dx": float}},
        }
    }

    def __init__(self) -> None:
        self.x = 0

    def see(self):
        return {"x": self.x}

    def move(self, dx):
        self.x += dx
        return {"x": self.x}


class Idle:
    async def start_session(self, request):
        del request

    async def stop_session(self, session_id):
        del session_id

    async def close(self):
        return None


def test_returns_the_action_taken_by_a_scripted_model():
    asyncio.run(_returns_the_action())


async def _returns_the_action():
    world = TinyWorld()
    events = []
    agents = AgentServer(
        inline=create_test_platform([{"name": "move", "arguments": {"dx": 3}}]),
        on_event=events.append,
        memory_path=False,
    )
    agent = await agents.start(world)
    result = await agent.turn()
    assert result.action == "move"
    assert result.args == {"dx": 3}
    assert result.attempts == 1
    assert result.usage.prompt_tokens == 1
    assert world.x == 3
    assert any(event.type == "action" and event.action == "move" for event in events)
    await agents.close()


def test_retries_once_after_a_rejected_action():
    asyncio.run(_retries_once())


async def _retries_once():
    world = TinyWorld()
    agents = AgentServer(
        inline=create_test_platform(
            [
                {"name": "chop", "arguments": {}},
                {"name": "chop", "arguments": {}},
            ]
        ),
        memory_path=False,
    )
    agent = await agents.start(world)
    result = await agent.turn()
    assert result.attempts == 2
    assert result.action == "chop"
    assert world.wood == 1
    await agents.close()


def test_turn_failed_error_lists_both_attempts():
    asyncio.run(_turn_failed())


async def _turn_failed():
    agents = AgentServer(
        inline=create_test_platform(
            [
                {"name": "bad", "arguments": {}},
                {"name": "bad", "arguments": {}},
            ]
        ),
        memory_path=False,
    )
    agent = await agents.start(TinyWorld())
    try:
        await agent.turn()
        raise AssertionError("expected TurnFailedError")
    except TurnFailedError as error:
        assert str(error) == "always bad"
        assert [(item.action, item.error) for item in error.attempts] == [
            ("bad", "always bad"),
            ("bad", "always bad"),
        ]
    await agents.close()


def test_runs_two_sessions_on_one_server():
    asyncio.run(_two_sessions())


async def _two_sessions():
    first = TinyWorld()
    second = TinyWorld()
    agents = AgentServer(
        inline=create_test_platform(
            [
                {"name": "move", "arguments": {"dx": 1}},
                {"name": "move", "arguments": {"dx": 5}},
            ]
        ),
        memory_path=False,
    )
    agent_a = await agents.start(first)
    agent_b = await agents.start(second)
    await agent_a.turn()
    await agent_b.turn()
    assert first.x == 1
    assert second.x == 5
    await agents.close()


def test_rejects_a_turn_that_exceeds_the_timeout():
    asyncio.run(_timeout())


async def _timeout():
    agents = AgentServer(inline=Idle(), turn_timeout_ms=20, memory_path=False)
    agent = await agents.start(TinyWorld())
    try:
        await agent.turn()
        raise AssertionError("expected TurnTimeoutError")
    except TurnTimeoutError:
        pass
    await agents.close()


def test_passes_goal_and_extra_instructions():
    asyncio.run(_goal())


async def _goal():
    seen = {}

    class Recording:
        async def start_session(self, request):
            seen["goal"] = request.goal
            seen["instructions"] = request.instructions

        async def stop_session(self, session_id):
            del session_id

        async def close(self):
            return None

    agents = AgentServer(inline=Recording(), memory_path=False)
    await agents.start(TinyWorld(), goal="Collect wood", instructions="Stay calm.")
    assert seen["goal"] == "Collect wood"
    assert "Stay calm." in seen["instructions"]
    assert "call wait first" in seen["instructions"]
    assert "done list" in seen["instructions"]
    await agents.close()


def test_schema_failure_is_a_failed_action_and_retries():
    asyncio.run(_schema_failure())


async def _schema_failure():
    world = Gated()
    agents = AgentServer(
        inline=create_test_platform(
            [
                {"name": "move", "arguments": {"dx": "nope"}},
                {"name": "move", "arguments": {"dx": 2}},
            ]
        ),
        memory_path=False,
    )
    agent = await agents.start(world)
    result = await agent.turn()
    assert result.attempts == 2
    assert world.x == 2
    await agents.close()


def test_reads_the_platform_key_from_the_environment(monkeypatch):
    monkeypatch.delenv("WSI_PLATFORM_KEY", raising=False)
    try:
        AgentServer()
        raise AssertionError("expected a missing key to fail")
    except RuntimeError as error:
        assert "WSI_PLATFORM_KEY" in str(error)
    monkeypatch.setenv("WSI_PLATFORM_KEY", "pk_test")
    agents = AgentServer(inline=Idle())
    assert isinstance(agents, AgentServer)


def test_shows_earlier_actions_on_the_next_view():
    asyncio.run(_earlier_actions())


async def _earlier_actions():
    views = []
    with tempfile.TemporaryDirectory() as directory:
        agents = AgentServer(
            memory_path=str(Path(directory) / "memory.sqlite"),
            inline=_platform_that_records_views(
                [
                    ToolCall("move", {"dx": 1}),
                    ToolCall("move", {"dx": 4}),
                ],
                views,
            ),
        )
        world = TinyWorld()
        agent = await agents.start(world, name="Ada")
        await agent.turn()
        await agent.turn()
        assert _done_from_prompt(views[0]) == []
        assert _done_from_prompt(views[1]) == [{"action": "move", "args": {"dx": 1}, "ok": True}]
        assert world.x == 5
        await agents.close()


def test_memory_off_omits_done_from_the_view():
    asyncio.run(_memory_off())


async def _memory_off():
    views = []
    agents = AgentServer(
        memory_path=False,
        inline=_platform_that_records_views([ToolCall("move", {"dx": 1})], views),
    )
    agent = await agents.start(TinyWorld(), name="Ada")
    await agent.turn()
    assert "done" not in _view_from_prompt(views[0])
    await agents.close()


def test_aborted_signal_rejects_the_turn():
    asyncio.run(_aborted())


async def _aborted():
    agents = AgentServer(inline=Idle(), memory_path=False)
    agent = await agents.start(TinyWorld())
    signal = AbortSignal()
    signal.abort("stop")
    try:
        await agent.turn(signal=signal)
        raise AssertionError("expected the abort")
    except RuntimeError as error:
        assert str(error) == "stop"
    await agents.close()


def test_turn_result_matches_a_scripted_call():
    asyncio.run(_result_shape())


async def _result_shape():
    agents = AgentServer(
        inline=create_test_platform([{"name": "move", "arguments": {"dx": 1}}]),
        memory_path=False,
    )
    world = TinyWorld()
    agent = await agents.start(world)
    result = await agent.turn()
    assert result == TurnResult(
        action="move",
        args={"dx": 1},
        result={"x": 1},
        attempts=1,
        usage=result.usage,
    )
    assert result.usage.completion_tokens == 1
    await agents.close()


def _platform_that_records_views(script, views):
    from whitesands.agent_player.protocol import TokenUsage
    from whitesands.agent_player.session_loop import ModelDecision

    index = {"value": 0}

    class _Model:
        async def decide(self, *, instructions, tools, messages):
            del instructions, tools
            prompt = next(message for message in messages if message["role"] == "user")
            views.append(prompt["content"])
            cursor = index["value"]
            index["value"] = cursor + 1
            call = script[cursor] if cursor < len(script) else ToolCall("wait", {})
            return ModelDecision(ToolCall.from_value(call), TokenUsage(1, 1))

    loops = {}

    class _Platform:
        async def start_session(self, request):
            def on_event(event):
                if request.on_event is not None:
                    request.on_event(loop_event_to_agent_event(request.session_id, event))

            loop = SessionLoop(
                session_id=request.session_id,
                transport=request.transport,
                instructions=request.instructions,
                goal=request.goal,
                model=_Model(),
                on_turn_result=request.on_turn_result,
                on_event=on_event,
            )
            loops[request.session_id] = loop
            await loop.start()

        async def stop_session(self, session_id):
            loop = loops.pop(session_id, None)
            if loop is not None:
                await loop.stop()

        async def close(self):
            running = list(loops.values())
            loops.clear()
            for loop in running:
                await loop.stop()

    return _Platform()


def _view_from_prompt(prompt):
    import json

    start = prompt.index("{")
    end = prompt.rindex("}")
    return json.loads(prompt[start : end + 1])


def _done_from_prompt(prompt):
    return _view_from_prompt(prompt)["done"]
