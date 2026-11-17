"""Filesystem tools: boundaries, read/write/edit, search."""

from __future__ import annotations

from pathlib import Path

import pytest

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.fs import (
    DeletePathTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "hello.txt").write_text("line one\nline two\nline three\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return root


async def test_read_file_numbers_lines(workspace: Path) -> None:
    tool = ReadFileTool(workspace)
    result = await tool.execute(path="hello.txt")
    assert not result.is_error
    assert "1| line one" in result.content
    assert "(3 lines)" in result.content


async def test_read_file_offset_limit(workspace: Path) -> None:
    tool = ReadFileTool(workspace)
    result = await tool.execute(path="hello.txt", offset=2, limit=1)
    assert "2| line two" in result.content
    assert "line one" not in result.content


async def test_read_file_rejects_escape(workspace: Path) -> None:
    tool = ReadFileTool(workspace)
    with pytest.raises(ToolError, match="escapes the workspace"):
        await tool.execute(path="../outside.txt")


async def test_write_and_append(workspace: Path) -> None:
    tool = WriteFileTool(workspace)
    result = await tool.execute(path="nested/new.txt", content="hello")
    assert result.files and Path(result.files[0]).read_text(encoding="utf-8") == "hello"
    await tool.execute(path="nested/new.txt", content=" world", mode="append")
    assert (workspace / "nested" / "new.txt").read_text(encoding="utf-8") == "hello world"


async def test_write_create_mode_refuses_existing(workspace: Path) -> None:
    tool = WriteFileTool(workspace)
    await tool.execute(path="a.txt", content="1")
    with pytest.raises(ToolError, match="already exists"):
        await tool.execute(path="a.txt", content="2", mode="create")


async def test_edit_file_requires_context(workspace: Path) -> None:
    tool = EditFileTool(workspace)
    with pytest.raises(ToolError, match="not found"):
        await tool.execute(path="hello.txt", old_string="missing", new_string="x")

    (workspace / "dup.txt").write_text("aa aa", encoding="utf-8")
    with pytest.raises(ToolError, match="appears 2 times"):
        await tool.execute(path="dup.txt", old_string="aa", new_string="b")

    result = await tool.execute(path="dup.txt", old_string="aa", new_string="b", replace_all=True)
    assert "2 replacement" in result.content
    assert (workspace / "dup.txt").read_text(encoding="utf-8") == "b b"


async def test_list_dir_and_glob(workspace: Path) -> None:
    listing = await ListDirTool(workspace).execute(path=".")
    assert "d  src/" in listing.content
    assert "hello.txt" in listing.content

    globbed = await GlobTool(workspace).execute(pattern="**/*.py")
    assert "src/main.py" in globbed.content

    recursive = await ListDirTool(workspace).execute(path=".", recursive=True)
    assert "src/main.py" in recursive.content


async def test_grep(workspace: Path) -> None:
    result = await GrepTool(workspace).execute(pattern=r"line t\w+", include="*.txt")
    assert "hello.txt:2: line two" in result.content
    assert "hello.txt:3: line three" in result.content

    empty = await GrepTool(workspace).execute(pattern="nothing-here")
    assert "no matches" in empty.content


async def test_grep_invalid_regex(workspace: Path) -> None:
    with pytest.raises(ToolError, match="invalid regular expression"):
        await GrepTool(workspace).execute(pattern="[")


async def test_delete_path(workspace: Path) -> None:
    tool = DeletePathTool(workspace)
    await tool.execute(path="hello.txt")
    assert not (workspace / "hello.txt").exists()

    with pytest.raises(ToolError, match="recursive"):
        await tool.execute(path="src")

    result = await tool.execute(path="src", recursive=True)
    assert "deleted src" in result.content
    assert not (workspace / "src").exists()


async def test_delete_refuses_workspace_root(workspace: Path) -> None:
    with pytest.raises(ToolError, match="workspace root"):
        await DeletePathTool(workspace).execute(path=".", recursive=True)


async def test_symlink_cannot_escape(workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks unavailable")
    with pytest.raises(ToolError, match="escapes the workspace"):
        await ReadFileTool(workspace).execute(path="link.txt")


async def test_registry_wraps_tool_errors(workspace: Path) -> None:
    from anyaclaw.core.registry import ToolRegistry

    registry = ToolRegistry([ReadFileTool(workspace)])
    result = await registry.execute("read_file", {"path": "../nope"})
    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "escapes the workspace" in result.content
