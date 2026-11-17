"""Session stores: SQLite ordering, listing, clearing and JSONL migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anyaclaw.core.session import (
    JsonlSessionStore,
    MemorySessionStore,
    SqliteSessionStore,
)


async def test_sqlite_roundtrip(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    await store.append("cli:a", {"role": "user", "content": "hello"})
    await store.append("cli:a", {"role": "assistant", "content": "hi"})

    history = await store.history("cli:a")
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["content"] == "hello"
    assert await store.count("cli:a") == 2

    limited = await store.history("cli:a", limit=1)
    assert len(limited) == 1 and limited[0]["role"] == "assistant"
    await store.close()


async def test_sqlite_sessions_listing(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    await store.append("web:one", {"role": "user", "content": "first topic"})
    await store.append("web:one", {"role": "assistant", "content": "ok"})
    await store.append("cli:two", {"role": "user", "content": "another"})

    sessions = await store.list_sessions()
    keys = {session.key for session in sessions}
    assert keys == {"web:one", "cli:two"}
    one = next(session for session in sessions if session.key == "web:one")
    assert one.messages == 2
    assert one.title == "first topic"
    assert one.channel == "web"
    await store.close()


async def test_sqlite_clear(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    await store.append("cli:a", {"role": "user", "content": "x"})
    await store.clear("cli:a")
    assert await store.history("cli:a") == []
    assert await store.list_sessions() == []
    await store.close()


async def test_jsonl_store(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append("cli:a", {"role": "user", "content": "one"})
    await store.append("cli:a", {"role": "assistant", "content": "two"})
    history = await store.history("cli:a")
    assert [message["content"] for message in history] == ["one", "two"]
    assert await store.count("cli:a") == 2

    sessions = await store.list_sessions()
    assert sessions and sessions[0].key == "cli:a"
    await store.clear("cli:a")
    assert await store.history("cli:a") == []


async def test_jsonl_import(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "cli_local.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "from jsonl", "timestamp": 1.0}),
                json.dumps({"role": "assistant", "content": "reply"}),
            ]
        ),
        encoding="utf-8",
    )
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    imported = await store.import_jsonl_dir(legacy)
    assert imported == 2
    history = await store.history("cli:local")
    assert history[0]["content"] == "from jsonl"
    assert "timestamp" not in history[0]
    await store.close()


async def test_memory_store() -> None:
    store = MemorySessionStore()
    await store.append("cli:a", {"role": "user", "content": "hi"})
    assert (await store.history("cli:a"))[0]["content"] == "hi"
    assert await store.count("cli:a") == 1
    await store.clear("cli:a")
    assert await store.count("cli:a") == 0


@pytest.mark.parametrize("backend", ["sqlite", "jsonl"])
def test_open_session_store(tmp_path: Path, backend: str) -> None:
    from anyaclaw.config import Config
    from anyaclaw.core.session import open_session_store

    config = Config()
    config.storage.data_dir = str(tmp_path)
    config.storage.session_backend = backend
    store = open_session_store(config)
    if backend == "sqlite":
        assert isinstance(store, SqliteSessionStore)
    else:
        assert isinstance(store, JsonlSessionStore)
