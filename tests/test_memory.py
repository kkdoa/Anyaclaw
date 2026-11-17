"""Long-term memory: dedupe, FTS/LIKE search, forget and export."""

from __future__ import annotations

from pathlib import Path

from anyaclaw.core.memory import MemoryStore


async def test_remember_and_search(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    await store.remember("User prefers TypeScript and pnpm", "preference")
    await store.remember("项目部署在家里的 NAS 上", "project")

    assert await store.count() == 2
    english = await store.search("TypeScript")
    assert english and "TypeScript" in english[0].content

    chinese = await store.search("部署")
    assert chinese and "NAS" in chinese[0].content

    await store.close()


async def test_duplicate_updates_instead_of_duplicating(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    first = await store.remember("same fact")
    second = await store.remember("same fact", "tagged")
    assert first.id == second.id
    assert await store.count() == 1
    assert (await store.list())[0].tags == "tagged"
    await store.close()


async def test_forget_by_id_and_substring(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    memory = await store.remember("temporary note about deadlines")
    await store.remember("permanent preference")

    assert await store.forget(str(memory.id)) == 1
    assert await store.count() == 1

    removed = await store.forget("permanent")
    assert removed == 1
    assert await store.count() == 0
    await store.close()


async def test_list_and_export(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    await store.remember("alpha")
    await store.remember("beta")
    listed = await store.list()
    assert len(listed) == 2

    markdown = await store.export_markdown()
    assert markdown.startswith("# MEMORY")
    assert "alpha" in markdown and "beta" in markdown
    await store.close()


async def test_empty_query_returns_nothing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    await store.remember("something")
    assert await store.search("   ") == []
    await store.close()


async def test_clear(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    await store.remember("one")
    await store.clear()
    assert await store.count() == 0
    assert await store.list() == []
    await store.close()


async def test_writes_are_committed_for_other_connections(tmp_path: Path) -> None:
    """Regression: memory writes must not leave an open transaction behind."""
    import sqlite3

    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    await store.remember("committed fact")

    other = sqlite3.connect(path)
    try:
        count = other.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        other.close()
    assert count == 1
    await store.close()
