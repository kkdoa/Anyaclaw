"""Filesystem boundary helpers shared by every file-touching tool.

The reference project joined ``workspace + user_path`` and called it a day — a
``../../.ssh/id_rsa`` argument escaped silently. All paths now go through
:func:`resolve_workspace_path`, which resolves symlinks and rejects anything
outside the configured workspace unless explicitly allowed.
"""

from __future__ import annotations

import os
from pathlib import Path

from anyaclaw.core.errors import ToolError


def resolve_workspace_path(
    workspace: Path,
    raw: str | os.PathLike[str],
    *,
    allow_outside: bool = False,
    must_exist: bool = False,
) -> Path:
    """Resolve ``raw`` against ``workspace`` and enforce the boundary."""
    text = str(raw or "").strip()
    if not text:
        raise ToolError("path must not be empty")

    root = Path(workspace).expanduser().resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve()
    if not allow_outside and not is_within(resolved, root):
        raise ToolError(
            f"path escapes the workspace: {raw!r} (workspace: {root}). "
            "Use a path inside the workspace or enable security.allow_outside_workspace."
        )
    if must_exist and not resolved.exists():
        raise ToolError(f"path does not exist: {raw}")
    return resolved


def is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it (case-insensitive on Windows)."""
    try:
        path = Path(path).resolve()
        root = Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable paths
        return False
    if path == root:
        return True
    if os.name == "nt":
        path_text = os.path.normcase(str(path))
        root_text = os.path.normcase(str(root))
        return path_text.startswith(root_text + os.sep)
    return root in path.parents


def portable_relative(path: Path, workspace: Path) -> str:
    """Return a workspace-relative POSIX-style path for display and storage."""
    try:
        return Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:
        return str(path)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
