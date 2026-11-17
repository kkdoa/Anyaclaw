"""Tool registry: definition export, lookup and safe execution.

Execution never raises: every failure becomes ``ToolResult(is_error=True)`` so a
bad tool call degrades into information the model can react to instead of killing
the conversation. Timeouts are enforced per call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Iterator
from typing import Protocol

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.logging import get_logger, redact
from anyaclaw.metrics import Metrics

log = get_logger("tools")


class ToolLike(Protocol):
    name: str
    description: str
    parameters: dict
    readonly: bool

    async def execute(self, **kwargs): ...

    def to_definition(self) -> dict: ...


class ToolRegistry:
    """An ordered, name-keyed collection of tools."""

    def __init__(self, tools: Iterable[ToolLike] = (), *, metrics: Metrics | None = None) -> None:
        self._tools: dict[str, ToolLike] = {}
        self.metrics = metrics
        for tool in tools:
            self.register(tool)

    # -- collection --------------------------------------------------------
    def register(self, tool: ToolLike, *, replace: bool = True) -> None:
        if not getattr(tool, "name", ""):
            raise ValueError("tool must define a non-empty name")
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolLike | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        allowed = set(names)
        return ToolRegistry(
            [tool for name, tool in self._tools.items() if name in allowed],
            metrics=self.metrics,
        )

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[ToolLike]:
        return iter(self._tools.values())

    # -- definitions -------------------------------------------------------
    def definitions(self) -> list[dict]:
        return [tool.to_definition() for tool in self._tools.values()]

    # -- execution ---------------------------------------------------------
    async def execute(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(f"unknown tool: {name}")

        args = arguments or {}
        if not isinstance(args, dict):
            return ToolResult.error(f"tool arguments must be an object, got {type(args).__name__}")

        started = time.perf_counter()
        result: ToolResult | None = None
        try:
            coro = tool.execute(**args)
            result = await asyncio.wait_for(coro, timeout=timeout) if timeout else await coro
            if not isinstance(result, ToolResult):  # tolerate legacy tools
                result = ToolResult.ok(str(result))
        except TimeoutError:
            result = ToolResult.error(f"tool '{name}' timed out after {timeout:.0f}s")
        except ToolError as exc:
            result = ToolResult.error(str(exc))
        except TypeError as exc:
            result = ToolResult.error(f"invalid arguments for '{name}': {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let a tool kill the loop
            log.exception(
                "tool crashed: %s",
                name,
                extra={"extra_fields": {"tool": name, "arguments": args}},
            )
            result = ToolResult.error(f"tool '{name}' crashed: {exc}")
        else:
            log.info(
                "tool ok: %s",
                name,
                extra={
                    "extra_fields": {
                        "tool": name,
                        "arguments": args,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "preview": redact(result.content)[:200],
                    }
                },
            )
        finally:
            if self.metrics is not None:
                self.metrics.observe(
                    "tool_latency", time.perf_counter() - started, tool=name
                )
                self.metrics.incr("tool_calls", tool=name)
                if result is not None and result.is_error:
                    self.metrics.incr("tool_errors", tool=name)
        return result
