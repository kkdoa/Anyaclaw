"""Gateway: routes inbound messages to per-session agents and events to UIs.

Design differences from the reference project:

* each session has a **serial queue** instead of rejecting the second message —
  people type two messages in a row all the time;
* cancellation targets the running turn, not the worker;
* agent events are published on an :class:`~anyaclaw.bus.EventHub` so the web UI
  streams without polling, and task records are updated as the turn progresses;
* channel failures are isolated: one broken adapter never stops the gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anyaclaw.bus import EventHub, InboundMessage, MessageBus, OutboundMessage
from anyaclaw.channels.base import Channel
from anyaclaw.config import Config
from anyaclaw.core.agent import AgentLoop
from anyaclaw.core.types import AgentResult, Event
from anyaclaw.logging import get_logger
from anyaclaw.metrics import Metrics
from anyaclaw.services.knowledge import KnowledgeBase
from anyaclaw.services.task_center import TaskCenter

log = get_logger("gateway")


@dataclass
class _SessionWorker:
    queue: asyncio.Queue[InboundMessage] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    current: asyncio.Task | None = None


class Gateway:
    def __init__(
        self,
        *,
        config: Config,
        bus: MessageBus,
        channels: list[Channel],
        agent_factory: Callable[[str, str], AgentLoop],
        task_center: TaskCenter | None = None,
        knowledge: KnowledgeBase | None = None,
        metrics: Metrics | None = None,
        hub: EventHub | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.channels = channels
        self.agent_factory = agent_factory
        self.task_center = task_center
        self.knowledge = knowledge
        self.metrics = metrics
        self.hub = hub or EventHub()

        self._channel_map = {channel.name: channel for channel in channels}
        self._agents: dict[str, AgentLoop] = {}
        self._workers: dict[str, _SessionWorker] = {}
        self._active_turns: dict[str, asyncio.Task] = {}
        self._closed = False
        self._started_at = time.time()

    # -- lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        for channel in self.channels:
            tasks.append(asyncio.create_task(self._run_channel(channel), name=f"ch:{channel.name}"))
        tasks.append(asyncio.create_task(self._inbound_loop(), name="gateway:inbound"))
        tasks.append(asyncio.create_task(self._outbound_loop(), name="gateway:outbound"))
        self._tasks = tasks
        log.info(
            "gateway started",
            extra={"extra_fields": {"channels": [c.name for c in self.channels]}},
        )
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()

    def request_stop(self) -> None:
        """Ask every loop to wind down (used by the CLI ``/exit`` command)."""
        self._closed = True
        for task in getattr(self, "_tasks", []):
            task.cancel()

    async def _run_channel(self, channel: Channel) -> None:
        try:
            await channel.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate adapters
            log.exception("channel %s failed: %s", channel.name, exc)

    async def shutdown(self) -> None:
        self._closed = True
        workers = list(self._workers.values())

        # Wake idle workers and let any running turn finish before stores close.
        for worker in workers:
            with contextlib.suppress(asyncio.QueueFull):
                worker.queue.put_nowait(None)  # type: ignore[arg-type]
        running = [worker.task for worker in workers if worker.task is not None]
        if running:
            done, pending = await asyncio.wait(running, timeout=20.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        for worker in workers:
            if worker.current is not None and not worker.current.done():
                worker.current.cancel()
        for channel in self.channels:
            try:
                await channel.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("channel %s stop failed: %s", channel.name, exc)
        self._workers.clear()
        self._agents.clear()
        log.info("gateway stopped")

    # -- inbound -----------------------------------------------------------
    async def _inbound_loop(self) -> None:
        while not self._closed:
            try:
                message = await self.bus.consume_inbound()
            except asyncio.CancelledError:
                break
            try:
                self.hub.publish(
                    {
                        "type": "inbound",
                        "channel": message.channel,
                        "sender_id": message.sender_id,
                        "chat_id": message.chat_id,
                        "content": message.content[:500],
                    }
                )
                if not self._sender_allowed(message):
                    await self._send_text(message, "你没有权限使用这个机器人。")
                    continue
                if await self._handle_command(message):
                    continue
                await self._enqueue(message)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep consuming
                log.exception("inbound handling failed")

    async def _enqueue(self, message: InboundMessage) -> None:
        key = message.session_key
        worker = self._workers.get(key)
        if worker is None:
            worker = _SessionWorker()
            self._workers[key] = worker
            worker.task = asyncio.create_task(
                self._session_worker(key, worker), name=f"session:{key}"
            )
        if worker.current is not None and not worker.current.done():
            await self._send_text(message, "收到，当前任务完成后处理这条消息。")
        await worker.queue.put(message)

    async def _session_worker(self, key: str, worker: _SessionWorker) -> None:
        while not self._closed:
            message = await worker.queue.get()
            if message is None:  # pragma: no cover - shutdown sentinel
                break
            current = asyncio.create_task(self._process(key, message), name=f"turn:{key}")
            worker.current = current
            try:
                await current
            except asyncio.CancelledError:
                if current.cancelled():
                    continue
                raise
            except Exception:  # noqa: BLE001
                log.exception("session worker error")
            finally:
                worker.current = None

    async def _process(self, key: str, message: InboundMessage) -> None:
        agent = self._get_agent(key, message.channel)
        task_id = ""
        if self.task_center is not None:
            try:
                task_id = await self.task_center.create(
                    channel=message.channel,
                    sender_id=message.sender_id,
                    chat_id=message.chat_id,
                    content=message.content,
                )
                await self.task_center.update(task_id, "running")
            except Exception:  # noqa: BLE001 - task logging must not block replies
                log.warning("could not create task record", exc_info=True)
                task_id = ""

        agent.event_sink = lambda event: self._on_event(message, task_id, event)

        try:
            result = await agent.run(message.content)
        except asyncio.CancelledError:
            if self.task_center is not None and task_id:
                await self.task_center.update(task_id, "cancelled")
            await self._send_text(message, "任务已取消。")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("agent run failed")
            if self.task_center is not None and task_id:
                await self.task_center.update(task_id, "error", error=str(exc))
            await self._send_text(message, f"处理消息时出错：{exc}")
            return

        if self.task_center is not None and task_id:
            try:
                await self.task_center.update(
                    task_id,
                    "error" if result.error else "done",
                    response=result.content,
                    error=result.error,
                    files=result.files,
                )
            except Exception:  # noqa: BLE001 - closed store during shutdown
                log.debug("could not finalise task record", exc_info=True)

        if result.error:
            await self._send_text(message, f"任务失败：{result.error}", files=result.files)
        else:
            await self._send_text(message, result.content, files=result.files)

    # -- direct turns (Web UI) ---------------------------------------------
    async def run_turn(
        self,
        *,
        session_key: str,
        channel: str,
        content: str,
        sink: Callable[[Event], None] | None = None,
    ) -> tuple[AgentResult, str]:
        """Run one turn synchronously for an HTTP/SSE request."""
        agent = self._get_agent(session_key, channel)
        task_id = ""
        if self.task_center is not None:
            task_id = await self.task_center.create(
                channel=channel, sender_id=session_key, chat_id=session_key, content=content
            )
            await self.task_center.update(task_id, "running")

        def forward(event: Event) -> None:
            if sink is not None:
                sink(event)
            self._publish_event(session_key, channel, task_id, event)

        agent.event_sink = forward
        current = asyncio.current_task()
        if current is not None:
            self._active_turns[session_key] = current
        try:
            result = await agent.run(content)
        except asyncio.CancelledError:
            if self.task_center is not None and task_id:
                await self.task_center.update(task_id, "cancelled")
            raise
        finally:
            self._active_turns.pop(session_key, None)

        if self.task_center is not None and task_id:
            try:
                await self.task_center.update(
                    task_id,
                    "error" if result.error else "done",
                    response=result.content,
                    error=result.error,
                    files=result.files,
                )
            except Exception:  # noqa: BLE001 - closed store during shutdown
                log.debug("could not finalise task record", exc_info=True)
        return result, task_id

    def cancel_session(self, session_key: str) -> bool:
        task = self._active_turns.get(session_key)
        if task is None or task.done():
            worker = self._workers.get(session_key)
            task = worker.current if worker else None
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # -- outbound ----------------------------------------------------------
    async def _outbound_loop(self) -> None:
        while not self._closed:
            try:
                outbound = await self.bus.consume_outbound()
            except asyncio.CancelledError:
                break
            channel = self._channel_map.get(outbound.channel)
            if channel is None:
                log.warning("no channel for outbound message: %s", outbound.channel)
                continue
            try:
                await channel.send(outbound)
            except Exception:  # noqa: BLE001
                log.exception("channel %s send failed", outbound.channel)

    # -- commands ----------------------------------------------------------
    async def _handle_command(self, message: InboundMessage) -> bool:
        text = message.content.strip()
        lowered = text.lower()
        key = message.session_key

        if lowered in {"/cancel", "cancel", "停止", "取消", "取消任务"}:
            if self.cancel_session(key):
                await self._send_text(message, "正在取消当前任务…")
            else:
                await self._send_text(message, "当前没有正在执行的任务。")
            return True

        if lowered in {"/status", "status", "进度"}:
            worker = self._workers.get(key)
            busy = bool(worker and worker.current and not worker.current.done())
            queued = worker.queue.qsize() if worker else 0
            await self._send_text(
                message,
                f"状态：{'执行中' if busy else '空闲'}，排队 {queued} 条。",
            )
            return True

        if lowered in {"/clear", "clear", "清空"}:
            await self._get_agent(key, message.channel).clear()
            await self._send_text(message, "会话历史已清空。")
            return True

        if lowered in {"/tools", "tools", "工具"}:
            agent = self._get_agent(key, message.channel)
            await self._send_text(message, "可用工具：" + ", ".join(agent.tools.names()))
            return True

        if lowered in {"/tasks", "tasks", "任务"} and self.task_center is not None:
            await self._send_text(message, await self.task_center.format_list(limit=15))
            return True

        if lowered.startswith("/task ") and self.task_center is not None:
            task_id = text.split(maxsplit=1)[1]
            await self._send_text(message, await self.task_center.format_detail(task_id))
            return True

        if self.knowledge is not None:
            if lowered in {"/memory", "memory", "记忆"}:
                await self._send_text(message, await self.knowledge.list_text())
                return True
            if lowered.startswith("/memory "):
                await self._send_text(
                    message, await self.knowledge.list_text(text.split(maxsplit=1)[1])
                )
                return True
            if lowered.startswith("/remember "):
                await self._send_text(
                    message, await self.knowledge.remember_text(text.split(maxsplit=1)[1])
                )
                return True
            if lowered.startswith("/forget "):
                await self._send_text(
                    message, await self.knowledge.forget_text(text.split(maxsplit=1)[1])
                )
                return True

        if lowered in {"/help", "help", "帮助"}:
            lines = [
                "命令：",
                "/clear 清空会话  /tools 查看工具  /tasks 任务  /task <id> 任务详情",
                "/status 状态  /cancel 取消  /memory 记忆  /remember <内容>  /forget <内容>",
            ]
            if self.knowledge is not None:
                lines.append(self.knowledge.help_text())
            await self._send_text(message, "\n".join(lines))
            return True

        return False

    # -- events ------------------------------------------------------------
    def _on_event(self, message: InboundMessage, task_id: str, event: Event) -> None:
        self._publish_event(message.session_key, message.channel, task_id, event)

    def _publish_event(
        self, session_key: str, channel: str, task_id: str, event: Event
    ) -> None:
        payload = {
            "type": event.kind,
            "session_key": session_key,
            "channel": channel,
            "task_id": task_id,
            **event.payload,
        }
        self.hub.publish(payload)

        if self.task_center is None or not task_id:
            return
        if event.kind == "tool_call":
            asyncio.create_task(
                self.task_center.add_tool_call(task_id, str(event.payload.get("tool", "")))
            )
            asyncio.create_task(
                self.task_center.add_event(
                    task_id,
                    "tool_call",
                    f"调用 {event.payload.get('tool', '')}",
                    str(event.payload.get("arguments", "")),
                )
            )
        elif event.kind in {"error", "thinking", "turn_end"}:
            asyncio.create_task(
                self.task_center.add_event(
                    task_id, event.kind, event.kind, str(event.payload.get("message", ""))[:500]
                )
            )

    # -- helpers -----------------------------------------------------------
    def _get_agent(self, session_key: str, channel: str) -> AgentLoop:
        agent = self._agents.get(session_key)
        if agent is None:
            agent = self.agent_factory(session_key, channel)
            self._agents[session_key] = agent
        return agent

    def _sender_allowed(self, message: InboundMessage) -> bool:
        security = self.config.security
        if not security.enforce_sender_whitelist:
            return True
        allowed = security.allowed_senders.get(message.channel, [])
        return message.sender_id in allowed or "*" in allowed

    async def _send_text(
        self, message: InboundMessage, text: str, files: list[str] | None = None
    ) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=text,
                files=files or [],
                reply_to=message.message_id,
            )
        )

    # -- introspection -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "channels": [channel.name for channel in self.channels],
            "sessions": len(self._agents),
            "workers": len(self._workers),
            "inbound_queue": self.bus.inbound.qsize(),
            "outbound_queue": self.bus.outbound.qsize(),
            "hub_subscribers": self.hub.subscriber_count,
            "uptime_seconds": round(time.time() - self._started_at, 3),
        }
