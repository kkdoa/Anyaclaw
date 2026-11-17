"""Long-term memory backed by SQLite (FTS5 when available).

The reference implementation told the model to append Markdown bullet points to
``MEMORY.md`` via ``write_file`` — which works until two sessions write at once,
or the file grows past the context window. Here memories are rows, fused with
FTS5 (trigram tokenizer, so Chinese works), retrieved by relevance and injected
into the prompt as a small, deduplicated block.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from anyaclaw.logging import get_logger

log = get_logger("memory")

_FTS_SPECIALS = re.compile(r'["*():^\-]')


@dataclass(slots=True)
class Memory:
    id: int
    content: str
    tags: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MemoryStore:
    """Small, durable, full-text searchable memory."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._has_fts = False
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    content    TEXT NOT NULL,
                    tags       TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content
                    ON memories(content);
                """
            )
            for tokenizer in ("trigram", "unicode61"):
                try:
                    self._conn.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                        f"USING fts5(content, tags, tokenize='{tokenizer}')"
                    )
                    self._has_fts = True
                    break
                except sqlite3.OperationalError:
                    continue
            if not self._has_fts:
                log.warning("SQLite FTS5 unavailable; memory search falls back to LIKE")

    # -- write -------------------------------------------------------------
    async def remember(self, content: str, tags: str = "") -> Memory:
        text = content.strip()
        if not text:
            raise ValueError("memory content must not be empty")
        now = time.time()

        def _write() -> int:
            with self._lock:
                cursor = self._conn.execute(
                    """
                    INSERT INTO memories(content, tags, created_at, updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(content) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        tags = CASE WHEN excluded.tags != '' THEN excluded.tags ELSE memories.tags END
                    """,
                    (text, tags, now, now),
                )
                row = self._conn.execute(
                    "SELECT id FROM memories WHERE content=?", (text,)
                ).fetchone()
                memory_id = int(row["id"]) if row else int(cursor.lastrowid)
                if self._has_fts:
                    self._conn.execute("DELETE FROM memories_fts WHERE rowid=?", (memory_id,))
                    self._conn.execute(
                        "INSERT INTO memories_fts(rowid, content, tags) VALUES(?,?,?)",
                        (memory_id, text, tags),
                    )
                return memory_id

        memory_id = await asyncio.to_thread(_write)
        return Memory(id=memory_id, content=text, tags=tags, created_at=now, updated_at=now)

    async def forget(self, target: str) -> int:
        """Delete by numeric id, exact content, or substring match."""
        value = str(target).strip()

        def _delete() -> int:
            with self._lock:
                if value.isdigit():
                    ids = [int(value)]
                else:
                    rows = self._conn.execute(
                        "SELECT id FROM memories WHERE content LIKE ? OR tags LIKE ?",
                        (f"%{value}%", f"%{value}%"),
                    ).fetchall()
                    ids = [int(row["id"]) for row in rows]
                for memory_id in ids:
                    self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
                    if self._has_fts:
                        self._conn.execute("DELETE FROM memories_fts WHERE rowid=?", (memory_id,))
                return len(ids)

        return await asyncio.to_thread(_delete)

    # -- read --------------------------------------------------------------
    async def search(self, query: str, limit: int = 5) -> list[Memory]:
        text = (query or "").strip()
        if not text:
            return []

        def _search() -> list[sqlite3.Row]:
            with self._lock:
                if self._has_fts and len(text) >= 3:
                    match = _fts_query(text)
                    try:
                        rows = self._conn.execute(
                            """
                            SELECT m.id, m.content, m.tags, m.created_at, m.updated_at
                            FROM memories_fts f JOIN memories m ON m.id = f.rowid
                            WHERE memories_fts MATCH ?
                            ORDER BY rank LIMIT ?
                            """,
                            (match, limit),
                        ).fetchall()
                        if rows:
                            return rows
                    except sqlite3.OperationalError:
                        pass
                # Trigram FTS5 needs at least three characters (CJK terms are often
                # shorter), so fall back to substring matching.
                rows = []
                for term in _terms(text):
                    rows.extend(
                        self._conn.execute(
                            """
                            SELECT id, content, tags, created_at, updated_at
                            FROM memories WHERE content LIKE ? OR tags LIKE ?
                            ORDER BY updated_at DESC LIMIT ?
                            """,
                            (f"%{term}%", f"%{term}%", limit),
                        ).fetchall()
                    )
                return rows

        rows = await asyncio.to_thread(_search)
        seen: set[int] = set()
        memories: list[Memory] = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            memories.append(_row_to_memory(row))
            if len(memories) >= limit:
                break
        return memories

    async def list(self, limit: int = 50) -> list[Memory]:
        def _read() -> list[sqlite3.Row]:
            with self._lock:
                return self._conn.execute(
                    "SELECT id, content, tags, created_at, updated_at "
                    "FROM memories ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        return [_row_to_memory(row) for row in await asyncio.to_thread(_read)]

    async def count(self) -> int:
        def _read() -> int:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
                return int(row["n"]) if row else 0

        return await asyncio.to_thread(_read)

    async def clear(self) -> None:
        def _write() -> None:
            with self._lock:
                self._conn.execute("DELETE FROM memories")
                if self._has_fts:
                    self._conn.execute("DELETE FROM memories_fts")

        await asyncio.to_thread(_write)

    async def export_markdown(self, limit: int = 500) -> str:
        memories = await self.list(limit)
        lines = ["# MEMORY", ""]
        lines.extend(f"- {memory.content}" for memory in reversed(memories))
        return "\n".join(lines) + "\n"

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(_close)


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=int(row["id"]),
        content=row["content"],
        tags=row["tags"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _terms(text: str) -> list[str]:
    parts = re.split(r"[\s,，。;；]+", text)
    return [part for part in parts if part][:5] or [text]


def _fts_query(text: str) -> str:
    cleaned = _FTS_SPECIALS.sub(" ", text)
    terms = [term for term in cleaned.split() if term]
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)
