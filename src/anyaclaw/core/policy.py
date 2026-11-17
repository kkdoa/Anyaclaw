"""Tool execution policy and interactive approval.

Two separate concerns live here:

1. :class:`ToolPolicy` decides, per tool call, whether it is ``allow``ed,
   needs ``confirm``ation, or is ``deny``ied. Read-only tools are always allowed.
2. :class:`ApprovalBroker` turns a ``confirm`` decision into an actual user
   interaction. The same broker serves the CLI prompt and the Web approval card;
   messaging channels default to deny unless ``auto_approve`` is set, because a
   silent "yes" from a chat bot is worse than a clear refusal.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from anyaclaw.config import SecurityConfig
from anyaclaw.logging import get_logger

log = get_logger("policy")

Action = str  # "allow" | "confirm" | "deny"
Confirmer = Callable[["ConfirmationRequest"], Awaitable[bool]]


@dataclass(slots=True)
class ConfirmationRequest:
    """A pending "may I run this tool?" question."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    session_key: str = ""
    channel: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "reason": self.reason,
            "session": self.session_key,
            "channel": self.channel,
        }


class ToolPolicy:
    """Maps a tool call to an allow/confirm/deny decision."""

    def __init__(self, security: SecurityConfig) -> None:
        self.security = security

    def action_for(self, tool_name: str, *, readonly: bool = False) -> Action:
        if readonly:
            return "allow"
        if self.security.auto_approve:
            return "allow"
        override = self.security.tool_policies.get(tool_name)
        if override:
            return override
        return self.security.tool_policy

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        readonly: bool = False,
        summary: str = "",
    ) -> tuple[Action, str]:
        action = self.action_for(tool_name, readonly=readonly)
        reason = summary or tool_name
        if action == "deny":
            log.info("tool denied by policy: %s", tool_name)
        return action, reason


class AutoApproveConfirmer:
    async def __call__(self, request: ConfirmationRequest) -> bool:
        return True


class AutoDenyConfirmer:
    async def __call__(self, request: ConfirmationRequest) -> bool:
        log.warning(
            "auto-denied tool %s on channel %s", request.tool, request.channel or "unknown"
        )
        return False


class ApprovalBroker:
    """Collects human approvals asynchronously.

    The web UI calls :meth:`resolve` when a user clicks approve/deny; the agent
    simply awaits :meth:`request`. Timed-out requests are denied, never approved.
    """

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout
        self._pending: dict[str, tuple[asyncio.Future[bool], ConfirmationRequest]] = {}
        self._listeners: list[Callable[[ConfirmationRequest], None]] = []

    def add_listener(self, listener: Callable[[ConfirmationRequest], None]) -> None:
        self._listeners.append(listener)

    async def request(self, request: ConfirmationRequest) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[request.id] = (future, request)
        for listener in self._listeners:
            try:
                listener(request)
            except Exception:  # noqa: BLE001 - listeners are best effort
                log.exception("approval listener failed")
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError:
            log.warning("approval timed out: %s", request.tool)
            return False
        finally:
            self._pending.pop(request.id, None)

    def resolve(self, request_id: str, approved: bool) -> bool:
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        future, _request = entry
        if not future.done():
            future.set_result(approved)
        return True

    def pending(self) -> list[dict[str, Any]]:
        return [request.to_dict() for _future, request in self._pending.values()]


async def confirm_via_stdin(request: ConfirmationRequest) -> bool:
    """Ask on the terminal without blocking the event loop."""
    print(f"\n[approval] {request.tool}: {request.reason}")
    answer = await asyncio.to_thread(input, "run this? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}
