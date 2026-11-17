"""Tool base class and helpers.

A tool is a small, self-describing unit: ``name``, ``description``, a JSON-Schema
``parameters`` object and an async ``execute`` returning :class:`ToolResult`.
``readonly`` is meaningful: read-only tools are auto-approved by the policy and
may run concurrently inside a turn, while mutating tools can require confirmation.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult

ParameterSchema = dict[str, Any]


class Tool(ABC):
    """Base class for every built-in or third-party tool."""

    name: str = ""
    description: str = ""
    parameters: ParameterSchema = {"type": "object", "properties": {}}
    readonly: bool = False

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Run the tool. Raise :class:`ToolError` for expected failures."""

    def to_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def summary(self, arguments: dict[str, Any]) -> str:
        """One-line human description used in confirmation prompts and logs."""
        items = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        return f"{self.name}({items})"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"


class FunctionTool(Tool):
    """Wrap a plain function as a tool.

    Useful for tests and for plugins:

    >>> def add(a: int, b: int) -> int: return a + b
    >>> tool = FunctionTool("add", "Add two ints", {...}, add)
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: ParameterSchema,
        func: Callable[..., Any],
        *,
        readonly: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.readonly = readonly
        self._func = func

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            if inspect.iscoroutinefunction(self._func):
                value = await self._func(**kwargs)
            else:
                value = await asyncio.to_thread(self._func, **kwargs)
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"invalid arguments for {self.name}: {exc}") from exc
        if isinstance(value, ToolResult):
            return value
        return ToolResult.ok(str(value))
