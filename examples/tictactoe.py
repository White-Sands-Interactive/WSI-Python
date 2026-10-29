"""Tic-tac-toe. You play X by typing a cell. The agent plays O by calling place.

python examples/tictactoe.py
Remote: PLATFORM_KEY=pk_... python examples/tictactoe.py --remote
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from whitesands.agent_player import AgentServer, TokenUsage, TurnFailedError
from whitesands.agent_player.testing import (
    ModelDecision,
    SessionLoop,
    ToolCall,
    loop_event_to_agent_event,
)

LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)
GOAL = (
    "Win as O. Call place with one empty cell. "
    "Cells are 1 through 9, left to right, top to bottom. "
    "Take a win when you have one. Otherwise stop X from winning."
)


class Grid:
    def __init__(self) -> None:
        self.cells = [" "] * 9

    def empty(self) -> list[int]:
        return [index + 1 for index, mark in enumerate(self.cells) if mark == " "]

    def winner(self) -> str | None:
        for line in LINES:
            marks = [self.cells[index] for index in line]
            if marks[0] != " " and marks[0] == marks[1] == marks[2]:
                return marks[0]
        return None

    def over(self) -> bool:
        return self.winner() is not None or not self.empty()

    def place(self, cell: int, mark: str) -> None:
        if isinstance(cell, bool) or not isinstance(cell, int) or cell < 1 or cell > 9:
            raise RuntimeError("cell must be an integer from 1 to 9")
        if self.over():
            raise RuntimeError("The game is over")
        if self.cells[cell - 1] != " ":
            raise RuntimeError(f"Cell {cell} is taken")
        self.cells[cell - 1] = mark

    def render(self) -> str:
        shown = [self.cells[index] if self.cells[index] != " " else str(index + 1) for index in range(9)]
        rows = [f" {shown[0]} | {shown[1]} | {shown[2]} ", "---+---+---"]
        rows.append(f" {shown[3]} | {shown[4]} | {shown[5]} ")
        rows.append("---+---+---")
        rows.append(f" {shown[6]} | {shown[7]} | {shown[8]} ")
        return "\n".join(rows)


class Opponent:
    agent = {
        "description": "Plays O against a human X on a tic-tac-toe board",
        "actions": {
            "place": {
                "description": "Mark one empty cell as O. cell is an integer from 1 to 9.",
                "args": {"cell": int},
            },
        },
    }

    def __init__(self, grid: Grid) -> None:
        self.grid = grid

    def see(self):
        return {
            "you": "O",
            "opponent": "X",
            "cells": list(self.grid.cells),
            "empty": self.grid.empty(),
            "winner": self.grid.winner(),
        }

    def place(self, cell):
        self.grid.place(int(cell), "O")
        return {"cell": cell, "cells": list(self.grid.cells), "winner": self.grid.winner()}


async def main() -> None:
    use_remote = "--remote" in sys.argv
    grid = Grid()
    opponent = Opponent(grid)
    if use_remote:
        agents = AgentServer(platform_key=_required("PLATFORM_KEY"))
    else:
        agents = AgentServer(inline=_local_platform())

    agent = await agents.start(opponent, name="O", goal=GOAL)
    stopped = False
    show_board = False
    print("You are X. The agent is O and plays by calling place.")
    print("Type a cell number. q quits.")
    try:
        while not grid.over():
            print()
            print(grid.render())
            if not await _human_turn(grid):
                stopped = True
                break
            print()
            print(grid.render())
            show_board = False
            if grid.over():
                break
            try:
                result = await agent.turn()
            except TurnFailedError as error:
                print(f"\nO failed to move: {error}")
                break
            played = ", ".join(f"{key}={value}" for key, value in result.args.items())
            print(f"\nO {result.action}({played})")
            show_board = True
        if show_board and not stopped:
            print()
            print(grid.render())
        if stopped:
            print("\nStopped.")
        elif grid.winner() == "X":
            print("\nYou win.")
        elif grid.winner() == "O":
            print("\nO wins.")
        else:
            print("\nDraw.")
    finally:
        await agents.close()


async def _human_turn(grid: Grid) -> bool:
    while True:
        try:
            line = await asyncio.to_thread(input, "Your cell (1-9, q to quit): ")
        except EOFError:
            return False
        text = line.strip().lower()
        if text in {"q", "quit"}:
            return False
        if not text.isdigit():
            print("Enter a cell from 1 to 9.")
            continue
        try:
            grid.place(int(text), "X")
        except RuntimeError as error:
            print(error)
            continue
        return True


def _local_platform() -> Any:
    """Offline opponent. It still calls place through the session loop."""

    class _Chooser:
        async def decide(self, *, instructions: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]):
            del instructions, tools
            cell = _choose_cell(_view(messages))
            return ModelDecision(ToolCall("place", {"cell": cell}), TokenUsage(0, 0))

    model = _Chooser()
    loops: dict[str, SessionLoop] = {}

    class _Platform:
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

    return _Platform()


def _view(messages: list[dict[str, Any]]) -> dict[str, Any]:
    content = str(messages[-1]["content"])
    start = content.find("{")
    if start < 0:
        raise RuntimeError("The turn view was not JSON")
    view, _end = json.JSONDecoder().raw_decode(content[start:])
    if not isinstance(view, dict):
        raise RuntimeError("The turn view was not an object")
    return view


def _choose_cell(view: dict[str, Any]) -> int:
    cells = [str(mark) for mark in view.get("cells") or []]
    empty = [int(cell) for cell in view.get("empty") or []]
    if len(cells) != 9 or not empty:
        raise RuntimeError("No empty cell to play")

    def would_win(mark: str, cell: int) -> bool:
        trial = list(cells)
        trial[cell - 1] = mark
        for line in LINES:
            marks = [trial[index] for index in line]
            if marks[0] != " " and marks[0] == marks[1] == marks[2]:
                return True
        return False

    for cell in empty:
        if would_win("O", cell):
            return cell
    for cell in empty:
        if would_win("X", cell):
            return cell
    if 5 in empty:
        return 5
    return empty[0]


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
