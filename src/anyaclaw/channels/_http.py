"""Helpers shared by the HTTP-based messaging channels."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from anyaclaw.core.errors import ToolError
from anyaclaw.logging import get_logger, redact

log = get_logger("channel.http")


@dataclass
class TokenCache:
    """Caches an OAuth-style access token until shortly before it expires."""

    fetch: Any
    safety_margin: float = 60.0
    _token: str = ""
    _expires_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            token, ttl = await self.fetch()
            self._token = token
            self._expires_at = time.time() + max(ttl - self.safety_margin, 30.0)
            return self._token

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers or {})
    except httpx.HTTPError as exc:
        raise ToolError(f"request to {url} failed: {exc}") from exc
    return _decode(url, response)


async def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers or {}, params=params or {})
    except httpx.HTTPError as exc:
        raise ToolError(f"request to {url} failed: {exc}") from exc
    return _decode(url, response)


def _decode(url: str, response: httpx.Response) -> dict[str, Any]:
    text = response.text
    try:
        data = response.json()
    except ValueError:
        log.warning("non-JSON response from %s: %s", url, redact(text)[:300])
        raise ToolError(f"non-JSON response from {url}: HTTP {response.status_code}") from None
    if response.status_code >= 400:
        raise ToolError(f"{url} returned HTTP {response.status_code}: {redact(text)[:300]}")
    if not isinstance(data, dict):
        return {"data": data}
    return data


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
