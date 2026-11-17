"""Task centre: a durable record of every agent run.

Each inbound message becomes a task with status transitions, tool calls and an
event log. That makes ``/tasks``, retries and the Web "activity" view possible,
and turns "why did the bot take 40 seconds?" into a query instead of a guess.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

STATUSES = {"queued", "running", "done", "error", "cancelled"}


@dataclass(slots=True)
class TaskRecord:
    id: str
    channel: str
    sender_id: str
    chat_id: str
    content: str
    status: str = "queued"
    response: str = ""
    error: str = ""
    files: list[str] = field(default_factory=list)
    tool_calls: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "channel": self.channel,
            "sender_id": self.sender_id,
            "chat_id": self.chat_id,
            "content": self.content,
            "status": self.status,
            "response": self.response,
            "error": self.error,
            "files": self.files,
            "tool_calls": self.tool_calls,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_events:
            data["events"] = self.events
        return data


class TaskCenter:
    """SQLite-backed task log."""

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
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id         TEXT PRIMARY KEY,
                    channel    TEXT NOT NULL DEFAULT '',
                    sender_id  TEXT NOT NULL DEFAULT '',
                    chat_id    TEXT NOT NULL DEFAULT '',
                    content    TEXT NOT NULL DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'queued',
                    response   TEXT NOT NULL DEFAULT '',
                    error      TEXT NOT NULL DEFAULT '',
                    files      TEXT NOT NULL DEFAULT '[]',
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    events     TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_channel ON tasks(channel, created_at DESC);
                """
            )

    # -- write -------------------------------------------------------------
    async def create(
        self,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
        content: str,
    ) -> str:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()

        def _write() -> None:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO tasks(id, channel, sender_id, chat_id, content,
                                      status, created_at, updated_at)
                    VALUES(?,?,?,?,?,'queued',?,?)
                    """,
                    (task_id, channel, sender_id, chat_id, content[:4000], now, now),
                )

        await asyncio.to_thread(_write)
        return task_id

    async def update(
        self,
        task_id: str,
        status: str | None = None,
        *,
        response: str | None = None,
        error: str | None = None,
        files: list[str] | None = None,
    ) -> None:
        if status is not None and status not in STATUSES:
            raise ValueError(f"invalid task status: {status}")

        def _write() -> None:
            with self._lock:
                current = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if current is None:
                    return
                self._conn.execute(
                    """
                    UPDATE tasks SET status=?, response=?, error=?, files=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        status or current["status"],
                        response if response is not None else current["response"],
                        error if error is not None else current["error"],
                        json.dumps(files if files is not None else json.loads(current["files"]), ensure_ascii=False),
                        time.time(),
                        task_id,
                    ),
                )

        await asyncio.to_thread(_write)

    async def add_event(self, task_id: str, kind: str, title: str, detail: str = "") -> None:
        entry = {
            "ts": time.time(),
            "kind": kind,
            "title": title,
            "detail": detail[:2000],
        }

        def _write() -> None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT events FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    return
                events = json.loads(row["events"])
                events.append(entry)
                self._conn.execute(
                    "UPDATE tasks SET events=?, updated_at=? WHERE id=?",
                    (json.dumps(events[-200:], ensure_ascii=False), time.time(), task_id),
                )

        await asyncio.to_thread(_write)

    async def add_tool_call(self, task_id: str, tool: str) -> None:
        def _write() -> None:
            with self._lock:
                self._conn.execute(
                    "UPDATE tasks SET tool_calls = tool_calls + 1, updated_at=? WHERE id=?",
                    (time.time(), task_id),
                )

        await asyncio.to_thread(_write)

    # -- read --------------------------------------------------------------
    async def get(self, task_id: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            with self._lock:
                row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return _row_to_task(row).to_dict(include_events=True) if row else None

        return await asyncio.to_thread(_read)

    async def list(self, limit: int = 20, channel: str = "") -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._lock:
                if channel:
                    rows = self._conn.execute(
                        "SELECT * FROM tasks WHERE channel=? ORDER BY created_at DESC LIMIT ?",
                        (channel, limit),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                    ).fetchall()
            return [_row_to_task(row).to_dict() for row in rows]

        return await asyncio.to_thread(_read)

    async def retry_content(self, task_id: str) -> str | None:
        task = await self.get(task_id)
        return task["content"] if task else None

    async def stats(self) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
                ).fetchall()
            return {row["status"]: row["n"] for row in rows}

        return await asyncio.to_thread(_read)

    async def format_list(self, limit: int = 20, channel: str = "") -> str:
        tasks = await self.list(limit=limit, channel=channel)
        if not tasks:
            return "还没有任务记录。"
        lines = ["最近任务："]
        for task in tasks:
            stamp = datetime.fromtimestamp(task["created_at"]).strftime("%m-%d %H:%M")
            summary = " ".join(task["content"].split())[:40]
            lines.append(
                f"- {task['id']} [{task['status']}] {stamp} {task['channel']} "
                f"{task['tool_calls']}次工具 · {summary}"
            )
        return "\n".join(lines)

    async def format_detail(self, task_id: str) -> str:
        task = await self.get(task_id)
        if task is None:
            return f"未找到任务 {task_id}"
        lines = [
            f"任务 {task['id']} [{task['status']}]",
            f"渠道: {task['channel']}  创建: {_fmt(task['created_at'])}",
            f"内容: {task['content'][:500]}",
        ]
        if task["response"]:
            lines.append(f"回复: {task['response'][:800]}")
        if task["error"]:
            lines.append(f"错误: {task['error']}")
        if task["files"]:
            lines.append("文件: " + ", ".join(task["files"]))
        if task["events"]:
            lines.append("事件:")
            for event in task["events"][-15:]:
                lines.append(f"  - {event['kind']}: {event['title']} {event['detail'][:120]}")
        return "\n".join(lines)

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(_close)


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        channel=row["channel"],
        sender_id=row["sender_id"],
        chat_id=row["chat_id"],
        content=row["content"],
        status=row["status"],
        response=row["response"],
        error=row["error"],
        files=json.loads(row["files"] or "[]"),
        tool_calls=row["tool_calls"],
        events=json.loads(row["events"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _fmt(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "-"
