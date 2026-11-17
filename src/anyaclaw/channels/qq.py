"""QQ (official bot platform) channel.

Outbound messages use the OpenAPI with an app access token. Inbound messages are
delivered by QQ over a signed webhook or the gateway SDK; this adapter accepts
webhook posts at ``/api/channels/qq/webhook`` and validates the shared token when
``webhook_secret`` is configured. Full Ed25519 payload verification needs the
``cryptography`` extra and is intentionally left to a reverse proxy in front of
the bot if you expose it publicly.
"""

from __future__ import annotations

from typing import Any

from anyaclaw.bus import InboundMessage, MessageBus, OutboundMessage
from anyaclaw.channels._http import TokenCache, post_json
from anyaclaw.channels.base import Channel
from anyaclaw.config import QQConfig
from anyaclaw.core.errors import ToolError
from anyaclaw.logging import get_logger

try:  # optional dependency
    from fastapi import APIRouter, Request
except ImportError:  # pragma: no cover
    APIRouter = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]

log = get_logger("channel.qq")

API_BASE = "https://api.sgroup.qq.com"
SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"
MAX_CHARS = 1800


class QQChannel(Channel):
    name = "qq"

    def __init__(self, config: QQConfig, bus: MessageBus) -> None:
        self.config = config
        self.bus = bus
        self._tokens = TokenCache(self._fetch_token)
        self.base = SANDBOX_BASE if config.sandbox else API_BASE

    async def _fetch_token(self) -> tuple[str, float]:
        data = await post_json(
            "https://bots.qq.com/app/getAppAccessToken",
            {"appId": self.config.app_id, "clientSecret": self.config.app_secret},
        )
        token = str(data.get("access_token") or "")
        if not token:
            raise ToolError(f"qq token request failed: {data}")
        return token, float(data.get("expires_in") or 7200)

    async def start(self) -> None:
        log.info("qq channel ready (webhook mode at /api/channels/qq/webhook)")

    async def send(self, message: OutboundMessage) -> None:
        target = message.chat_id or self.config.default_target
        if not target:
            log.warning("qq: no target chat_id; dropping reply")
            return
        kind, _, target_id = target.partition(":")
        content = message.content[:MAX_CHARS]
        if message.files:
            content += "\n\n附件：" + ", ".join(message.files)

        token = await self._tokens.get()
        headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": self.config.app_id}
        payload: dict[str, Any] = {"content": content, "msg_type": 0}
        if message.reply_to:
            payload["msg_id"] = message.reply_to

        if kind in {"group", "channel"}:
            url = f"{self.base}/channels/{target_id}/messages"
        elif kind in {"user", "c2c"}:
            url = f"{self.base}/v2/users/{target_id}/messages"
        else:
            raise ToolError(f"unsupported qq target: {target!r} (use group:/channel:/user:)")

        try:
            await post_json(url, payload, headers=headers)
        except ToolError:
            self._tokens.invalidate()
            raise

    # -- inbound -----------------------------------------------------------
    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.config.webhook_secret:
            supplied = payload.pop("token", "") or payload.get("d", {}).get("token", "")
            if supplied != self.config.webhook_secret:
                raise ToolError("qq webhook token mismatch")

        event_type = payload.get("t") or payload.get("type") or ""
        data = payload.get("d") or payload
        content = str(data.get("content") or "").strip()
        if not content:
            return {"ok": True, "ignored": True}

        author = data.get("author") or {}
        sender_id = str(author.get("id") or author.get("user_openid") or "unknown")
        chat_id = str(data.get("channel_id") or data.get("group_openid") or sender_id)
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=sender_id,
                chat_id=f"channel:{chat_id}",
                content=content,
                message_id=str(data.get("id") or ""),
                raw={"event": event_type, **data},
            )
        )
        return {"ok": True}

    def router(self):  # noqa: ANN201 - FastAPI is optional
        if APIRouter is None:
            return None

        router = APIRouter(prefix="/api/channels/qq", tags=["qq"])

        @router.post("/webhook")
        async def webhook(request: Request):  # noqa: ANN202
            payload = await request.json()
            return await self.handle_event(payload)

        return router
