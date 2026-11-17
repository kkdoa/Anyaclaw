"""Feishu / Lark channel.

Outbound messages use the tenant access token + ``im/v1/messages`` REST API.
Inbound messages arrive on a webhook (``/api/channels/feishu/webhook``) which is
mounted by the web app; the endpoint validates Feishu's verification token and
understands plaintext ``im.message.receive_v1`` events. Encrypted payloads
(``encrypt`` field) require the AES key handling from the official SDK, so they
are rejected with an actionable error instead of being silently dropped.
"""

from __future__ import annotations

import json
from typing import Any

from anyaclaw.bus import InboundMessage, MessageBus, OutboundMessage
from anyaclaw.channels._http import TokenCache, post_json
from anyaclaw.channels.base import Channel
from anyaclaw.config import FeishuConfig
from anyaclaw.core.errors import ToolError
from anyaclaw.logging import get_logger

try:  # optional dependency
    from fastapi import APIRouter, Request
except ImportError:  # pragma: no cover
    APIRouter = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]

log = get_logger("channel.feishu")

BASE = "https://open.feishu.cn/open-apis"
MAX_CHARS = 8000


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, config: FeishuConfig, bus: MessageBus, *, default_target: str = "") -> None:
        self.config = config
        self.bus = bus
        self.default_target = default_target or config.default_target
        self._tokens = TokenCache(self._fetch_token)

    async def _fetch_token(self) -> tuple[str, float]:
        data = await post_json(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": self.config.app_id, "app_secret": self.config.app_secret},
        )
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise ToolError(f"feishu token request failed: {data}")
        return token, float(data.get("expire") or 7200)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        log.info("feishu channel ready (webhook mode at /api/channels/feishu/webhook)")

    async def send(self, message: OutboundMessage) -> None:
        target = message.chat_id or self.default_target
        if not target:
            log.warning("feishu: no target chat_id; dropping reply")
            return
        receive_id_type, receive_id = _parse_target(target)
        text = message.content[:MAX_CHARS]
        if message.files:
            listing = "\n".join(f"- {path}" for path in message.files)
            text += f"\n\n附件（请在 Web 端下载）：\n{listing}"

        token = await self._tokens.get()
        try:
            await post_json(
                f"{BASE}/im/v1/messages",
                {
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except ToolError:
            self._tokens.invalidate()
            raise

    # -- inbound -----------------------------------------------------------
    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        header = payload.get("header") or {}
        if self.config.verification_token and header.get("token") not in {
            None,
            self.config.verification_token,
        }:
            raise ToolError("feishu verification token mismatch")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if "encrypt" in payload:
            raise ToolError(
                "encrypted feishu events are not supported; disable encryption or use the "
                "official SDK bridge"
            )

        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        open_id = ((sender.get("sender_id") or {}).get("open_id")) or ""
        content = message.get("content") or "{}"
        try:
            text = json.loads(content).get("text", "")
        except (json.JSONDecodeError, AttributeError):
            text = str(content)

        if not text.strip():
            return {"ok": True, "ignored": True}

        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=open_id or message.get("chat_id", "unknown"),
                chat_id=f"chat:{message.get('chat_id', '')}",
                content=text,
                message_id=message.get("message_id"),
                raw=payload,
            )
        )
        return {"ok": True}

    def router(self):  # noqa: ANN201 - FastAPI is optional
        if APIRouter is None:
            return None

        router = APIRouter(prefix="/api/channels/feishu", tags=["feishu"])

        @router.post("/webhook")
        async def webhook(request: Request):  # noqa: ANN202
            payload = await request.json()
            return await self.handle_event(payload)

        return router


def _parse_target(target: str) -> tuple[str, str]:
    if ":" in target:
        prefix, _, value = target.partition(":")
        if prefix in {"chat", "open", "user", "email"}:
            return {"open": "open_id", "user": "open_id", "chat": "chat_id"}.get(
                prefix, "chat_id"
            ), value
    return "chat_id", target
