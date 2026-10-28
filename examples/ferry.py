"""River crossing. Offline by default, replaying one solution.

Remote: PLATFORM_KEY=pk_... python examples/ferry.py --remote
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Literal

from whitesands.agent_player import AgentServer, TurnFailedError
from whitesands.agent_player.testing import create_test_platform

PASSENGERS = ("wolf", "goat", "cabbage")
Cargo = Literal["wolf", "goat", "cabbage", "none"]
SOLUTION = ("goat", "none", "cabbage", "goat", "wolf", "none", "goat")
GOAL = (
    "Get the wolf, the goat, and the cabbage onto the far bank. "
    "Each turn, cross with a passenger from the take list. "
    "none means cross alone."
)


class Ferryman:
    agent = {
        "description": "A ferryman moving a wolf, a goat, and a cabbage across a river",
        "actions": {
            "cross": {
                "description": (
                    "Cross to the other bank. passenger must be one value from take in the view. "
                    "none crosses alone."
                ),
                "args": {"passenger": Cargo},
            },
        },
    }

    def __init__(self) -> None:
        self.boat = "near"
        self._near = ["wolf", "goat", "cabbage"]
        self._far: list[str] = []
        self._last_passenger: str | None = None

    def see(self):
        return {
            "boat": self.boat,
            "near": _listed(self._near),
            "far": _listed(self._far),
            "take": self._take(),
            "solved": self.boat == "far" and len(self._far) == len(PASSENGERS),
            "rule": "Call cross with passenger set to a value in take. none crosses alone.",
        }

    def cross(self, passenger: str):
        take = self._take()
        if passenger not in take:
            raise RuntimeError(f"passenger must be one of: {', '.join(take)}")
        origin = self._near if self.boat == "near" else self._far
        destination = self._far if self.boat == "near" else self._near
        if passenger != "none":
            origin.pop(origin.index(passenger))
            destination.append(passenger)
        self.boat = "far" if self.boat == "near" else "near"
        self._last_passenger = passenger
        return {
            "boat": self.boat,
            "near": _listed(self._near),
            "far": _listed(self._far),
            "take": self._take(),
        }

    def _take(self) -> list[str]:
        origin = self._near if self.boat == "near" else self._far
        alone = _unsupervised_danger(list(origin)) is None and self._last_passenger != "none"
        if "wolf" in origin and "cabbage" in origin and "goat" not in origin and alone:
            return ["none"]
        choices = [
            passenger
            for passenger in _listed(origin)
            if passenger != self._last_passenger
            and _unsupervised_danger([item for item in origin if item != passenger]) is None
        ]
        if not choices and alone:
            return ["none"]
        return choices


def render(ferry: Ferryman) -> str:
    view = ferry.see()
    boat = "boat ~ ~ ~ ~" if view["boat"] == "near" else "~ ~ ~ ~ boat"
    near = " ".join(view["near"])
    far = " ".join(view["far"])
    return f"near [{near}]  {boat}  far [{far}]"


def _listed(items: list[str]) -> list[str]:
    return [passenger for passenger in PASSENGERS if passenger in items]


def _unsupervised_danger(left_behind: list[str]) -> str | None:
    wolf = "wolf" in left_behind
    goat = "goat" in left_behind
    cabbage = "cabbage" in left_behind
    if wolf and goat:
        return "The wolf would eat the goat on the bank you are leaving"
    if goat and cabbage:
        return "The goat would eat the cabbage on the bank you are leaving"
    return None


async def main() -> None:
    use_remote = "--remote" in sys.argv
    ferry = Ferryman()
    print(render(ferry))
    if use_remote:
        agents = AgentServer(platform_key=_required("PLATFORM_KEY"))
    else:
        agents = AgentServer(
            inline=create_test_platform(
                [{"name": "cross", "arguments": {"passenger": passenger}} for passenger in SOLUTION]
            )
        )

    agent = await agents.start(ferry, name="Ferryman", goal=GOAL)
    limit = 12 if use_remote else len(SOLUTION)
    turn = 1
    while turn <= limit and not ferry.see()["solved"]:
        try:
            result = await agent.turn()
            print(f"\n{turn}. {result.action}({result.args}) attempts={result.attempts}")
            print(render(ferry))
        except Exception as error:
            failed = isinstance(error, TurnFailedError)
            extra = f" after {len(error.attempts)} attempt(s)" if failed else ""
            print(f"\n{turn}. turn failed{extra}: {error}")
            print(render(ferry))
            break
        turn += 1

    await agent.stop()
    await agents.close()
    if not ferry.see()["solved"]:
        raise RuntimeError("The wolf, goat, and cabbage did not all reach the far bank")
    print("\nOK — everyone is on the far bank.")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for --remote")
    return value


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
