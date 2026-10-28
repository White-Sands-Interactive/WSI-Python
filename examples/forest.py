"""Two villagers on a small map. Offline by default.

Remote: PLATFORM_KEY=pk_... python examples/forest.py --remote
"""

from __future__ import annotations

import asyncio
import os
import sys

from whitesands.agent_player import AgentServer
from whitesands.agent_player.testing import create_test_platform


class Forest:
    def __init__(self, width: int = 5, height: int = 5) -> None:
        self.width = width
        self.height = height
        self._tiles = ["."] * (width * height)
        self.set(2, 2, "T")

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def get(self, x: int, y: int) -> str:
        if not self.in_bounds(x, y):
            raise RuntimeError(f"Out of bounds: {x},{y}")
        return self._tiles[y * self.width + x]

    def set(self, x: int, y: int, tile: str) -> None:
        if not self.in_bounds(x, y):
            raise RuntimeError(f"Out of bounds: {x},{y}")
        self._tiles[y * self.width + x] = tile

    def trees_left(self) -> int:
        return sum(1 for tile in self._tiles if tile == "T")

    def render(self) -> str:
        rows = []
        for y in range(self.height):
            rows.append("".join(self.get(x, y) for x in range(self.width)))
        return "\n".join(rows)


class Villager:
    def __init__(self, world: Forest, name: str, x: int = 0, y: int = 0) -> None:
        self.world = world
        self.name = name
        self.x = x
        self.y = y
        self.wood = 0

    def see(self):
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "wood": self.wood,
            "tile": self.world.get(self.x, self.y),
            "treesLeft": self.world.trees_left(),
            "bounds": {
                "minX": 0,
                "minY": 0,
                "maxX": self.world.width - 1,
                "maxY": self.world.height - 1,
            },
            "hint": "move(dx,dy) changes position by integer steps; stay in bounds. chop() only works on tile T.",
            "map": self.world.render(),
        }

    def move(self, dx, dy):
        next_x = self.x + dx
        next_y = self.y + dy
        if not self.world.in_bounds(next_x, next_y):
            raise RuntimeError(f"Cannot move to {next_x},{next_y}")
        self.x = next_x
        self.y = next_y
        return {"x": self.x, "y": self.y, "tile": self.world.get(self.x, self.y)}

    def chop(self):
        if self.world.get(self.x, self.y) != "T":
            raise RuntimeError(f"No tree at {self.x},{self.y}")
        self.world.set(self.x, self.y, ".")
        self.wood += 1
        return {"wood": self.wood, "treesLeft": self.world.trees_left()}

    def _secret(self):
        return "nope"


async def main() -> None:
    use_remote = "--remote" in sys.argv
    world = Forest()
    ada = Villager(world, "Ada", 0, 2)
    bea = Villager(world, "Bea", 4, 4)

    print("Before:")
    print(world.render())
    print(f"Ada at ({ada.x},{ada.y}) wood={ada.wood}")

    if use_remote:
        agents = AgentServer(platform_key=_required("PLATFORM_KEY"))
    else:
        agents = AgentServer(
            inline=create_test_platform(
                [
                    {"name": "move", "arguments": {"dx": 2, "dy": 0}},
                    {"name": "chop", "arguments": {}},
                    {"name": "move", "arguments": {"dx": -1, "dy": 0}},
                ]
            )
        )

    ada_agent = await agents.start(ada, goal="Stand on the tree and chop it")
    bea_agent = await agents.start(bea)

    moved = await ada_agent.turn()
    print(f"\nAda {moved.action}({moved.args})")
    print(f"Ada at ({ada.x},{ada.y}) tile={world.get(ada.x, ada.y)} wood={ada.wood}")

    chopped = await ada_agent.turn()
    print(f"\nAda {chopped.action}({chopped.args})")
    print(world.render())
    print(f"Ada at ({ada.x},{ada.y}) wood={ada.wood} treesLeft={world.trees_left()}")

    try:
        bea_turn = await bea_agent.turn()
        print(f"\nBea {bea_turn.action}({bea_turn.args})")
        print(f"Bea at ({bea.x},{bea.y})")
    except Exception as error:
        print(f"\nBea turn failed (model chose an illegal action): {error}")
        print(f"Bea still at ({bea.x},{bea.y})")

    await ada_agent.stop()
    await bea_agent.stop()
    await agents.close()

    if not use_remote and (moved.action != "move" or chopped.action != "chop"):
        raise RuntimeError("Scripted forest did not move then chop")
    if ada.x != 2 or ada.y != 2 or ada.wood != 1 or world.trees_left() != 0:
        raise RuntimeError("Forest demo did not produce expected Ada state")
    if not use_remote and (bea.x != 3 or bea.y != 4):
        raise RuntimeError("Forest demo did not produce expected Bea state")
    print("\nOK — Ada moved and chopped; tile and wood changed.")


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
