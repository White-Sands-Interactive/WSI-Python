import asyncio
import json
import threading

from whitesands.agent_player import (
    AgentServer,
    PlatformAuthError,
    ProtocolVersionError,
    TurnFailedError,
    TurnResult,
)
from whitesands.agent_player.protocol import PROTOCOL_VERSION, TokenUsage

PLAYER_ID = "3f6c2d1e-8b7a-4c5d-9e0f-1a2b3c4d5e6f"


class Pawn:
    def see(self):
        return {}

    def move(self, dx):
        return {"dx": dx}


def test_maps_hello_err_codes_to_typed_errors():
    asyncio.run(_hello_errors())


async def _hello_errors():
    platform = await _fake_platform({"code": "PROTOCOL", "message": "upgrade"})
    outdated = AgentServer(platform_key="pk_test", memory_path=False)
    outdated._platform_url = platform["url"]
    try:
        await outdated.start(Pawn())
        raise AssertionError("expected ProtocolVersionError")
    except ProtocolVersionError:
        pass
    finally:
        await outdated.close()
    assert platform["received"][0]["type"] == "hello"
    assert platform["received"][0]["protocolVersion"] == PROTOCOL_VERSION
    await platform["close"]()

    platform = await _fake_platform({"code": "AUTH", "message": "bad key"})
    unauthorized = AgentServer(platform_key="pk_wrong", memory_path=False)
    unauthorized._platform_url = platform["url"]
    try:
        await unauthorized.start(Pawn())
        raise AssertionError("expected PlatformAuthError")
    except PlatformAuthError:
        pass
    finally:
        await unauthorized.close()
    await platform["close"]()


def test_reconnects_and_finishes_the_turn_on_the_new_socket():
    asyncio.run(_reconnects())


async def _reconnects():
    platform = await _fake_platform()
    events = []
    agents = AgentServer(
        platform_key="pk_test",
        on_event=lambda event: events.append(event.type),
        memory_path=False,
    )
    agents._platform_url = platform["url"]
    try:
        agent = await agents.start(Pawn(), goal="Step", player_id=PLAYER_ID)
        started = next(item for item in platform["received"] if item["type"] == "start_session")
        assert started["goal"] == "Step"
        assert started["playerId"] == PLAYER_ID

        _abort(platform["sockets"][0])
        await _wait_for(lambda: "reconnected" in events)
        assert "disconnected" in events
        assert len(platform["sockets"]) == 2
        assert {
            "type": "resume_session",
            "sessionId": started["sessionId"],
            "sessionToken": started["sessionToken"],
        } in platform["received"]
        assert all("playerId" not in item for item in platform["received"] if item["type"] == "resume_session")

        turn = asyncio.create_task(agent.turn(timeout_ms=5_000))
        await asyncio.sleep(0.05)
        await platform["sockets"][-1].send(
            json.dumps(
                {
                    "type": "turn_result",
                    "sessionId": agent.session_id,
                    "ok": True,
                    "action": "move",
                    "args": {"dx": 1},
                    "result": {"dx": 1},
                    "attempts": 1,
                    "usage": {"promptTokens": 3, "completionTokens": 2},
                }
            )
        )
        assert await turn == TurnResult(
            action="move",
            args={"dx": 1},
            result={"dx": 1},
            attempts=1,
            usage=TokenUsage(3, 2),
        )
    finally:
        await agents.close()
        await platform["close"]()


def test_interrupt_unblocks_a_start_waiting_for_hello():
    asyncio.run(_interrupt_during_hello())


async def _interrupt_during_hello():
    platform = await _fake_platform(hold_hello=True)
    events = []
    agents = AgentServer(
        platform_key="pk_test",
        on_event=lambda event: events.append(event.type),
        memory_path=False,
    )
    agents._platform_url = platform["url"]
    started = asyncio.create_task(agents.start(Pawn()))
    try:
        await _wait_for(lambda: any(item.get("type") == "hello" for item in platform["received"]))
        _interrupt(agents)
        await _wait_for(lambda: agents._connection is not None and agents._connection._closed)
        try:
            await asyncio.wait_for(started, timeout=2)
            raise AssertionError("expected start() to return")
        except Exception as error:
            assert _closed_error(error)
        await asyncio.sleep(0.6)
        assert len(platform["sockets"]) == 1
        assert "reconnected" not in events
        assert agents._sessions == {}
    finally:
        started.cancel()
        await agents.close()
        await platform["close"]()


def test_interrupt_unblocks_a_start_waiting_for_session_started():
    asyncio.run(_interrupt_during_session_start())


async def _interrupt_during_session_start():
    platform = await _fake_platform(hold_start=True)
    agents = AgentServer(
        platform_key="pk_test",
        memory_path=False,
    )
    agents._platform_url = platform["url"]
    started = asyncio.create_task(agents.start(Pawn(), player_id=PLAYER_ID))
    try:
        await _wait_for(lambda: any(item.get("type") == "start_session" for item in platform["received"]))
        _interrupt(agents)
        await _wait_for(lambda: agents._connection is not None and agents._connection._closed)
        try:
            await asyncio.wait_for(started, timeout=2)
            raise AssertionError("expected start() to return")
        except RuntimeError as error:
            assert "closed" in str(error).lower()
        assert len(agents._sessions) == 1
    finally:
        started.cancel()
        await agents.close()
        assert agents._sessions == {}
        await platform["close"]()


def test_a_platform_session_requires_a_character_id():
    asyncio.run(_requires_character_id())


async def _requires_character_id():
    platform = await _fake_platform()
    missing = AgentServer(platform_key="pk_test", memory_path=False)
    missing._platform_url = platform["url"]
    try:
        await missing.start(Pawn())
        raise AssertionError("expected a missing player id to fail")
    except RuntimeError as error:
        assert str(error) == "playerId is required. Copy the character ID from the web app"
    finally:
        await missing.close()
    assert all(item.get("type") != "start_session" for item in platform["received"])
    await platform["close"]()

    platform = await _fake_platform()
    invalid = AgentServer(platform_key="pk_test", memory_path=False)
    invalid._platform_url = platform["url"]
    try:
        await invalid.start(Pawn(), player_id="  not-a-character  ")
        raise AssertionError("expected a non-uuid player id to fail")
    except RuntimeError as error:
        assert (
            str(error)
            == 'playerId "not-a-character" is not a character ID. Copy the ID from the character in the web app'
        )
    finally:
        await invalid.close()
    assert all(item.get("type") != "start_session" for item in platform["received"])
    await platform["close"]()

    platform = await _fake_platform()
    agents = AgentServer(platform_key="pk_test", memory_path=False)
    agents._platform_url = platform["url"]
    try:
        agent = await agents.start(Pawn(), player_id="  " + PLAYER_ID + "  ")
        started = next(item for item in platform["received"] if item["type"] == "start_session")
        assert started["playerId"] == PLAYER_ID
        await agent.stop()
    finally:
        await agents.close()
        await platform["close"]()

    from whitesands.agent_player.testing import create_test_platform

    offline = AgentServer(
        inline=create_test_platform([{"name": "move", "arguments": {"dx": 1}}]),
        memory_path=False,
    )
    agent = await offline.start(Pawn())
    result = await agent.turn()
    assert result.action == "move"
    await offline.close()


def test_interrupt_leaves_a_waiting_turn_until_close():
    asyncio.run(_interrupt_leaves_the_turn())


async def _interrupt_leaves_the_turn():
    platform = await _fake_platform()
    agents = AgentServer(
        platform_key="pk_test",
        memory_path=False,
    )
    agents._platform_url = platform["url"]
    agent = await agents.start(Pawn(), player_id=PLAYER_ID)
    turn = asyncio.create_task(agent.turn(timeout_ms=5_000))
    try:
        await asyncio.sleep(0.05)
        _interrupt(agents)
        await _wait_for(lambda: agents._connection is not None and agents._connection._closed)
        await asyncio.sleep(0.05)
        assert not turn.done()
        assert agent.session_id in agents._sessions
        await agents.close()
        assert agent.session_id not in agents._sessions
        try:
            await turn
            raise AssertionError("expected the stopped turn to fail")
        except TurnFailedError as error:
            assert "session stopped" in str(error)
    finally:
        if not turn.done():
            turn.cancel()
        await agents.close()
        await platform["close"]()


def _interrupt(agents: AgentServer) -> None:
    thread = threading.Thread(target=agents.interrupt)
    thread.start()
    thread.join()


def _closed_error(error: BaseException) -> bool:
    return "closed" in str(error).lower() or type(error).__name__.startswith("ConnectionClosed")


async def _fake_platform(hello_err=None, *, hold_hello=False, hold_start=False):
    from websockets.asyncio.server import serve

    received = []
    sockets = []

    async def handler(ws):
        sockets.append(ws)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                envelope = json.loads(raw)
                received.append(envelope)
                kind = envelope.get("type")
                if kind == "hello":
                    if hold_hello:
                        continue
                    if hello_err:
                        await ws.send(json.dumps({"type": "hello_err", **hello_err}))
                    else:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "hello_ok",
                                    "accountName": "fake",
                                    "protocolVersion": PROTOCOL_VERSION,
                                }
                            )
                        )
                elif kind == "start_session":
                    if hold_start:
                        continue
                    await ws.send(json.dumps({"type": "session_started", "sessionId": envelope["sessionId"]}))
                elif kind == "resume_session":
                    await ws.send(json.dumps({"type": "session_resumed", "sessionId": envelope["sessionId"]}))
                elif kind == "stop_session":
                    await ws.send(json.dumps({"type": "session_stopped", "sessionId": envelope["sessionId"]}))
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "id": envelope["id"]}))
        except Exception:
            return

    server = await serve(handler, "127.0.0.1", 0, ping_interval=None)
    port = server.sockets[0].getsockname()[1]

    async def close():
        server.close()
        await server.wait_closed()

    return {"url": f"ws://127.0.0.1:{port}", "received": received, "sockets": sockets, "close": close}


def _abort(ws):
    transport = getattr(ws, "transport", None)
    if transport is not None:
        transport.abort()
        return
    ws.close()


async def _wait_for(ready, timeout=5):
    start = asyncio.get_running_loop().time()
    while not ready():
        if asyncio.get_running_loop().time() - start > timeout:
            raise TimeoutError("timed out")
        await asyncio.sleep(0.01)
