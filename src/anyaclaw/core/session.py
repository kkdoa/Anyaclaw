"""Conversation persistence.

Two backends are shipped:

* :class:`SqliteSessionStore` (default) — ordered, indexed and cheap to query
  (session list, message counts, pagination). SQLite in WAL mode survives
  crashes and concurrent web requests far better than append-only text files.
* :class:`JsonlSessionStore` — the original one-file-per-session layout,
  retained for migration and for people who like grepping their transcripts.

Messages are stored in the OpenAI wire format so they can be replayed verbatim.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anyaclaw.core.errors import SessionError
from anyaclaw.core.types import Message
from anyaclaw.logging import get_logger

log = get_logger("session")


@dataclass(slots=True)
class SessionInfo:
    key: str
    channel: str = ""
    title: str = ""
    messages: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "channel": self.channel,
            "title": self.title,
            "messages": self.messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SessionStore(ABC):
    """Abstract conversation store."""

    @abstractmethod
    async def append(self, session_key: str, message: Message) -> None: ...

    @abstractmethod
    async def history(self, session_key: str, limit: int | None = None) -> list[Message]: ...

    @abstractmethod
    async def clear(self, session_key: str) -> None: ...

    @abstractmethod
    async def list_sessions(self, limit: int = 100) -> list[SessionInfo]: ...

    @abstractmethod
    async def count(self, session_key: str) -> int: ...

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


class SqliteSessionStore(SessionStore):
    """SQLite-backed transcript store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    key        TEXT PRIMARY KEY,
                    channel    TEXT NOT NULL DEFAULT '',
                    title      TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_key, id);
                """
            )

    # -- write -------------------------------------------------------------
    async def append(self, session_key: str, message: Message) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        now = time.time()
        channel = session_key.split(":", 1)[0] if ":" in session_key else ""

        def _write() -> None:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO sessions(key, channel, title, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (session_key, channel, _derive_title(message), now, now),
                )
                self._conn.execute(
                    "INSERT INTO messages(session_key, role, payload, created_at) VALUES(?,?,?,?)",
                    (session_key, str(message.get("role", "")), payload, now),
                )
                if message.get("role") == "user":
                    self._conn.execute(
                        "UPDATE sessions SET title = ? WHERE key = ? AND title = ''",
                        (_derive_title(message), session_key),
                    )

        try:
            await asyncio.to_thread(_write)
        except sqlite3.Error as exc:  # pragma: no cover - disk failures
            raise SessionError(f"failed to persist message: {exc}") from exc

    # -- read --------------------------------------------------------------
    async def history(self, session_key: str, limit: int | None = None) -> list[Message]:
        def _read() -> list[Message]:
            with self._lock:
                if limit:
                    rows = self._conn.execute(
                        "SELECT payload FROM messages WHERE session_key=? ORDER BY id DESC LIMIT ?",
                        (session_key, limit),
                    ).fetchall()
                    rows.reverse()
                else:
                    rows = self._conn.execute(
                        "SELECT payload FROM messages WHERE session_key=? ORDER BY id ASC",
                        (session_key,),
                    ).fetchall()
            return [json.loads(row[0]) for row in rows]

        try:
            return await asyncio.to_thread(_read)
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            log.warning("failed to load history for %s: %s", session_key, exc)
            return []

    async def list_sessions(self, limit: int = 100) -> list[SessionInfo]:
        def _read() -> list[SessionInfo]:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT s.key, s.channel, s.title, s.created_at, s.updated_at,
                           (SELECT COUNT(*) FROM messages m WHERE m.session_key = s.key)
                    FROM sessions s
                    ORDER BY s.updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                SessionInfo(
                    key=row[0],
                    channel=row[1],
                    title=row[2],
                    created_at=row[3],
                    updated_at=row[4],
                    messages=row[5],
                )
                for row in rows
            ]

        return await asyncio.to_thread(_read)

    async def count(self, session_key: str) -> int:
        def _read() -> int:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_key=?", (session_key,)
                ).fetchone()
            return int(row[0] if row else 0)

        return await asyncio.to_thread(_read)

    async def clear(self, session_key: str) -> None:
        def _write() -> None:
            with self._lock:
                self._conn.execute("DELETE FROM messages WHERE session_key=?", (session_key,))
                self._conn.execute("DELETE FROM sessions WHERE key=?", (session_key,))

        await asyncio.to_thread(_write)

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(_close)

    # -- migration helper --------------------------------------------------
    async def import_jsonl_dir(self, directory: str | Path) -> int:
        """Import legacy ``*.jsonl`` transcripts; returns the number of messages."""
        directory = Path(directory)
        if not directory.is_dir():
            return 0
        imported = 0
        for path in sorted(directory.glob("*.jsonl")):
            session_key = path.stem.replace("_", ":", 1) if "_" in path.stem else path.stem
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message.pop("timestamp", None)
                if not isinstance(message, dict) or "role" not in message:
                    continue
                await self.append(session_key, message)
                imported += 1
        return imported


class MemorySessionStore(SessionStore):
    """Ephemeral store used by subagents and tests."""

    def __init__(self) -> None:
        self._data: dict[str, list[Message]] = {}

    async def append(self, session_key: str, message: Message) -> None:
        self._data.setdefault(session_key, []).append(dict(message))

    async def history(self, session_key: str, limit: int | None = None) -> list[Message]:
        messages = [dict(message) for message in self._data.get(session_key, [])]
        return messages[-limit:] if limit else messages

    async def clear(self, session_key: str) -> None:
        self._data.pop(session_key, None)

    async def list_sessions(self, limit: int = 100) -> list[SessionInfo]:
        return [
            SessionInfo(key=key, messages=len(messages))
            for key, messages in list(self._data.items())[:limit]
        ]

    async def count(self, session_key: str) -> int:
        return len(self._data.get(session_key, []))


class JsonlSessionStore(SessionStore):
    """One ``.jsonl`` file per session (legacy compatible)."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_key: str) -> Path:
        safe = session_key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self.directory / f"{safe}.jsonl"

    async def append(self, session_key: str, message: Message) -> None:
        record = dict(message)
        record["timestamp"] = time.time()

        def _write() -> None:
            with self._path(session_key).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        await asyncio.to_thread(_write)

    async def history(self, session_key: str, limit: int | None = None) -> list[Message]:
        path = self._path(session_key)
        if not path.is_file():
            return []

        def _read() -> list[Message]:
            messages: list[Message] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message.pop("timestamp", None)
                messages.append(message)
            return messages[-limit:] if limit else messages

        return await asyncio.to_thread(_read)

    async def list_sessions(self, limit: int = 100) -> list[SessionInfo]:
        def _read() -> list[SessionInfo]:
            infos: list[SessionInfo] = []
            for path in self.directory.glob("*.jsonl"):
                infos.append(
                    SessionInfo(
                        key=path.stem.replace("_", ":", 1),
                        channel=path.stem.split("_", 1)[0],
                        title="",
                        messages=sum(1 for _ in path.open("r", encoding="utf-8")),
                        updated_at=path.stat().st_mtime,
                        created_at=path.stat().st_ctime,
                    )
                )
            infos.sort(key=lambda info: info.updated_at, reverse=True)
            return infos[:limit]

        return await asyncio.to_thread(_read)

    async def count(self, session_key: str) -> int:
        return len(await self.history(session_key))

    async def clear(self, session_key: str) -> None:
        path = self._path(session_key)
        if path.is_file():
            await asyncio.to_thread(path.unlink)


def _derive_title(message: Message) -> str:
    if message.get("role") != "user":
        return ""
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    title = " ".join(content.split())
    return title[:60]


def open_session_store(config: Any) -> SessionStore:
    """Create the session store selected by ``storage.session_backend``."""
    data_dir: Path = config.data_dir
    if config.storage.session_backend == "jsonl":
        return JsonlSessionStore(data_dir / "sessions")
    return SqliteSessionStore(data_dir / "anyaclaw.db")
