"""MCP (Model Context Protocol) client support.

The ``mcp`` SDK is optional: if it is not installed, no MCP tools are registered
and the rest of the runtime works normally. Servers are configured as::

    [tools.mcp_servers.filesystem]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "."]

Each discovered MCP tool is wrapped as a normal :class:`~anyaclaw.tools.base.Tool`
so the policy layer, timeouts and event stream apply to it too.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.logging import get_logger
from anyaclaw.tools.base import Tool

log = get_logger("mcp")


@dataclass(slots=True)
class MCPServerStatus:
    name: str
    connected: bool = False
    tools: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "tools": self.tools,
            "error": self.error,
        }


class MCPTool(Tool):
    """A tool exposed by an MCP server."""

    readonly = False

    def __init__(
        self,
        *,
        server: str,
        name: str,
        description: str,
        parameters: dict[str, Any],
        caller: Callable[[str, dict[str, Any]], Any],
        readonly: bool = False,
    ) -> None:
        self.server = server
        self.name = f"{server}__{name}" if server else name
        self.remote_name = name
        self.description = description or f"MCP tool {name} from {server}"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._caller = caller
        self.readonly = readonly

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            content = await self._caller(self.remote_name, kwargs)
        except Exception as exc:  # noqa: BLE001 - normalize SDK errors
            raise ToolError(f"MCP tool {self.name} failed: {exc}") from exc
        return ToolResult.ok(content)


class MCPManager:
    """Owns the stdio connections to all configured MCP servers."""

    def __init__(self, servers: dict[str, dict[str, Any]] | None = None) -> None:
        self.servers = servers or {}
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, Any] = {}
        self._status: dict[str, MCPServerStatus] = {
            name: MCPServerStatus(name=name) for name in self.servers
        }
        self.available = _sdk_available()

    async def connect_all(self, timeout: float = 30.0) -> list[MCPTool]:
        """Connect every configured server; failures are reported, not raised."""
        if not self.servers:
            return []
        if not self.available:
            log.warning("mcp SDK is not installed; skipping MCP servers")
            for status in self._status.values():
                status.error = "mcp SDK not installed (pip install anyaclaw[mcp])"
            return []

        import anyio  # noqa: F401  # ensure the SDK event-loop policy is initialised

        self._stack = AsyncExitStack()
        tools: list[MCPTool] = []
        for name, spec in self.servers.items():
            try:
                server_tools = await asyncio.wait_for(
                    self._connect_one(name, spec), timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 - one bad server must not stop the rest
                log.warning("MCP server %s failed: %s", name, exc)
                self._status[name].error = str(exc)
                continue
            self._status[name].connected = True
            self._status[name].tools = len(server_tools)
            tools.extend(server_tools)
        log.info("MCP ready: %d tools from %d servers", len(tools), len(self.servers))
        return tools

    async def _connect_one(self, name: str, spec: dict[str, Any]) -> list[MCPTool]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        assert self._stack is not None
        params = StdioServerParameters(
            command=str(spec.get("command") or ""),
            args=[str(arg) for arg in spec.get("args") or []],
            env={str(k): str(v) for k, v in (spec.get("env") or {}).items()} or None,
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._sessions[name] = session

        listing = await session.list_tools()
        tools: list[MCPTool] = []
        for item in listing.tools:
            schema = getattr(item, "inputSchema", None) or {"type": "object", "properties": {}}
            tools.append(
                MCPTool(
                    server=name,
                    name=item.name,
                    description=getattr(item, "description", "") or "",
                    parameters=schema,
                    caller=self._make_caller(name, session),
                    readonly=not _is_mutating(item.name, getattr(item, "description", "") or ""),
                )
            )
        return tools

    def _make_caller(self, server: str, session: Any) -> Callable[[str, dict], Any]:
        async def call(name: str, arguments: dict[str, Any]) -> str:
            result = await session.call_tool(name, arguments)
            chunks: list[str] = []
            for item in getattr(result, "content", []) or []:
                text = getattr(item, "text", None)
                if text is not None:
                    chunks.append(str(text))
                else:
                    chunks.append(json.dumps(_dump(item), ensure_ascii=False))
            if getattr(result, "isError", False):
                raise ToolError("".join(chunks) or "MCP tool returned an error")
            return "\n".join(chunks).strip() or "(no content)"

        return call

    def status(self) -> list[dict[str, Any]]:
        return [status.to_dict() for status in self._status.values()]

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:  # noqa: BLE001 - best effort shutdown
                log.warning("MCP shutdown error: %s", exc)
            self._stack = None
        self._sessions.clear()

    async def shutdown(self) -> None:
        await self.close()


def _sdk_available() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def _is_mutating(name: str, description: str) -> bool:
    text = f"{name} {description}".lower()
    return any(
        verb in text
        for verb in ("write", "create", "delete", "remove", "move", "rename", "edit", "execute", "run")
    )


def _dump(value: Any) -> Any:
    for attr in ("model_dump", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:  # noqa: BLE001
                continue
    return str(value)
