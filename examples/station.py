"""Station Kite. You and the crew walk, talk, and close faults together.

python examples/station.py
Remote: PLATFORM_KEY=pk_... python examples/station.py --remote
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Literal

from whitesands.agent_player import AgentServer, TokenUsage, TurnFailedError
from whitesands.agent_player.testing import (
    ModelDecision,
    SessionLoop,
    ToolCall,
    loop_event_to_agent_event,
)

ModuleName = Literal["dock", "hub", "lab", "engineering"]
SystemName = Literal["comms", "containment", "reactor"]
ItemName = Literal["spool"]
MODULES = ("dock", "hub", "lab", "engineering")
GOAL = (
    "You are Ivo, the mechanic on station Kite. The duty officer can also walk, carry, and repair. "
    "Close every fault. move only along exits. take the spool when the reactor still needs it. "
    "repair fixes a fault in your current module. The reactor repair consumes the spool. "
    "say is how you talk to the duty officer. "
    "When actionsLeft is above 1, move, take, or repair. When actionsLeft is 1, call say: "
    "answer what the officer just told you, and name the fault you will handle next."
)
HELP = """
look                         show the ring
move <dock|hub|lab|engineering>
take spool                   pick up the coolant spool
repair <comms|containment|reactor>
say <message>                talk to Ivo
<anything else>              also talk to Ivo
<empty line>                 let Ivo keep working
quit                         end the shift
""".strip()


class Station:
    def __init__(self) -> None:
        self.where = {"you": "hub", "Ivo": "dock"}
        self.spool_at: str | None = "dock"
        self.carried_by: str | None = None
        self.faults = [
            {
                "module": "dock",
                "system": "comms",
                "detail": "Traffic-control uplink is dark",
                "needs": None,
            },
            {
                "module": "lab",
                "system": "containment",
                "detail": "Sample tray is unsealed",
                "needs": None,
            },
            {
                "module": "engineering",
                "system": "reactor",
                "detail": "Coolant pressure is climbing",
                "needs": "spool",
            },
        ]
        self.transcript: list[dict[str, str]] = []
        self.pending = False

    def clear(self) -> bool:
        return not self.faults

    def exits(self, module: str) -> list[str]:
        index = MODULES.index(module)
        found = []
        if index > 0:
            found.append(MODULES[index - 1])
        if index + 1 < len(MODULES):
            found.append(MODULES[index + 1])
        return found

    def move(self, who: str, module: str) -> str:
        current = self.where[who]
        if module not in MODULES:
            raise RuntimeError(f"Unknown module {module}")
        if module not in self.exits(current):
            reachable = ", ".join(self.exits(current))
            raise RuntimeError(f"Cannot reach {module} from {current}. Exits: {reachable}")
        self.where[who] = module
        return f"{_label(who)} walked to {module}."

    def take(self, who: str, item: str) -> str:
        if item != "spool":
            raise RuntimeError(f"There is no {item} here")
        if self.carried_by == who:
            raise RuntimeError(f"{_label(who)} is already carrying the spool")
        if self.carried_by is not None:
            raise RuntimeError(f"{_label(self.carried_by)} is carrying the spool")
        if self.spool_at == "installed":
            raise RuntimeError("The spool is already installed in the reactor")
        if self.spool_at != self.where[who]:
            raise RuntimeError(f"The spool is in {self.spool_at}, not {self.where[who]}")
        self.carried_by = who
        self.spool_at = None
        return f"{_label(who)} picked up the coolant spool."

    def repair(self, who: str, system: str) -> str:
        module = self.where[who]
        fault = next((item for item in self.faults if item["module"] == module and item["system"] == system), None)
        if fault is None:
            here = [item["system"] for item in self.faults if item["module"] == module]
            if here:
                raise RuntimeError(f"No {system} fault in {module}. Open here: {', '.join(here)}")
            raise RuntimeError(f"No {system} fault in {module}")
        if fault["needs"] == "spool" and self.carried_by != who:
            if self.carried_by is not None:
                raise RuntimeError(f"The reactor needs the spool. {_label(self.carried_by)} is carrying it")
            raise RuntimeError(f"The reactor needs the spool. It is in {self.spool_at}")
        if fault["needs"] == "spool":
            self.carried_by = None
            self.spool_at = "installed"
        self.faults = [item for item in self.faults if item is not fault]
        return f"{_label(who)} repaired {system} in {module}."

    def hear(self, text: str) -> str:
        self.transcript.append({"who": "you", "text": text})
        self.pending = True
        return f"You: {text}"

    def reply(self, text: str) -> str:
        cleaned = " ".join(text.split())
        if not cleaned:
            raise RuntimeError("say needs a message")
        self.transcript.append({"who": "Ivo", "text": cleaned})
        self.pending = False
        return cleaned

    def render(self) -> str:
        if self.carried_by is not None:
            spool = f"carried by {_label(self.carried_by)}"
        elif self.spool_at == "installed":
            spool = "installed in the reactor"
        else:
            spool = self.spool_at or "missing"
        lines = [
            "dock — hub — lab — engineering",
            f"You: {self.where['you']}    Ivo: {self.where['Ivo']}    Spool: {spool}",
        ]
        if self.faults:
            lines.append("Faults:")
            for fault in self.faults:
                extra = " (needs spool)" if fault["needs"] else ""
                lines.append(f"  {fault['module']} {fault['system']} — {fault['detail']}{extra}")
        else:
            lines.append("Faults: none")
        tail = self.transcript[-4:]
        if tail:
            lines.append("Recent:")
            for entry in tail:
                speaker = "You" if entry["who"] == "you" else "Ivo"
                lines.append(f"  {speaker}: {entry['text']}")
        return "\n".join(lines)

    def view(self, actions_left: int) -> dict[str, Any]:
        module = self.where["Ivo"]
        faults_here = [
            {"system": fault["system"], "detail": fault["detail"], "needs": fault["needs"]}
            for fault in self.faults
            if fault["module"] == module
        ]
        items_here = []
        if self.carried_by is None and self.spool_at == module:
            items_here.append("spool")
        return {
            "you": "Ivo",
            "module": module,
            "exits": self.exits(module),
            "carrying": "spool" if self.carried_by == "Ivo" else None,
            "itemsHere": items_here,
            "faultsHere": faults_here,
            "faults": [
                {
                    "module": fault["module"],
                    "system": fault["system"],
                    "detail": fault["detail"],
                    "needs": fault["needs"],
                }
                for fault in self.faults
            ],
            "spool": self.carried_by or self.spool_at,
            "officer": {
                "module": self.where["you"],
                "said": self.transcript[-1]["text"] if self.pending and self.transcript else None,
                "pending": self.pending,
            },
            "transcript": self.transcript[-8:],
            "actionsLeft": actions_left,
            "map": "dock—hub—lab—engineering",
        }


class Crew:
    agent = {
        "description": "Ivo, mechanic on station Kite. Talks to the duty officer and walks the ring to close faults.",
        "actions": {
            "say": {
                "description": "Speak to the duty officer. Use this when actionsLeft is 1.",
                "args": {"message": str},
            },
            "move": {
                "description": "Walk to an adjacent module listed in exits.",
                "args": {"module": ModuleName},
            },
            "take": {
                "description": "Pick up an item in this module. The reactor repair needs the spool.",
                "args": {"item": ItemName},
            },
            "repair": {
                "description": "Fix a fault in this module. Reactor repair consumes the spool you are carrying.",
                "args": {"system": SystemName},
            },
        },
    }

    def __init__(self, station: Station) -> None:
        self.station = station
        self.actions_left = 1

    def see(self):
        return self.station.view(self.actions_left)

    def say(self, message):
        spoken = self.station.reply(str(message))
        return {"said": spoken}

    def move(self, module):
        self.station.move("Ivo", str(module))
        return {"module": self.station.where["Ivo"], "exits": self.station.exits(self.station.where["Ivo"])}

    def take(self, item):
        self.station.take("Ivo", str(item))
        return {"carrying": "spool"}

    def repair(self, system):
        self.station.repair("Ivo", str(system))
        return {"repaired": system, "faultsLeft": len(self.station.faults)}


async def main() -> None:
    use_remote = "--remote" in sys.argv
    station = Station()
    crew = Crew(station)
    if use_remote:
        agents = AgentServer(platform_key=_required("PLATFORM_KEY"))
    else:
        agents = AgentServer(inline=_local_platform())
    agent = await agents.start(crew, name="Ivo", goal=GOAL)
    print("Station Kite. You are the duty officer. Ivo is the mechanic.")
    print("Walk the ring, talk, and close the faults together. The reactor needs the coolant spool.")
    print("Type help for commands.")
    try:
        show = True
        while not station.clear():
            if show:
                print()
                print(station.render())
                show = False
            try:
                line = await asyncio.to_thread(input, "> ")
            except EOFError:
                print("\nShift ended.")
                return
            kind, outcome = _command(station, line)
            if kind == "quit":
                print("\nShift ended.")
                return
            if kind == "help":
                print(HELP)
                continue
            if kind == "error":
                print(outcome)
                continue
            if outcome:
                print(outcome)
            if station.clear():
                break
            if kind == "look":
                show = True
                continue
            await _crew_burst(agent, crew, station)
            show = True
        print()
        print(station.render())
        print("\nKite is stable. Shift complete.")
    finally:
        await agents.close()


async def _crew_burst(agent: Any, crew: Crew, station: Station) -> None:
    for remaining in (3, 2, 1):
        crew.actions_left = remaining
        try:
            result = await agent.turn()
        except TurnFailedError as error:
            print(f"Ivo failed: {error}")
            return
        if result.action == "say":
            print(f"Ivo: {_said(result.result)}")
            return
        detail = ", ".join(f"{key}={value}" for key, value in result.args.items())
        print(f"Ivo {result.action}({detail})")
        if station.clear():
            crew.actions_left = 1
            try:
                report = await agent.turn()
            except TurnFailedError as error:
                print(f"Ivo failed: {error}")
                return
            if report.action == "say":
                print(f"Ivo: {_said(report.result)}")
            return


def _said(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("said", ""))
    return str(result or "")


def _command(station: Station, line: str) -> tuple[str, str]:
    text = line.strip()
    if not text:
        return "wait", ""
    lowered = text.lower()
    if lowered in {"q", "quit"}:
        return "quit", ""
    if lowered == "help":
        return "help", ""
    if lowered == "look":
        return "look", ""
    verb, _, rest = text.partition(" ")
    verb = verb.lower()
    argument = rest.strip()
    try:
        if verb == "move":
            if argument.lower() not in MODULES:
                return "error", "move where? dock, hub, lab, or engineering."
            return "act", station.move("you", argument.lower())
        if verb == "take":
            if not argument:
                return "error", "take what? The only loose part is the spool."
            return "act", station.take("you", argument.lower())
        if verb == "repair":
            if argument.lower() not in ("comms", "containment", "reactor"):
                return "error", "repair which system? comms, containment, or reactor."
            return "act", station.repair("you", argument.lower())
        if verb == "say":
            if not argument:
                return "error", "say what?"
            return "say", station.hear(argument)
    except RuntimeError as error:
        return "error", str(error)
    return "say", station.hear(text)


def _local_platform() -> Any:
    """Offline crew. It still talks and works by calling tools."""

    class _Chooser:
        async def decide(self, *, instructions: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]):
            del instructions, tools
            call = _choose(_view(messages))
            return ModelDecision(call, TokenUsage(0, 0))

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


def _choose(view: dict[str, Any]) -> ToolCall:
    left = int(view.get("actionsLeft") or 1)
    module = str(view.get("module"))
    carrying = view.get("carrying")
    if left > 1:
        for fault in view.get("faultsHere") or []:
            if fault.get("needs") != "spool" or carrying == "spool":
                return ToolCall("repair", {"system": fault["system"]})
        if "spool" in (view.get("itemsHere") or []) and _reactor_open(view) and carrying != "spool":
            return ToolCall("take", {"item": "spool"})
        dest = _destination(view)
        if dest and dest != module:
            step = _step(module, dest)
            if step is not None:
                return ToolCall("move", {"module": step})
    return ToolCall("say", {"message": _line(view)})


def _reactor_open(view: dict[str, Any]) -> bool:
    return any(fault.get("system") == "reactor" for fault in view.get("faults") or [])


def _destination(view: dict[str, Any]) -> str | None:
    faults = list(view.get("faults") or [])
    if not faults:
        return None
    here = str(view["module"])
    carrying = view.get("carrying")
    reactor = next((fault for fault in faults if fault.get("system") == "reactor"), None)
    others = [fault for fault in faults if fault.get("system") != "reactor"]
    if reactor is not None and carrying != "spool":
        spool = view.get("spool")
        if spool == "you":
            if not others:
                return None
        elif spool in MODULES:
            return str(spool)
    if reactor is not None and carrying == "spool":
        return "engineering"
    pool = others or faults
    return min(pool, key=lambda fault: abs(MODULES.index(str(fault["module"])) - MODULES.index(here)))["module"]


def _step(here: str, dest: str) -> str | None:
    if here not in MODULES or dest not in MODULES or here == dest:
        return None
    current = MODULES.index(here)
    target = MODULES.index(dest)
    return MODULES[current + 1] if target > current else MODULES[current - 1]


def _line(view: dict[str, Any]) -> str:
    faults = list(view.get("faults") or [])
    if not faults:
        body = "All faults are closed. Kite is holding."
    else:
        open_faults = ", ".join(f"{fault['system']} in {fault['module']}" for fault in faults)
        body = f"Still open: {open_faults}."
    if view.get("carrying") == "spool":
        body += " I have the coolant spool."
    elif view.get("spool") == "you" and _reactor_open(view):
        body += " You have the spool, so the reactor is yours if you can reach engineering."
    officer = view.get("officer") or {}
    if officer.get("pending") and officer.get("said"):
        return f"Copy. {body}"
    return f"I'm in {view.get('module')}. {body}"


def _label(who: str) -> str:
    return "You" if who == "you" else who


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
