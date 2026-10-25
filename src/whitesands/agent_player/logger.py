"""Optional logger. Omit it and the SDK writes nothing."""

from __future__ import annotations

from typing import Any


def log(logger: Any, level: str, message: str, extra: Any = None) -> None:
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if fn is None and level == "warn":
        fn = getattr(logger, "warning", None)
    if not callable(fn):
        return
    try:
        if extra is None:
            fn(message)
        else:
            fn(message, extra)
    except TypeError:
        fn(message)
