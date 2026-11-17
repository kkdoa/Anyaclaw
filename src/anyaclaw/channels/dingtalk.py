"""DingTalk channel.

Outbound replies go through a group robot webhook (no app credentials needed) or
through the OpenAPI when ``client_id``/``client_secret`` are configured. Inbound
messages need DingTalk Stream mode, which is provided by the optional
``dingtalk-stream`` SDK; when it is not installed the channel runs send-only and
says so at startup.
"""

from __future__ import annotations

from typing import Any

from anyaclaw.bus import InboundMessage, MessageBus, OutboundMessage
from anyaclaw.channels._http import post_json
from anyaclaw.channels.base import Channel
from anyaclaw.config import DingTalkConfig
from anyaclaw.core.errors import ToolError
from anyaclaw.logging import get_logger

log = get_logger("channel.dingtalk")

MAX_CHARS = 4000


class DingTalkChannel(Channel):
    name = "dingtalk"

    def __init__(self, config: DingTalkConfig, bus: MessageBus) -> None:
        self.config = config
        self.bus = bus
        self._stream_task: Any = None

    async def start(self) -> None:
        if not (self.config.client_id and self.config.client_secret):
            log.info("dingtalk: no app credentials; running in webhook send-only mode")
            return
        try:
            import dingtalk_stream  # noqa: F401
        except ImportError:
            log.warning(
                "dingtalk-stream is not installed; inbound messages disabled "
                "(pip install dingtalk-stream)"
            )
            return
        self._stream_task = await self._start_stream()

    async def _start_stream(self) -> Any:
        import asyncio

        return asyncio.create_task(self._run_stream())

    async def _run_stream(self) -> None:
        import asyncio

        import dingtalk_stream

        client = dingtalk_stream.DingTalkStreamClient(
            dingtalk_stream.Credential(self.config.client_id, self.config.client_secret)
        )

        bus = self.bus

        class _Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):  # noqa: ANN001, ANN202
                data = callback.data or {}
                text = ((data.get("text") or {}).get("content") or "").strip()
                sender = data.get("senderStaffId") or data.get("senderId") or "unknown"
                if text:
                    await bus.publish_inbound(
                        InboundMessage(
                            channel="dingtalk",
                            sender_id=str(sender),
                            chat_id=f"chat:{data.get('conversationId', sender)}",
                            content=text,
                            raw=data,
                        )
                    )
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, _Handler())
        try:
            await asyncio.to_thread(client.start_forever)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None

    async def send(self, message: OutboundMessage) -> None:
        if not self.config.webhook_url:
            raise ToolError(
                "dingtalk: webhook_url is not configured; set channels.dingtalk.webhook_url"
            )
        text = message.content[:MAX_CHARS]
        if self.config.webhook_keyword and self.config.webhook_keyword not in text:
            text = f"{self.config.webhook_keyword} {text}"
        if message.files:
            listing = "\n".join(f"- {path}" for path in message.files)
            text += f"\n\n附件：\n{listing}"

        await post_json(
            self.config.webhook_url,
            {"msgtype": "text", "text": {"content": text}},
        )
