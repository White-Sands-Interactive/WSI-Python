import asyncio

from whitesands.agent_player.protocol import TurnReport
from whitesands.agent_player.turn_gate import TurnGate


def test_pairs_wait_with_turn():
    asyncio.run(_pairs_wait_with_turn())


async def _pairs_wait_with_turn():
    gate = TurnGate()
    events = []

    async def waiting():
        await gate.wait_for_turn()
        events.append("wait-released")

    waiter = asyncio.create_task(waiting())
    await asyncio.sleep(0)
    assert events == []

    async def turning():
        outcome = await gate.run_turn()
        events.append(f"turn-{'ok' if outcome.ok else 'fail'}")
        return outcome

    turn = asyncio.create_task(turning())
    await waiter
    assert events == ["wait-released"]
    gate.complete_turn(TurnReport(ok=True))
    await turn
    assert events == ["wait-released", "turn-ok"]
