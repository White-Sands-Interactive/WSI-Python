"""A courier with a declared integer argument. Offline.

python examples/courier.py
"""

from __future__ import annotations

import asyncio
import sys

from whitesands.agent_player import AgentServer
from whitesands.agent_player.testing import create_test_platform

DOOR = 3


class Courier:
    agent = {
        "description": "A courier carrying one parcel along a street",
        "actions": {
            "walk": {
                "description": "Move dx steps. Positive is toward the door.",
                "args": {"dx": int},
            },
            "deliver": {
                "description": f"Hand over the parcel. Only works at x = {DOOR}.",
            },
        },
    }

    def __init__(self, x: int = 0) -> None:
        self.x = x
        self.delivered = False

    def see(self):
        return {"x": self.x, "door": DOOR, "carrying": not self.delivered}

    def walk(self, dx):
        nxt = self.x + dx
        if nxt < 0 or nxt > DOOR:
            raise RuntimeError(f"Cannot walk to {nxt}; the street is 0..{DOOR}")
        self.x = nxt
        return {"x": self.x}

    def deliver(self):
        if self.x != DOOR:
            raise RuntimeError(f"The door is at {DOOR}, you are at {self.x}")
        if self.delivered:
            raise RuntimeError("Already delivered")
        self.delivered = True
        return {"delivered": True}


async def main() -> None:
    nia = Courier(0)
    agents = AgentServer(
        inline=create_test_platform(
            [
                {"name": "walk", "arguments": {"dx": 3}},
                {"name": "deliver", "arguments": {}},
            ]
        )
    )
    agent = await agents.start(nia, name="Nia", goal="Deliver the parcel to the door")
    walked = await agent.turn()
    delivered = await agent.turn()
    print(walked.action, walked.args, "->", walked.result)
    print(delivered.action, "->", delivered.result)
    await agents.close()

    if nia.x != 3 or not nia.delivered:
        raise RuntimeError("Courier did not deliver the parcel")
    if walked.attempts != 1 or delivered.action != "deliver":
        raise RuntimeError("Unexpected turn results")
    print("OK — parcel delivered.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
