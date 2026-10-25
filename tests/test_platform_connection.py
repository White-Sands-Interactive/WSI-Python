import asyncio
import json

from whitesands.agent_player import (
    AgentServer,
    PlatformAuthError,
    ProtocolVersionError,
    TurnResult,
)
from whitesands.agent_player.protocol import PROTOCOL_VERSION, TokenUsage


class Pawn:
    def see(self):
        return {}

    def move(self, dx):
        return {"dx": dx}


def test_maps_hello_err_codes_to_typed_errors():
    asyncio.run(_hello_errors())


async def _hello_errors():
    platform = await _fake_platform({"code": "PROTOCOL", "message": "upgrade"})
    outdated = AgentServer(platform_key="pk_test", platform_url=platform["url"], memory_path=False)
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
    unauthorized = AgentServer(platform_key="pk_wrong", platform_url=platform["url"], memory_path=False)
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
        platform_url=platform["url"],
        on_event=lambda event: events.append(event.type),
        memory_path=False,
    )
    try:
        agent = await agents.start(Pawn(), goal="Step")
        started = next(item for item in platform["received"] if item["type"] == "start_session")
        assert started["goal"] == "Step"

        _abort(platform["sockets"][0])
        await _wait_for(lambda: "reconnected" in events)
        assert "disconnected" in events
        assert len(platform["sockets"]) == 2
        assert {
            "type": "resume_session",
            "sessionId": started["sessionId"],
            "sessionToken": started["sessionToken"],
        } in platform["received"]

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


async def _fake_platform(hello_err=None):
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
