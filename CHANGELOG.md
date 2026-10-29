# Changelog

## 1.1.1

- `examples/tictactoe.py` lets you play X while the agent plays O by calling `place`.
- `examples/station.py` is a station shift. You and the crew walk, talk, and close faults.

## 1.1.0

- The SDK connects to `wss://api.whitesandsint.com`. The address is not configurable.

## 1.0.0

First release of the Python agent player SDK. It speaks the same protocol as `@whitesands/agent-player` 1.0.0 and `WhiteSands.AgentPlayer` 1.0.0.

- `AgentServer` reads `WSI_PLATFORM_KEY` and connects to `wss://api.whitesandsint.com`.
- Player classes become tools. Argument schemas are declared on `agent`.
- `turn()` returns the action, arguments, result, attempt count, and token usage.
- Typed errors, event listeners, turn timeouts, abort signals, and `async with`.
- Reconnect with session resume, heartbeat, and a protocol version handshake.
- `create_test_platform` runs the production loop offline.
- Action history is written to `.wsi/agent-memory.sqlite`. Pass `memory_path=False` to turn it off.
