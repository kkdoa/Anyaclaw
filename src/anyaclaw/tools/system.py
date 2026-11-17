"""Small informational tools: workspace layout, runtime facts, task listing."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from anyaclaw import __version__
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool
from anyaclaw.tools.paths import human_size


class WorkspaceInfoTool(Tool):
    name = "workspace_info"
    readonly = True
    description = "Describe the workspace: paths, top-level entries, generated files."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, workspace: Path, files_dir: Path, data_dir: Path) -> None:
        self.workspace = workspace
        self.files_dir = files_dir
        self.data_dir = data_dir

    async def execute(self, **kwargs: Any) -> ToolResult:
        entries = []
        try:
            for item in sorted(self.workspace.iterdir())[:30]:
                suffix = "/" if item.is_dir() else f" {human_size(item.stat().st_size)}"
                entries.append(f"- {item.name}{suffix}")
        except OSError:
            entries = ["(unavailable)"]

        generated = []
        if self.files_dir.is_dir():
            generated = [
                f"- {path.name} ({human_size(path.stat().st_size)})"
                for path in sorted(self.files_dir.iterdir(), key=lambda p: p.stat().st_mtime)[-10:]
                if path.is_file()
            ]

        lines = [
            f"workspace: {self.workspace}",
            f"generated files: {self.files_dir}",
            f"data dir: {self.data_dir}",
            "",
            "top-level entries:",
            *(entries or ["(empty)"]),
            "",
            "recent generated files:",
            *(generated or ["(none)"]),
        ]
        return ToolResult.ok("\n".join(lines))


class SystemInfoTool(Tool):
    name = "system_info"
    readonly = True
    description = "Report OS, Python, Anyaclaw version and current time."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, model: str = "") -> None:
        self.model = model

    async def execute(self, **kwargs: Any) -> ToolResult:
        from datetime import datetime

        lines = [
            f"anyaclaw: {__version__}",
            f"model: {self.model or '(unknown)'}",
            f"python: {sys.version.split()[0]}",
            f"platform: {platform.platform()}",
            f"cwd: {os.getcwd()}",
            f"time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        ]
        return ToolResult.ok("\n".join(lines))
