import tempfile
from pathlib import Path

from whitesands.agent_player.memory import SessionMemory, default_memory_path, view_with_done


def test_returns_one_sessions_actions_oldest_first():
    with tempfile.TemporaryDirectory() as directory:
        memory = SessionMemory(str(Path(directory) / "memory.sqlite"))
        memory.record("ada", "Ada", {"action": "cross", "args": {"passenger": "goat"}, "ok": True})
        memory.record(
            "ada",
            "Ada",
            {"action": "cross", "args": {"passenger": "wolf"}, "ok": False, "error": "wrong bank"},
        )
        memory.record("bea", "Bea", {"action": "move", "args": {"dx": 1}, "ok": True})
        assert memory.done("ada") == [
            {"action": "cross", "args": {"passenger": "goat"}, "ok": True},
            {"action": "cross", "args": {"passenger": "wolf"}, "ok": False, "error": "wrong bank"},
        ]
        assert memory.done("bea") == [{"action": "move", "args": {"dx": 1}, "ok": True}]
        memory.close()


def test_keeps_the_most_recent_actions_on_the_view():
    with tempfile.TemporaryDirectory() as directory:
        memory = SessionMemory(str(Path(directory) / "memory.sqlite"))
        for i in range(55):
            memory.record("ada", "Ada", {"action": "move", "args": {"dx": i}, "ok": True})
        done = memory.done("ada")
        assert len(done) == 50
        assert done[0]["args"] == {"dx": 5}
        assert done[49]["args"] == {"dx": 54}
        memory.close()


def test_adds_done_onto_an_object_view():
    assert view_with_done({"boat": "near"}, []) == {"boat": "near", "done": []}
    assert view_with_done("plain", []) == {"view": "plain", "done": []}


def test_default_memory_path_is_under_wsi():
    path = Path(default_memory_path())
    assert path.name == "agent-memory.sqlite"
    assert path.parent.name == ".wsi"
