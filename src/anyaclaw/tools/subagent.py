"""Subagent tool: delegate a self-contained task to a fresh agent.

The child gets its own transcript and iteration budget (and, by default, the
cheaper ``provider.subagent_model``). It may read anything but mutating tool calls
are auto-denied unless the parent's policy allows them — so a subagent cannot
silently delete files or send mail.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

from anyaclaw.config import Config
from anyaclaw.core.agent import AgentLoop
from anyaclaw.core.context import ContextBuilder
from anyaclaw.core.policy import AutoDenyConfirmer, ToolPolicy
from anyaclaw.core.provider import LLMProvider
from anyaclaw.core.registry import ToolRegistry
from anyaclaw.core.session import MemorySessionStore
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool

SUBAGENT_IDENTITY = """You are a focused subagent inside a larger agent system.

- Complete ONLY the task you were given; do not ask follow-up questions.
- Use tools when you need facts; never invent file contents or command output.
- Return a compact, self-contained answer: the parent agent will not see your
  tool calls, only your final message.
- If the task is impossible, say what blocked you and what you tried.
"""


class SpawnSubagentTool(Tool):
    name = "spawn_subagent"
    readonly = False
    description = (
        "Delegate a self-contained subtask (research, code review, summarising a "
        "file) to a fresh subagent with its own context. Returns only its final answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Complete, self-contained instruction for the subagent",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset of tool names the subagent may use",
            },
            "max_iterations": {"type": "integer", "description": "Default 8"},
        },
        "required": ["task"],
    }

    def __init__(
        self,
        *,
        config: Config,
        provider_factory: Callable[[str | None], LLMProvider],
        tools: ToolRegistry,
        depth: int = 0,
        max_depth: int = 2,
        event_sink: Callable[[Any], None] | None = None,
    ) -> None:
        self.config = config
        self.provider_factory = provider_factory
        self.tools = tools
        self.depth = depth
        self.max_depth = max_depth
        self.event_sink = event_sink

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self.depth >= self.max_depth:
            return ToolResult.error(
                f"subagent nesting limit reached ({self.max_depth}); solve the task directly"
            )

        task = str(kwargs.get("task") or "").strip()
        if not task:
            return ToolResult.error("task must not be empty")

        requested = kwargs.get("tools")
        if isinstance(requested, list) and requested:
            child_tools = self.tools.subset(
                [name for name in requested if name in self.tools and name != self.name]
            )
        else:
            child_tools = self.tools.subset(
                [name for name in self.tools.names() if name != self.name]
            )
        if len(child_tools) == 0:
            return ToolResult.error("no tools available for the subagent")

        iterations = max(1, min(int(kwargs.get("max_iterations") or 8), 24))
        settings = dataclasses.replace(
            self.config.agent,
            max_iterations=iterations,
            max_tool_calls=max(iterations * 3, 12),
            stream=False,
            parallel_tool_calls=True,
        )
        provider = self.provider_factory(self.config.provider.subagent_model or None)
        context = ContextBuilder(
            self.config,
            identity_override=SUBAGENT_IDENTITY,
            skills=None,
        )
        session_key = f"subagent:{uuid.uuid4().hex[:8]}"
        agent = AgentLoop(
            provider=provider,
            tools=child_tools,
            context=context,
            sessions=MemorySessionStore(),
            session_key=session_key,
            settings=settings,
            model=self.config.provider.subagent_model or None,
            policy=ToolPolicy(self.config.security),
            confirmer=AutoDenyConfirmer(),
            event_sink=self.event_sink,
            channel="subagent",
        )

        try:
            result = await agent.run(task)
        finally:
            await provider.aclose()

        if result.error:
            return ToolResult.error(f"subagent failed: {result.error}")
        usage = result.usage.to_dict()
        return ToolResult.ok(
            f"subagent finished ({result.iterations} iterations, "
            f"{usage.get('total_tokens', 0)} tokens):\n\n{result.content}",
            meta={"usage": usage, "iterations": result.iterations},
        )
