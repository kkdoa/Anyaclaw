"""Generated-file workspace: uploads, downloads and attachment bookkeeping."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from anyaclaw.core.errors import ToolError
from anyaclaw.tools.paths import human_size, is_within

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FileWorkspace:
    """Owns ``<data_dir>/files`` — the only place the web UI may download from."""

    def __init__(
        self,
        files_dir: str | Path,
        *,
        max_bytes: int = 25_000_000,
    ) -> None:
        self.files_dir = Path(files_dir).expanduser().resolve()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    # -- names -------------------------------------------------------------
    def sanitize(self, filename: str) -> str:
        name = Path(str(filename or "")).name
        name = _UNSAFE.sub("_", name).strip(" .")
        return name or "file"

    def unique_path(self, filename: str) -> Path:
        name = self.sanitize(filename)
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate = self.files_dir / name
        counter = 2
        while candidate.exists():
            candidate = self.files_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        return candidate

    def resolve(self, name: str) -> Path:
        """Resolve a download name and refuse traversal."""
        candidate = (self.files_dir / self.sanitize(name)).resolve()
        if not is_within(candidate, self.files_dir) or not candidate.is_file():
            raise ToolError(f"file not found: {name}")
        return candidate

    # -- write -------------------------------------------------------------
    async def save_bytes(self, filename: str, data: bytes) -> dict[str, Any]:
        if len(data) > self.max_bytes:
            raise ToolError(
                f"file too large: {human_size(len(data))} > {human_size(self.max_bytes)}"
            )
        path = self.unique_path(filename)

        def _write() -> None:
            path.write_bytes(data)

        await asyncio.to_thread(_write)
        return {
            "name": path.name,
            "size": len(data),
            "path": str(path),
            "url": f"/api/files/{path.name}",
        }

    # -- read --------------------------------------------------------------
    def list_files(self, limit: int = 100) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in sorted(self.files_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            stat = path.stat()
            entries.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "url": f"/api/files/{path.name}",
                }
            )
            if len(entries) >= limit:
                break
        return entries

    def recent_paths(self, limit: int = 5) -> list[str]:
        files = self.list_files(limit=limit)
        return [str(self.files_dir / entry["name"]) for entry in files]

    def describe(self) -> dict[str, Any]:
        files = self.list_files(limit=1)
        return {
            "dir": str(self.files_dir),
            "count": len(list(self.files_dir.glob("*"))) if self.files_dir.is_dir() else 0,
            "latest": files[0]["name"] if files else "",
        }

    def touch(self, name: str) -> None:
        """Update mtime so a regenerated file sorts to the top."""
        path = self.files_dir / self.sanitize(name)
        if path.is_file():
            path.touch()
