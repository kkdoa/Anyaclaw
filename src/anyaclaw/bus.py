"""In-process message bus and event hub.

Channels publish *inbound* messages, the gateway consumes them and produces
*outbound* messages that channels send. The :class:`EventHub` is a fan-out of
structured progress events for UIs (SSE/WebSocket) and is intentionally
lossy-on-overflow: a slow browser must never block the agent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class InboundMessage:
    """A message from a user, regardless of channel."""

    channel: str
    sender_id: str
    chat_id: str
    content: str
    message_id: str | None = None
    files: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.sender_id}"


@dataclass(slots=True)
class OutboundMessage:
    """A reply to be delivered by a channel."""

    channel: str
    chat_id: str
    content: str
    files: list[str] = field(default_factory=list)
    reply_to: str | None = None
    stream: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class MessageBus:
    """Two unbounded-but-monitored queues with graceful shutdown."""

    def __init__(self, *, maxsize: int = 1000) -> None:
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def publish_inbound(self, message: InboundMessage) -> None:
        await self._put(self.inbound, message)

    async def publish_outbound(self, message: OutboundMessage) -> None:
        await self._put(self.outbound, message)

    async def consume_inbound(self) -> InboundMessage:
        return await self.inbound.get()

    async def consume_outbound(self) -> OutboundMessage:
        return await self.outbound.get()

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @staticmethod
    async def _put(queue: asyncio.Queue, item: Any) -> None:
        # Backpressure is acceptable here: dropping a user message is worse than
        # making one channel wait a moment.
        await queue.put(item)


class EventHub:
    """Fan-out of JSON-serialisable events to UI subscribers."""

    def __init__(self, *, maxsize: int = 500) -> None:
        self.maxsize = maxsize
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        payload = {"ts": time.time(), **event}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest event for this subscriber, then retry once.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
