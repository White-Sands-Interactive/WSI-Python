from whitesands.agent_player.reflect import parameter_names, reflect_player


class Sample:
    def see(self):
        return {"ok": True}

    def move(self, dx, dy):
        return dx + dy

    def chop(self):
        return 1

    def _hidden(self):
        return "secret"


class Typed:
    agent = {
        "description": "A typed player",
        "actions": {
            "move": {
                "description": "Step by dx",
                "args": {"dx": int},
            },
        },
    }

    def see(self):
        return {"ok": True}

    def move(self, dx):
        return dx

    def wave(self):
        return "hi"


def test_parameter_names_skip_self():
    assert parameter_names(Sample.move) == ["dx", "dy"]
    assert parameter_names(Sample.chop) == []


def test_publishes_see_actions_and_wait_and_skips_private_methods():
    reflected = reflect_player(Sample())
    assert reflected.see.name == "see"
    assert sorted(action.name for action in reflected.actions) == ["chop", "move"]
    assert sorted(tool["name"] for tool in reflected.tools) == ["chop", "move", "see", "wait"]
    move = next(tool for tool in reflected.tools if tool["name"] == "move")
    assert move["inputSchema"]["required"] == ["dx", "dy"]
    assert move["inputSchema"]["properties"]["dx"].get("type") != "number"


def test_invokes_methods_with_named_args():
    import asyncio

    reflected = reflect_player(Sample())
    move = next(action for action in reflected.actions if action.name == "move")
    assert asyncio.run(move.invoke({"dx": 2, "dy": 3})) == 5


def test_requires_see():
    class NoSee:
        def move(self):
            return 1

    try:
        reflect_player(NoSee())
        raise AssertionError("expected see() to be required")
    except RuntimeError as error:
        assert "see" in str(error)


def test_uses_declared_args_and_still_reflects_undeclared_methods():
    import asyncio

    reflected = reflect_player(Typed())
    assert reflected.description == "A typed player"
    assert sorted(action.name for action in reflected.actions) == ["move", "wave"]
    move_tool = next(tool for tool in reflected.tools if tool["name"] == "move")
    assert move_tool["description"] == "Step by dx"
    assert move_tool["inputSchema"]["properties"]["dx"]["type"] == "integer"
    action = next(item for item in reflected.actions if item.name == "move")
    try:
        asyncio.run(action.invoke({"dx": "no"}))
        raise AssertionError("expected a schema error")
    except ValueError as error:
        assert "dx" in str(error)
    assert asyncio.run(action.invoke({"dx": 4})) == 4


def test_rejects_a_declared_action_that_is_not_a_method():
    class Broken:
        agent = {"actions": {"fly": {"description": "nope"}}}

        def see(self):
            return {}

    try:
        reflect_player(Broken())
        raise AssertionError("expected a missing action to fail")
    except RuntimeError as error:
        assert "fly" in str(error)
