# WSI Agent Player for Python

The library starts a remote game agent from a player class. The game calls `start` and `turn`. The SDK publishes that class as tools. The model runs on the platform, so the game process never holds the model key.

This is the Python port of [`@whitesands/agent-player`](https://github.com/White-Sands-Interactive/WSI-Node) and `WhiteSands.AgentPlayer`. All three speak protocol version 1.

The library also provides:

- A player class turned into tools, with optional argument schemas.
- One action per turn, and one retry when that action throws.
- An offline loop for tests, with no socket and no platform key.
- A local SQLite log of what each character has already called.

## Installation

```sh
pip install agent-player
```

Python 3.11 or newer. The action log uses the standard-library `sqlite3` module. A connection to the platform uses `websockets`, which this package depends on. An offline `inline` platform does not open a socket.

## Usage

Set the platform key, then start one agent per character. `turn()` grants that character one chance to act and returns what they did.

```sh
export WSI_PLATFORM_KEY=pk_...
```

The SDK connects to `wss://api.whitesandsint.com`. That address is not read from the environment or from the command line.

```python
from whitesands.agent_player import AgentServer

agents = AgentServer()
ada = await agents.start(Villager(world, "Ada"), name="Ada", goal="Stand on the tree and chop it")

result = await ada.turn()
# result.action, result.args, result.result, result.attempts, result.usage
```

`AgentServer(platform_key="...")` overrides `WSI_PLATFORM_KEY`.

### The player class

`see` is required. It returns the JSON the model reads at the start of the turn. Every other public method is an action the model may call. Names that start with `_`, and `wait` (the SDK provides that tool), are left off the tool list.

Throw to refuse an action. The platform asks the model once more. A second failure raises `TurnFailedError` from `turn()`.

```python
class Villager:
    def __init__(self, world, name):
        self.world = world
        self.name = name
        self.x = 0
        self.y = 0
        self.wood = 0

    def see(self):
        return {"x": self.x, "y": self.y, "wood": self.wood, "tile": self.world.get(self.x, self.y)}

    def move(self, dx, dy):
        next_x = self.x + dx
        next_y = self.y + dy
        if not self.world.in_bounds(next_x, next_y):
            raise RuntimeError(f"Cannot move to {next_x},{next_y}")
        self.x = next_x
        self.y = next_y
        return {"x": self.x, "y": self.y}

    def chop(self):
        if self.world.get(self.x, self.y) != "T":
            raise RuntimeError(f"No tree at {self.x},{self.y}")
        self.wood += 1
        return {"wood": self.wood}
```

A method with no `agent` entry is still a tool. Its arguments accept any JSON value. `async def` methods are awaited. A method that returns `None` is reported as `{ "ok": true }`.

### Declaring arguments

`agent` on the class adds the description the model sees and checks arguments before your method runs. A schema failure counts as a failed action and uses the one retry.

```python
class Villager:
    agent = {
        "description": "A villager who gathers wood",
        "actions": {
            "move": {
                "description": "Step by dx and dy. Stay inside the map.",
                "args": {"dx": int, "dy": int},
            },
            "chop": {"description": "Chop the tree on your tile"},
        },
    }

    def see(self):
        return {"x": self.x, "y": self.y}
```

| Annotation | JSON Schema | Accepted values |
| --- | --- | --- |
| `int` | integer | integers. `True` and `False` are rejected |
| `float` | number | integers and floats |
| `str` | string | strings |
| `bool` | boolean | `True` and `False` |
| `list` or `list[int]` | array | lists, and each item when the item type is set |
| `dict` | object | dicts |
| `Literal["wolf", "goat"]` | string enum | one of those values |
| `int \| None` | integer, not required | omitted, `None`, or an integer |

Argument names must match the method parameters. Keys in `args` are the names passed into the method. A missing required argument fails before the method runs. Extra keys are ignored.

### Turns

`turn()` does not choose the action. It unblocks that session's `wait`. The platform reads `see()`, calls one action, and reports back.

```python
result = await ada.turn(timeout_ms=30_000, signal=signal)
```

| Field | Meaning |
| --- | --- |
| `action` | Tool the model called |
| `args` | Arguments it passed |
| `result` | What your method returned, after a JSON round trip |
| `attempts` | `1`, or `2` when the first call failed and the retry succeeded |
| `usage` | `prompt_tokens` and `completion_tokens` for that turn |

The default timeout is 60 seconds. Pass `signal` to abort the wait. `AbortSignal` is the built-in signal: call `signal.abort()` and `turn()` raises. Any object with `aborted`, `reason`, and `add_abort_listener` works the same way.

`handle.stop()` ends that session. `agents.close()`, also run by `async with`, stops every session on the server. The platform process keeps running.

```python
async with AgentServer() as agents:
    ada = await agents.start(villager, name="Ada")
    await ada.turn()
```

### Errors

| Error | When |
| --- | --- |
| `PlatformAuthError` | The platform rejected the key |
| `ProtocolVersionError` | This SDK and the platform disagree on the protocol |
| `TurnFailedError` | The action and the retry both failed. `error.attempts` lists both calls |
| `TurnTimeoutError` | The platform did not finish within `timeout_ms` |
| `ConnectionLostError` | The socket dropped and the session could not be resumed |

The SDK reconnects with backoff. A disconnected session can be resumed for 30 seconds.

```python
from whitesands.agent_player import TurnFailedError

try:
    await ada.turn()
except TurnFailedError as error:
    print(error.attempts)
    raise
```

### Several characters

One `AgentServer` hosts every character. Each `start()` is its own session.

```python
ada = await agents.start(Villager(world, "Ada"), name="Ada", goal="Chop the tree")
bea = await agents.start(Villager(world, "Bea"), name="Bea", goal="Carry wood home")

await ada.turn()
await bea.turn()
```

The game decides whose turn it is by calling `turn()` on that handle.

### Events

`agents.on` hears every session. `handle.on` hears one session. Both return an unsubscribe function.

```python
stop = ada.on(lambda event: print(event.type, event.action))
```

| `event.type` | Fields |
| --- | --- |
| `action` | `session_id`, `action`, `args` |
| `retry` | `session_id`, `action`, `args`, `error` |
| `turnFailed` | `session_id`, `error`, and `action` / `args` when the retry was a real tool |
| `disconnected` | none. No `session_id` |
| `reconnected` | none. No `session_id` |

`disconnected` and `reconnected` are connection events. They are delivered to `agents.on` only.

### Action history

Every `start()` records that character's calls in one SQLite file, `.wsi/agent-memory.sqlite` in the process's current directory. The directory is created on the first `start()`. Deleting it is safe. The next start creates it again.

The next `see()` view includes `done`, oldest first, up to the last 50 calls for that session:

```json
{
  "x": 1,
  "y": 0,
  "done": [
    { "action": "move", "args": { "dx": 1, "dy": 0 }, "ok": true }
  ]
}
```

A refused call is `{ "ok": false, "error": "..." }`. A new `start()` has an empty `done` list. Older rows stay in the file. Two characters on one server share the file and see only their own rows. The name on each row is the `name` passed to `start()`, or the class name.

```python
agents = AgentServer(memory_path="saves/agents.sqlite")
```

`memory_path=False` turns the log off. The view then has no `done` field.

### Goal and instructions

`goal` is added to the model prompt for that character. `instructions` on `start()` is appended to the default prompt. `instructions` on `AgentServer` replaces the default prompt for every character on that server.

```python
await agents.start(ferry, name="Ferryman", goal="Get the wolf, the goat, and the cabbage onto the far bank.")
```

### Testing without a platform

`whitesands.agent_player.testing.create_test_platform` runs the same wait, act, and retry loop in memory. The script is the list of tool calls, in order. No key and no socket. Every session on that server consumes the script from one shared queue.

```python
from whitesands.agent_player import AgentServer
from whitesands.agent_player.testing import create_test_platform

agents = AgentServer(
    inline=create_test_platform(
        [
            {"name": "move", "arguments": {"dx": 1, "dy": 0}},
            {"name": "chop", "arguments": {}},
        ]
    )
)
```

When the script runs out, the stand-in model calls `wait`, which is not an action, so the turn fails.

### Where each piece runs

| | Game process | Platform |
| --- | --- | --- |
| World and player methods | yes | |
| Tools (`see`, actions, `wait`) | yes | calls them |
| Whose turn it is | `turn()` | |
| Which action to take | | the model |
| Model API key | | yes |

### Logging

Pass a `logger` to print SDK warnings. Omit it and nothing is written. `debug`, `info`, `warn`, and `error` are all optional. `warning` is accepted in place of `warn`.

```python
class Log:
    def warn(self, message, extra=None):
        print(message)

    def error(self, message, extra=None):
        print(message)

agents = AgentServer(logger=Log())
```

A standard-library logger works for `error`. `warning` is accepted in place of `warn`.

### Protocol

`whitesands.agent_player.protocol` exports the wire constants and `PROTOCOL_VERSION`. The Node SDK's copy is `src/protocol.ts`. The C# SDK's copy is `WhiteSands.AgentPlayer.Protocol`. A change to the wire format has to land in every place. Game code uses `AgentServer` and does not import the protocol.

Messages on the socket use the same camelCase fields as the other SDKs (`sessionId`, `promptTokens`, `inputSchema`). The Python API uses snake_case (`session_id`, `prompt_tokens`).

## This repository

| Path | Role |
| --- | --- |
| `src/whitesands/agent_player` | The SDK |
| `examples/forest.py` | Two villagers, reflected methods |
| `examples/courier.py` | An integer argument and a goal |
| `examples/ferry.py` | One passenger chosen each turn |

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
python examples/forest.py
python examples/courier.py
python examples/ferry.py
```

Each example replays a script. Add `--remote` to forest or ferry to connect to `wss://api.whitesandsint.com`:

```sh
PLATFORM_KEY=pk_... python examples/forest.py --remote
```

Releases are recorded in [CHANGELOG.md](CHANGELOG.md).
