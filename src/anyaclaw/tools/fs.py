"""Filesystem tools, all confined to the workspace.

Every path argument passes through :func:`resolve_workspace_path`; symlinks are
resolved before the boundary check, so ``link-to-/etc/passwd`` cannot be used to
escape either.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool
from anyaclaw.tools.paths import (
    ensure_parent,
    human_size,
    portable_relative,
    resolve_workspace_path,
)

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".anyaclaw",
    "dist",
    "build",
}

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".pdf",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".wav",
}


class ReadFileTool(Tool):
    name = "read_file"
    readonly = True
    description = (
        "Read a UTF-8 text file inside the workspace. Returns numbered lines. "
        "Use offset/limit for large files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "offset": {"type": "integer", "description": "1-based first line", "minimum": 1},
            "limit": {"type": "integer", "description": "Max lines to return", "minimum": 1},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: str | Path, *, max_bytes: int = 2_000_000) -> None:
        self.workspace = Path(workspace)
        self.max_bytes = max_bytes

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = resolve_workspace_path(self.workspace, kwargs.get("path", ""), must_exist=True)
        if not path.is_file():
            raise ToolError(f"not a file: {kwargs.get('path')}")
        size = path.stat().st_size
        if size > self.max_bytes:
            raise ToolError(
                f"file is too large to read ({human_size(size)} > {human_size(self.max_bytes)})"
            )
        offset = max(int(kwargs.get("offset") or 1), 1)
        limit = max(int(kwargs.get("limit") or 2000), 1)

        def _read() -> tuple[str, int]:
            raw = path.read_bytes()
            if b"\x00" in raw[:2048]:
                raise ToolError(f"binary file (not text): {kwargs.get('path')}")
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()
            selected = lines[offset - 1 : offset - 1 + limit]
            numbered = "\n".join(f"{offset + i}| {line}" for i, line in enumerate(selected))
            return numbered, len(lines)

        body, total = await asyncio.to_thread(_read)
        header = f"# {portable_relative(path, self.workspace)} ({total} lines)"
        if offset > 1 or offset - 1 + limit < total:
            header += f" showing lines {offset}-{min(offset - 1 + limit, total)}"
        return ToolResult.ok(f"{header}\n\n{body}", meta={"lines": total})


class WriteFileTool(Tool):
    name = "write_file"
    readonly = False
    description = (
        "Write or append UTF-8 text to a file inside the workspace. "
        "Parent directories are created automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "content": {"type": "string", "description": "Text to write"},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append", "create"],
                "description": "overwrite (default), append, or create (fail if exists)",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: str | Path, *, max_bytes: int = 5_000_000) -> None:
        self.workspace = Path(workspace)
        self.max_bytes = max_bytes

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = resolve_workspace_path(self.workspace, kwargs.get("path", ""))
        content = kwargs.get("content", "")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            raise ToolError(f"content too large ({human_size(len(data))})")
        mode = str(kwargs.get("mode") or "overwrite").lower()
        if mode not in {"overwrite", "append", "create"}:
            raise ToolError(f"invalid mode: {mode}")

        def _write() -> int:
            ensure_parent(path)
            if mode == "create":
                if path.exists():
                    raise ToolError(f"file already exists: {kwargs.get('path')}")
                path.write_bytes(data)
            elif mode == "append":
                with path.open("ab") as handle:
                    handle.write(data)
            else:
                path.write_bytes(data)
            return path.stat().st_size

        size = await asyncio.to_thread(_write)
        verb = {"overwrite": "wrote", "append": "appended to", "create": "created"}[mode]
        return ToolResult.ok(
            f"{verb} {portable_relative(path, self.workspace)} ({human_size(size)})",
            files=[str(path)],
            meta={"bytes": size},
        )


class EditFileTool(Tool):
    name = "edit_file"
    readonly = False
    description = (
        "Replace an exact string in a workspace file. By default the old string "
        "must appear exactly once, which prevents accidental mass edits."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = resolve_workspace_path(self.workspace, kwargs.get("path", ""), must_exist=True)
        old = kwargs.get("old_string")
        new = kwargs.get("new_string")
        if not isinstance(old, str) or old == "":
            raise ToolError("old_string must be a non-empty string")
        if not isinstance(new, str):
            raise ToolError("new_string must be a string")
        replace_all = bool(kwargs.get("replace_all"))

        def _edit() -> tuple[int, int]:
            text = path.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                raise ToolError("old_string was not found in the file")
            if count > 1 and not replace_all:
                raise ToolError(
                    f"old_string appears {count} times; add more context or set replace_all=true"
                )
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            path.write_text(updated, encoding="utf-8")
            return count, len(updated)

        count, size = await asyncio.to_thread(_edit)
        return ToolResult.ok(
            f"edited {portable_relative(path, self.workspace)}: "
            f"{count} replacement(s), now {human_size(size)}",
            files=[str(path)],
        )


class ListDirTool(Tool):
    name = "list_dir"
    readonly = True
    description = "List directory entries inside the workspace, directories first."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path, default '.'"},
            "recursive": {"type": "boolean", "description": "Walk subdirectories"},
            "pattern": {"type": "string", "description": "Glob filter for file names"},
        },
    }

    def __init__(self, workspace: str | Path, *, max_entries: int = 300) -> None:
        self.workspace = Path(workspace)
        self.max_entries = max_entries

    async def execute(self, **kwargs: Any) -> ToolResult:
        root = resolve_workspace_path(self.workspace, kwargs.get("path") or ".", must_exist=True)
        if not root.is_dir():
            raise ToolError(f"not a directory: {kwargs.get('path')}")
        recursive = bool(kwargs.get("recursive"))
        pattern = str(kwargs.get("pattern") or "")

        def _list() -> list[str]:
            entries: list[str] = []
            walker = root.rglob("*") if recursive else root.iterdir()
            for item in walker:
                rel = item.relative_to(root)
                if any(part in _SKIP_DIRS for part in rel.parts):
                    continue
                if pattern and item.is_file() and not fnmatch.fnmatch(item.name, pattern):
                    continue
                try:
                    if item.is_dir():
                        entries.append(f"d  {rel.as_posix()}/")
                    else:
                        entries.append(f"f  {human_size(item.stat().st_size):>8}  {rel.as_posix()}")
                except OSError:
                    continue
                if len(entries) >= self.max_entries * 4:
                    break
            entries.sort(key=lambda line: (not line.startswith("d"), line.lower()))
            return entries[: self.max_entries]

        entries = await asyncio.to_thread(_list)
        if not entries:
            return ToolResult.ok(f"(empty) {portable_relative(root, self.workspace)}")
        shown = "\n".join(entries)
        total = len(entries)
        if total >= self.max_entries:
            shown += f"\n… (limited to {self.max_entries} entries)"
        return ToolResult.ok(shown, meta={"entries": total})


class GlobTool(Tool):
    name = "glob"
    readonly = True
    description = "Find files by glob pattern, e.g. '**/*.py' or 'src/**/*.ts'."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern relative to the workspace"},
            "limit": {"type": "integer", "description": "Max results (default 200)"},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    async def execute(self, **kwargs: Any) -> ToolResult:
        pattern = str(kwargs.get("pattern") or "").strip()
        if not pattern:
            raise ToolError("pattern must not be empty")
        if Path(pattern).is_absolute():
            raise ToolError("pattern must be relative to the workspace")
        limit = max(int(kwargs.get("limit") or 200), 1)

        def _glob() -> list[str]:
            found: list[str] = []
            for item in self.workspace.glob(pattern):
                if not item.is_file():
                    continue
                rel = item.relative_to(self.workspace)
                if any(part in _SKIP_DIRS for part in rel.parts):
                    continue
                found.append(rel.as_posix())
                if len(found) >= limit:
                    break
            return sorted(found)

        matches = await asyncio.to_thread(_glob)
        if not matches:
            return ToolResult.ok(f"no files matched {pattern!r}")
        return ToolResult.ok("\n".join(matches), meta={"matches": len(matches)})


class GrepTool(Tool):
    name = "grep"
    readonly = True
    description = "Search file contents with a regular expression inside the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "include": {"type": "string", "description": "File glob, e.g. '*.py'"},
            "path": {"type": "string", "description": "Directory to search, default '.'"},
            "max_results": {"type": "integer", "description": "Default 100"},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: str | Path, *, max_file_bytes: int = 1_000_000) -> None:
        self.workspace = Path(workspace)
        self.max_file_bytes = max_file_bytes

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_pattern = str(kwargs.get("pattern") or "")
        try:
            regex = re.compile(raw_pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        include = str(kwargs.get("include") or "*")
        root = resolve_workspace_path(self.workspace, kwargs.get("path") or ".", must_exist=True)
        limit = max(int(kwargs.get("max_results") or 100), 1)

        def _search() -> list[str]:
            results: list[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
                for name in filenames:
                    if not fnmatch.fnmatch(name, include):
                        continue
                    path = Path(dirpath) / name
                    if path.suffix.lower() in BINARY_SUFFIXES:
                        continue
                    try:
                        if path.stat().st_size > self.max_file_bytes:
                            continue
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    rel = portable_relative(path, self.workspace)
                    for number, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            results.append(f"{rel}:{number}: {line.strip()[:240]}")
                            if len(results) >= limit:
                                return results
            return results

        matches = await asyncio.to_thread(_search)
        if not matches:
            return ToolResult.ok(f"no matches for {raw_pattern!r}")
        body = "\n".join(matches)
        if len(matches) >= limit:
            body += f"\n… (limited to {limit} matches)"
        return ToolResult.ok(body, meta={"matches": len(matches)})


class DeletePathTool(Tool):
    name = "delete_path"
    readonly = False
    description = "Delete a file or directory inside the workspace. Requires confirmation by default."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path"},
            "recursive": {"type": "boolean", "description": "Required for non-empty directories"},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = resolve_workspace_path(self.workspace, kwargs.get("path", ""), must_exist=True)
        if path == self.workspace.resolve():
            raise ToolError("refusing to delete the workspace root")
        if path.is_dir():
            if not kwargs.get("recursive"):
                raise ToolError("directory deletion requires recursive=true")
            children = list(path.iterdir())
            if children and not kwargs.get("recursive"):
                raise ToolError("directory is not empty")
        recursive = bool(kwargs.get("recursive"))

        def _delete() -> int:
            removed = 0
            if path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                        removed += 1
                path.rmdir() if recursive else None
            else:
                path.unlink()
                removed = 1
            return removed

        removed = await asyncio.to_thread(_delete)
        return ToolResult.ok(
            f"deleted {portable_relative(path, self.workspace)} ({removed} entries)"
        )
