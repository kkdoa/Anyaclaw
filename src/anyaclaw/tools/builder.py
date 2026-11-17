"""Registry factory: assembles every enabled built-in tool group."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from anyaclaw.config import Config
from anyaclaw.core.memory import MemoryStore
from anyaclaw.core.provider import LLMProvider
from anyaclaw.core.registry import ToolRegistry
from anyaclaw.metrics import Metrics
from anyaclaw.services.workspace import FileWorkspace
from anyaclaw.tools.fs import (
    DeletePathTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from anyaclaw.tools.memory import (
    ForgetMemoryTool,
    ListMemoryTool,
    RememberTool,
    SearchMemoryTool,
)
from anyaclaw.tools.shell import ExecTool
from anyaclaw.tools.subagent import SpawnSubagentTool
from anyaclaw.tools.system import SystemInfoTool, WorkspaceInfoTool
from anyaclaw.tools.web import WebFetchTool, WebSearchTool


def build_tool_registry(
    *,
    config: Config,
    memory: MemoryStore,
    workspace: FileWorkspace,
    metrics: Metrics | None = None,
    provider_factory: Callable[[str | None], LLMProvider] | None = None,
) -> ToolRegistry:
    """Create the registry for one runtime according to ``[tools]`` switches."""
    registry = ToolRegistry(metrics=metrics)
    root = config.workspace
    security = config.security
    files_dir = config.files_dir

    if config.tools.files:
        registry.register(ReadFileTool(root, max_bytes=security.max_file_bytes))
        registry.register(WriteFileTool(root, max_bytes=security.max_file_bytes))
        registry.register(EditFileTool(root))
        registry.register(ListDirTool(root))
        registry.register(GlobTool(root))
        registry.register(GrepTool(root))
        registry.register(DeletePathTool(root))

    if config.tools.shell and security.shell_enabled:
        registry.register(
            ExecTool(
                root,
                allowed_commands=security.allowed_commands,
                deny_patterns=security.deny_patterns,
                max_output_chars=config.agent.max_tool_output_chars,
            )
        )

    if config.tools.web:
        registry.register(WebSearchTool(user_agent=config.tools.user_agent))
        registry.register(
            WebFetchTool(user_agent=config.tools.user_agent, max_chars=config.tools.max_web_chars)
        )

    if config.tools.memory:
        registry.register(RememberTool(memory))
        registry.register(SearchMemoryTool(memory))
        registry.register(ForgetMemoryTool(memory))
        registry.register(ListMemoryTool(memory))

    if config.tools.documents:
        _register_documents(registry, config, root, files_dir)

    if config.tools.subagent and provider_factory is not None:
        registry.register(
            SpawnSubagentTool(
                config=config,
                provider_factory=provider_factory,
                tools=registry,
            )
        )

    registry.register(WorkspaceInfoTool(root, files_dir, config.data_dir))
    registry.register(SystemInfoTool(config.provider.model))
    return registry


def _register_documents(
    registry: ToolRegistry, config: Config, root: Path, files_dir: Path
) -> None:
    try:
        from anyaclaw.tools.documents import CreateDocumentTool, ReadDocumentTool
    except ImportError:  # pragma: no cover - optional module
        return
    registry.register(CreateDocumentTool(root, files_dir))
    registry.register(ReadDocumentTool(root))
