"""Memory tools: let the model decide what is worth remembering."""

from __future__ import annotations

from typing import Any

from anyaclaw.core.memory import MemoryStore
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool


class RememberTool(Tool):
    name = "remember"
    readonly = False
    description = (
        "Store a durable fact about the user or the project (preferences, names, "
        "decisions, credentials-free context). Do not store small talk."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "One self-contained fact"},
            "tags": {"type": "string", "description": "Comma-separated tags, optional"},
        },
        "required": ["content"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, **kwargs: Any) -> ToolResult:
        content = str(kwargs.get("content") or "").strip()
        tags = str(kwargs.get("tags") or "").strip()
        if not content:
            return ToolResult.error("content must not be empty")
        memory = await self.memory.remember(content, tags)
        return ToolResult.ok(f"remembered #{memory.id}: {memory.content}")


class SearchMemoryTool(Tool):
    name = "search_memory"
    readonly = True
    description = "Search stored long-term memories by keywords."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "Default 5"},
        },
        "required": ["query"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return ToolResult.error("query must not be empty")
        limit = max(1, min(int(kwargs.get("limit") or 5), 20))
        memories = await self.memory.search(query, limit=limit)
        if not memories:
            return ToolResult.ok("no memories found")
        lines = [f"#{memory.id} {memory.content}" for memory in memories]
        return ToolResult.ok("\n".join(lines), meta={"count": len(memories)})


class ForgetMemoryTool(Tool):
    name = "forget"
    readonly = False
    description = "Delete a stored memory by id, exact text or substring."
    parameters = {
        "type": "object",
        "properties": {"target": {"type": "string", "description": "Memory id or text"}},
        "required": ["target"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = str(kwargs.get("target") or "").strip()
        if not target:
            return ToolResult.error("target must not be empty")
        removed = await self.memory.forget(target)
        return ToolResult.ok(f"forgot {removed} memory entries")


class ListMemoryTool(Tool):
    name = "list_memory"
    readonly = True
    description = "List the most recent long-term memories."
    parameters = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "Default 20"}},
    }

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, **kwargs: Any) -> ToolResult:
        limit = max(1, min(int(kwargs.get("limit") or 20), 100))
        memories = await self.memory.list(limit)
        if not memories:
            return ToolResult.ok("memory is empty")
        return ToolResult.ok(
            "\n".join(f"#{memory.id} {memory.content}" for memory in memories),
            meta={"count": len(memories)},
        )
