"""Runtime wiring: one object that owns config, provider, tools, stores, gateway.

Entry points (CLI, tests, embedding) construct a :class:`Runtime` and call
:meth:`Runtime.run`. Everything is created lazily in :meth:`Runtime.setup` so
``anyaclaw doctor`` can inspect a config without opening network connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from anyaclaw import __version__
from anyaclaw.bus import EventHub, MessageBus
from anyaclaw.channels.base import Channel
from anyaclaw.channels.cli import CLIChannel
from anyaclaw.channels.web import WebChannel
from anyaclaw.config import Config
from anyaclaw.core.agent import AgentLoop
from anyaclaw.core.compaction import ConversationCompactor
from anyaclaw.core.context import ContextBuilder
from anyaclaw.core.memory import MemoryStore
from anyaclaw.core.openai_compat import OpenAICompatProvider
from anyaclaw.core.policy import (
    ApprovalBroker,
    AutoApproveConfirmer,
    AutoDenyConfirmer,
    ToolPolicy,
    confirm_via_stdin,
)
from anyaclaw.core.provider import LLMProvider
from anyaclaw.core.session import SessionStore, open_session_store
from anyaclaw.core.skills import SkillLoader
from anyaclaw.gateway import Gateway
from anyaclaw.logging import get_logger
from anyaclaw.metrics import Metrics
from anyaclaw.services.knowledge import KnowledgeBase
from anyaclaw.services.task_center import TaskCenter
from anyaclaw.services.workspace import FileWorkspace
from anyaclaw.tools.builder import build_tool_registry
from anyaclaw.tools.mcp import MCPManager

log = get_logger("runtime")


class Runtime:
    """Shared state for every channel of one process."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.version = __version__
        self.started_at = time.time()

        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.files_dir.mkdir(parents=True, exist_ok=True)
        config.workspace.mkdir(parents=True, exist_ok=True)

        self.metrics = Metrics()
        self.sessions: SessionStore = open_session_store(config)
        self.memory = MemoryStore(config.data_dir / "memory.db")
        self.task_center = TaskCenter(config.data_dir / "tasks.db")
        self.workspace = FileWorkspace(config.files_dir, max_bytes=config.security.max_file_bytes)
        self.knowledge = KnowledgeBase(self.memory)
        self.approvals = ApprovalBroker()
        self.hub = EventHub()
        self.bus = MessageBus()
        self.provider: LLMProvider = OpenAICompatProvider(config.provider)
        self.mcp = MCPManager(config.tools.mcp_servers) if config.tools.mcp else None
        self.tools = build_tool_registry(
            config=config,
            memory=self.memory,
            workspace=self.workspace,
            metrics=self.metrics,
            provider_factory=self.build_provider,
        )
        self.channels: list[Channel] = []
        self.gateway: Gateway | None = None
        self._setup_done = False

        self.approvals.add_listener(self._on_approval_request)

    # -- setup -------------------------------------------------------------
    async def setup(self) -> Runtime:
        if self._setup_done:
            return self
        if self.mcp is not None and self.mcp.servers:
            mcp_tools = await self.mcp.connect_all(timeout=30.0)
            for tool in mcp_tools:
                self.tools.register(tool)
        self._setup_done = True
        return self

    def build_provider(self, model: str | None = None) -> LLMProvider:
        """Always return a fresh provider so subagents can close it independently."""
        provider_config = dataclasses.replace(
            self.config.provider, model=model or self.config.provider.model
        )
        return OpenAICompatProvider(provider_config)

    # -- agents ------------------------------------------------------------
    def build_agent(self, session_key: str, channel: str) -> AgentLoop:
        config = self.config
        context = ContextBuilder(
            config,
            memory=self.memory,
            skills=SkillLoader(config.workspace / config.agent.skills_dir),
        )
        compactor = ConversationCompactor(
            self.provider,
            model=config.provider.cheap_model or config.provider.model,
            enabled=config.agent.summarize,
        )
        return AgentLoop(
            provider=self.provider,
            tools=self.tools,
            context=context,
            sessions=self.sessions,
            session_key=session_key,
            settings=config.agent,
            model=config.provider.model,
            memory=self.memory,
            metrics=self.metrics,
            policy=ToolPolicy(config.security),
            confirmer=self._confirmer_for(channel),
            compactor=compactor,
            channel=channel,
        )

    def _confirmer_for(self, channel: str) -> Callable:
        security = self.config.security
        if security.auto_approve:
            return AutoApproveConfirmer()
        if channel == "cli":
            return confirm_via_stdin
        if channel == "web":
            return self.approvals.request
        return AutoDenyConfirmer()

    def _on_approval_request(self, request) -> None:  # noqa: ANN001
        self.hub.publish({"type": "approval_request", **request.to_dict()})

    # -- channels ----------------------------------------------------------
    def _build_channels(self) -> list[Channel]:
        config = self.config
        channels: list[Channel] = []

        if config.channels.cli.enabled:
            channels.append(
                CLIChannel(
                    self.bus,
                    self.hub,
                    prompt=config.channels.cli.prompt,
                    show_reasoning=config.agent.show_reasoning,
                    on_exit=self.stop_graceful,
                )
            )
        if config.web.enabled:
            channels.append(
                WebChannel(self, host=config.web.host, port=config.web.port)
            )
        if config.channels.qq.enabled:
            from anyaclaw.channels.qq import QQChannel

            channels.append(QQChannel(config.channels.qq, self.bus))
        if config.channels.feishu.enabled:
            from anyaclaw.channels.feishu import FeishuChannel

            channels.append(FeishuChannel(config.channels.feishu, self.bus))
        if config.channels.dingtalk.enabled:
            from anyaclaw.channels.dingtalk import DingTalkChannel

            channels.append(DingTalkChannel(config.channels.dingtalk, self.bus))
        return channels

    def stop(self) -> None:
        if self.gateway is not None:
            self.gateway.request_stop()

    async def stop_graceful(self, session_key: str = "", timeout: float = 60.0) -> None:
        """Let the last turn of ``session_key`` finish (bounded), then stop."""
        gateway = self.gateway
        if gateway is None:
            return

        deadline = time.monotonic() + timeout
        worker_seen_at = 0.0
        while time.monotonic() < deadline:
            worker = gateway._workers.get(session_key) if session_key else None
            if worker is not None and not worker_seen_at:
                worker_seen_at = time.monotonic()
            busy = bool(
                worker
                and (
                    (worker.current is not None and not worker.current.done())
                    or worker.queue.qsize() > 0
                )
            )
            if not busy:
                # Give the inbound loop a moment to register a just-sent message.
                waited = time.monotonic() - deadline + timeout
                if not session_key or worker_seen_at or waited > 1.0:
                    break
            await asyncio.sleep(0.1)

        inflight = {task for task in gateway._active_turns.values() if not task.done()}
        inflight |= {
            worker.current
            for worker in gateway._workers.values()
            if worker.current is not None and not worker.current.done()
        }
        if inflight:
            await asyncio.wait(inflight, timeout=5.0)
        gateway.request_stop()

    def ensure_gateway(self) -> Gateway:
        """Build channels + gateway without starting them (web tests, embedding)."""
        if self.gateway is not None:
            return self.gateway
        self.channels = self._build_channels()
        self.gateway = Gateway(
            config=self.config,
            bus=self.bus,
            channels=self.channels,
            agent_factory=self.build_agent,
            task_center=self.task_center,
            knowledge=self.knowledge,
            metrics=self.metrics,
            hub=self.hub,
        )
        return self.gateway

    # -- main loop ---------------------------------------------------------
    async def run(self, only: Sequence[str] | None = None) -> None:
        await self.setup()
        channels = self._build_channels()
        if only is not None:
            allowed = set(only)
            channels = [channel for channel in channels if channel.name in allowed]
        if not channels:
            raise RuntimeError(
                "no channel enabled: set channels.cli.enabled = true or web.enabled = true"
            )
        self.channels = channels
        self.gateway = Gateway(
            config=self.config,
            bus=self.bus,
            channels=channels,
            agent_factory=self.build_agent,
            task_center=self.task_center,
            knowledge=self.knowledge,
            metrics=self.metrics,
            hub=self.hub,
        )
        try:
            await self.gateway.run()
        finally:
            await self.shutdown()

    async def run_turn(self, message: str, *, session_key: str = "cli:local", channel: str = "cli"):
        await self.setup()
        gateway = self.ensure_gateway()
        return await gateway.run_turn(
            session_key=session_key, channel=channel, content=message
        )

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.provider.aclose()
        if self.mcp is not None:
            await self.mcp.close()
        for store in (self.sessions, self.memory, self.task_center):
            close = getattr(store, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        log.info("runtime stopped")

    # -- introspection -----------------------------------------------------
    def mcp_status(self) -> list[dict]:
        return self.mcp.status() if self.mcp is not None else []

    @property
    def workspace_path(self) -> Path:
        return self.config.workspace
