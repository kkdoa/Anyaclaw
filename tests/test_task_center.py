"""Task centre: lifecycle, events, listing and retry lookup."""

from __future__ import annotations

from pathlib import Path

from anyaclaw.services.task_center import TaskCenter


async def test_task_lifecycle(tmp_path: Path) -> None:
    center = TaskCenter(tmp_path / "tasks.db")
    task_id = await center.create(
        channel="web", sender_id="web:abc", chat_id="web:abc", content="do something"
    )
    assert task_id

    await center.update(task_id, "running")
    await center.add_tool_call(task_id, "read_file")
    await center.add_event(task_id, "tool_call", "called read_file", "{\"path\": \"a\"}")
    await center.update(task_id, "done", response="finished", files=["/tmp/a.txt"])

    task = await center.get(task_id)
    assert task is not None
    assert task["status"] == "done"
    assert task["response"] == "finished"
    assert task["files"] == ["/tmp/a.txt"]
    assert task["tool_calls"] == 1
    assert task["events"][0]["title"] == "called read_file"

    listed = await center.list(limit=10)
    assert listed and listed[0]["id"] == task_id
    await center.close()


async def test_task_filters_and_stats(tmp_path: Path) -> None:
    center = TaskCenter(tmp_path / "tasks.db")
    a = await center.create(channel="web", sender_id="u", chat_id="c", content="a")
    await center.create(channel="cli", sender_id="u", chat_id="c", content="b")
    await center.update(a, "error", error="boom")

    assert len(await center.list(channel="web")) == 1
    assert len(await center.list()) == 2
    stats = await center.stats()
    assert stats.get("error") == 1
    assert stats.get("queued") == 1

    assert await center.retry_content(a) == "a"
    assert await center.retry_content("missing") is None
    await center.close()


async def test_format_helpers(tmp_path: Path) -> None:
    center = TaskCenter(tmp_path / "tasks.db")
    task_id = await center.create(channel="qq", sender_id="u", chat_id="c", content="hello there")
    await center.update(task_id, "done", response="hi")

    listing = await center.format_list()
    assert "最近任务" in listing and task_id in listing

    detail = await center.format_detail(task_id)
    assert "任务" in detail and "hi" in detail
    assert "未找到" in await center.format_detail("nope")
    await center.close()


async def test_invalid_status_rejected(tmp_path: Path) -> None:
    center = TaskCenter(tmp_path / "tasks.db")
    task_id = await center.create(channel="web", sender_id="u", chat_id="c", content="x")
    try:
        await center.update(task_id, "bogus")
    except ValueError as exc:
        assert "invalid task status" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
    await center.close()


async def test_writes_are_committed_for_other_connections(tmp_path: Path) -> None:
    """Regression: implicit transactions used to keep a write lock forever."""
    import sqlite3

    path = tmp_path / "tasks.db"
    center = TaskCenter(path)
    task_id = await center.create(channel="web", sender_id="u", chat_id="c", content="visible")
    await center.update(task_id, "done", response="ok")

    other = sqlite3.connect(path)
    try:
        count = other.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        row = other.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        other.close()
    assert count == 1
    assert row[0] == "done"
    await center.close()


async def test_concurrent_creates(tmp_path: Path) -> None:
    import asyncio

    center = TaskCenter(tmp_path / "tasks.db")
    ids = await asyncio.gather(
        *(center.create(channel="web", sender_id="u", chat_id="c", content=f"c{i}") for i in range(8))
    )
    assert len(set(ids)) == 8
    assert len(await center.list(limit=20)) == 8
    await center.close()
