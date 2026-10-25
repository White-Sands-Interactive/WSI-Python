"""SQLite log of actions one game process has already called.

Each session reads only its own rows, so a new start() begins with an empty list.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

# Actions kept on the view. Older rows stay in the file and drop off the prompt.
MAX_DONE = 50


def default_memory_path() -> str:
    return os.path.join(os.getcwd(), ".wsi", "agent-memory.sqlite")


def view_with_done(view: Any, done: list[dict[str, Any]]) -> Any:
    """Attach this session's action log to a see() payload."""
    if isinstance(view, dict):
        return {**view, "done": done}
    return {"view": view, "done": done}


class SessionMemory:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS actions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              character TEXT NOT NULL,
              action TEXT NOT NULL,
              args TEXT NOT NULL,
              ok INTEGER NOT NULL,
              error TEXT
            );
            CREATE INDEX IF NOT EXISTS actions_by_session ON actions(session_id, id);
            """
        )
        self._db.commit()

    def record(self, session_id: str, character: str, entry: dict[str, Any]) -> None:
        args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
        error = entry.get("error")
        with self._lock:
            self._db.execute(
                """
                INSERT INTO actions (session_id, character, action, args, ok, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    character,
                    entry["action"],
                    json.dumps(args, separators=(",", ":"), ensure_ascii=False),
                    1 if entry.get("ok") else 0,
                    error if error else None,
                ),
            )
            self._db.commit()

    def done(self, session_id: str) -> list[dict[str, Any]]:
        """Oldest first, capped to the most recent actions for this session."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT action, args, ok, error FROM (
                  SELECT id, action, args, ok, error FROM actions
                  WHERE session_id = ?
                  ORDER BY id DESC
                  LIMIT ?
                ) ORDER BY id ASC
                """,
                (session_id, MAX_DONE),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for action, args, ok, error in rows:
            entry: dict[str, Any] = {
                "action": action,
                "args": json.loads(args),
                "ok": ok == 1,
            }
            if error:
                entry["error"] = error
            result.append(entry)
        return result

    def close(self) -> None:
        with self._lock:
            self._db.close()
