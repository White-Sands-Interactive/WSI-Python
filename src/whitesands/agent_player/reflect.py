"""Publish tools from a player instance.

see() is required. Every other public method is an action. Names that start
with ``_`` are skipped. A class attribute named ``agent`` supplies the
description the model sees and the argument schemas checked before the method
runs. A method with no entry is still a tool. Its arguments accept any JSON value.
"""

from __future__ import annotations

import inspect
import types
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Union, get_args, get_origin

from whitesands.agent_player.protocol import SEE_DESCRIPTION, WAIT_DESCRIPTION

_UNION_ORIGINS = (Union, types.UnionType)


@dataclass
class PlayerTool:
    name: str
    description: str
    param_names: list[str]
    descriptor: dict[str, Any]
    invoke: Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ReflectedPlayer:
    description: str | None
    see: PlayerTool
    actions: list[PlayerTool]
    tools: list[dict[str, Any]]


def parameter_names(fn: Any) -> list[str]:
    """Parameter names of a callable, without self, cls, *args, or **kwargs."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    names: list[str] = []
    for param in signature.parameters.values():
        if param.name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        names.append(param.name)
    return names


def reflect_player(instance: object) -> ReflectedPlayer:
    names = _method_names(instance)
    if "see" not in names:
        raise RuntimeError("Player must implement a public see() method")

    metadata = _read_metadata(instance)
    declared = dict(metadata.get("actions") or {})
    class_name = type(instance).__name__ or "Player"
    for name in declared:
        if name not in names or name == "see":
            raise RuntimeError(f'Declared action "{name}" is not a public method on {class_name}')

    see = _build_tool(instance, "see", SEE_DESCRIPTION, None)
    actions = []
    for name in names:
        if name == "see":
            continue
        spec = declared.get(name)
        description = (spec or {}).get("description") or f"Action: {name}"
        actions.append(_build_tool(instance, name, description, spec))

    wait = {
        "name": "wait",
        "description": WAIT_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": {}},
    }
    description = metadata.get("description")
    return ReflectedPlayer(
        description=description if isinstance(description, str) and description else None,
        see=see,
        actions=actions,
        tools=[see.descriptor, *[action.descriptor for action in actions], wait],
    )


def _method_names(instance: object) -> list[str]:
    names: set[str] = set()
    for cls in type(instance).mro():
        if cls is object:
            break
        for name, value in cls.__dict__.items():
            if name.startswith("_") or name == "wait":
                continue
            if isinstance(value, (staticmethod, classmethod, property)):
                continue
            if callable(value):
                names.add(name)
    return sorted(names)


def _read_metadata(instance: object) -> dict[str, Any]:
    raw = getattr(type(instance), "agent", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _build_tool(instance: object, name: str, description: str, spec: dict[str, Any] | None) -> PlayerTool:
    method = getattr(instance, name)
    if not callable(method):
        raise RuntimeError(f"Expected method {name}")
    specs = spec.get("args") if spec and "args" in spec else None
    if isinstance(specs, dict):
        param_names = list(specs.keys())
    else:
        specs = None
        param_names = parameter_names(method)
    input_schema = _input_schema(param_names, specs)

    async def invoke(args: dict[str, Any]) -> Any:
        parsed = _bind(param_names, specs, args or {})
        result = method(**parsed)
        if inspect.isawaitable(result):
            result = await result
        return result

    return PlayerTool(
        name=name,
        description=description,
        param_names=param_names,
        descriptor={"name": name, "description": description, "inputSchema": input_schema},
        invoke=invoke,
    )


def _input_schema(param_names: list[str], specs: dict[str, Any] | None) -> dict[str, Any]:
    if specs is None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {name: {} for name in param_names},
        }
        if param_names:
            schema["required"] = list(param_names)
        return schema
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, annotation in specs.items():
        properties[name] = property_schema(annotation)
        if not _is_optional(annotation):
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _bind(param_names: list[str], specs: dict[str, Any] | None, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        args = {}
    if specs is None:
        if not param_names:
            return {}
        errors = [f"{name}: required" for name in param_names if name not in args]
        if errors:
            raise ValueError("; ".join(errors))
        return {name: args[name] for name in param_names}

    errors: list[str] = []
    parsed: dict[str, Any] = {}
    for name, annotation in specs.items():
        if name not in args:
            if not _is_optional(annotation):
                errors.append(f"{name}: required")
            continue
        error = _check(name, args[name], annotation)
        if error:
            errors.append(error)
        else:
            parsed[name] = args[name]
    if errors:
        raise ValueError("; ".join(errors))
    return parsed


def property_schema(annotation: Any) -> dict[str, Any]:
    """JSON Schema fragment for one declared argument."""
    origin = get_origin(annotation)
    if origin in _UNION_ORIGINS:
        inner = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(inner) == 1:
            return property_schema(inner[0])
        return {}
    if origin is Literal:
        values = list(get_args(annotation))
        if values and all(isinstance(value, str) for value in values):
            return {"type": "string", "enum": values}
        if values and all(isinstance(value, bool) for value in values):
            return {"type": "boolean", "enum": values}
        if values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return {"type": "integer", "enum": values}
        return {"enum": values}
    if isinstance(annotation, dict):
        return dict(annotation)
    if annotation is Any or annotation is object:
        return {}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is list or origin is list:
        schema: dict[str, Any] = {"type": "array"}
        args = get_args(annotation)
        if args:
            schema["items"] = property_schema(args[0])
        return schema
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def _is_optional(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return origin in _UNION_ORIGINS and type(None) in get_args(annotation)


def _check(path: str, value: Any, annotation: Any) -> str | None:
    origin = get_origin(annotation)
    if origin in _UNION_ORIGINS:
        args = get_args(annotation)
        if value is None and type(None) in args:
            return None
        inner = [arg for arg in args if arg is not type(None)]
        if len(inner) == 1:
            return _check(path, value, inner[0])
        return None
    if origin is Literal:
        choices = get_args(annotation)
        if value not in choices:
            rendered = ", ".join(repr(choice) for choice in choices)
            return f"{path}: expected one of {rendered}"
        return None
    if isinstance(annotation, dict):
        return _check_schema(path, value, annotation)
    if annotation is Any or annotation is object:
        return None
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{path}: expected integer, received {_type_name(value)}"
        return None
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{path}: expected number, received {_type_name(value)}"
        return None
    if annotation is str:
        if not isinstance(value, str):
            return f"{path}: expected string, received {_type_name(value)}"
        return None
    if annotation is bool:
        if not isinstance(value, bool):
            return f"{path}: expected boolean, received {_type_name(value)}"
        return None
    if annotation is list or origin is list:
        if not isinstance(value, list):
            return f"{path}: expected array, received {_type_name(value)}"
        item_args = get_args(annotation)
        if item_args:
            messages = [
                message
                for index, item in enumerate(value)
                if (message := _check(f"{path}.{index}", item, item_args[0]))
            ]
            if messages:
                return "; ".join(messages)
        return None
    if annotation is dict or origin is dict:
        if not isinstance(value, dict):
            return f"{path}: expected object, received {_type_name(value)}"
        return None
    return None


def _check_schema(path: str, value: Any, schema: dict[str, Any]) -> str | None:
    if "enum" in schema and value not in schema["enum"]:
        rendered = ", ".join(repr(choice) for choice in schema["enum"])
        return f"{path}: expected one of {rendered}"
    expected = schema.get("type")
    if expected == "integer":
        return _check(path, value, int)
    if expected == "number":
        return _check(path, value, float)
    if expected == "string":
        return _check(path, value, str)
    if expected == "boolean":
        return _check(path, value, bool)
    if expected == "array":
        return _check(path, value, list)
    if expected == "object":
        return _check(path, value, dict)
    return None


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__
