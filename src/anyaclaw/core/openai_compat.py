"""OpenAI-compatible chat provider over raw ``httpx``.

Why not the official SDK? The wire format is simple, and talking HTTP directly
buys us: explicit retry/back-off policy, per-call timeouts, streaming usage
accounting, tolerant parsing of ``reasoning_content`` (DeepSeek/Kimi/GLM style)
and no dependency besides httpx.

Reliability notes (these are the "fixed bugs" compared to the reference project):

* retries happen only **before** the first streamed delta, so a retry can never
  duplicate text already shown to the user;
* ``429``/``5xx``/timeouts are retried with jittered exponential back-off, and
  ``Retry-After`` is honoured;
* HTTP errors raise :class:`ProviderError` instead of being swallowed into a
  fake assistant message, so the loop can distinguish "model said no" from
  "network broke";
* ``usage`` is parsed from the final SSE chunk (``stream_options.include_usage``).
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from anyaclaw.config import ProviderConfig
from anyaclaw.core.errors import ProviderError
from anyaclaw.core.provider import DeltaCallback, LLMProvider
from anyaclaw.core.types import LLMResponse, Message, ToolCall, Usage
from anyaclaw.logging import get_logger, redact

log = get_logger("provider")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class OpenAICompatProvider(LLMProvider):
    """Chat provider for any OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.model = config.model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.timeout,
                connect=config.connect_timeout,
                read=config.timeout,
                write=config.timeout,
                pool=config.connect_timeout,
            ),
            headers={
                "Authorization": f"Bearer {self._require_key(config)}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **config.extra_headers,
            },
            transport=transport,
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- public API --------------------------------------------------------
    async def chat(
        self,
        messages: Sequence[Message],
        tools: list[dict] | None = None,
        *,
        model: str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        active_model = model or self.model
        payload = self._payload(messages, tools, active_model, stream=on_delta is not None)
        if on_delta is not None:
            return await self._chat_stream(payload, active_model, on_delta)
        return await self._chat_once(payload, active_model)

    # -- request building --------------------------------------------------
    def _payload(
        self,
        messages: Sequence[Message],
        tools: list[dict] | None,
        model: str,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        cfg = self.config
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_clean_message(message, cfg.send_reasoning) for message in messages],
        }
        if cfg.temperature is not None:
            payload["temperature"] = cfg.temperature
        if cfg.top_p is not None:
            payload["top_p"] = cfg.top_p
        if cfg.max_tokens:
            payload["max_tokens"] = cfg.max_tokens

        if stream:
            payload["stream"] = True
            if cfg.stream_usage:
                payload["stream_options"] = {"include_usage": True}

        extra_body = dict(cfg.extra_body or {})
        if cfg.thinking and _supports_thinking(model):
            if _is_siliconflow(cfg.base_url):
                extra_body.setdefault("enable_thinking", True)
                if cfg.reasoning_budget:
                    extra_body.setdefault("thinking_budget", cfg.reasoning_budget)
            else:
                extra_body.setdefault("thinking", {"type": "enabled"})
            if cfg.reasoning_effort:
                payload.setdefault("reasoning_effort", cfg.reasoning_effort)
        payload.update(extra_body)

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    # -- non-streaming -----------------------------------------------------
    async def _chat_once(self, payload: dict[str, Any], model: str) -> LLMResponse:
        response = await self._post(payload, stream=False)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"provider returned non-JSON response: {_snippet(response.text)}") from exc
        finally:
            await response.aclose()

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"provider returned no choices: {_snippet(json.dumps(data))}")

        choice = choices[0]
        message = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function") or {}
            raw_args = function.get("arguments") or ""
            tool_calls.append(
                ToolCall(
                    id=item.get("id") or f"call_{index}",
                    name=function.get("name") or "",
                    arguments=_parse_arguments(raw_args),
                    raw_arguments=raw_args if isinstance(raw_args, str) else "",
                )
            )

        return LLMResponse(
            content=_as_text(message.get("content")),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "stop",
            reasoning_content=_as_text(
                message.get("reasoning_content") or message.get("reasoning")
            ),
            usage=_parse_usage(data.get("usage")),
            model=data.get("model") or model,
        )

    # -- streaming ---------------------------------------------------------
    async def _chat_stream(
        self,
        payload: dict[str, Any],
        model: str,
        on_delta: DeltaCallback,
    ) -> LLMResponse:
        response = await self._post(payload, stream=True)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage = Usage()
        model_name = model
        emitted = False

        try:
            async for event in _iter_sse(response):
                if event == "[DONE]":
                    break
                try:
                    chunk = json.loads(event)
                except json.JSONDecodeError:
                    log.debug("skipping malformed SSE chunk", extra={"extra_fields": {"chunk": event[:200]}})
                    continue

                usage = _merge_usage(usage, chunk.get("usage"))
                model_name = chunk.get("model") or model_name

                for choice in chunk.get("choices") or []:
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or choice.get("message") or {}

                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        reasoning_parts.append(reasoning)
                        emitted = True
                        on_delta("reasoning", reasoning)

                    content = delta.get("content")
                    if content:
                        text = _as_text(content)
                        if text:
                            content_parts.append(text)
                            emitted = True
                            on_delta("content", text)

                    for item in delta.get("tool_calls") or []:
                        index = int(item.get("index") or 0)
                        entry = tool_calls.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if item.get("id"):
                            entry["id"] = item["id"]
                        function = item.get("function") or {}
                        if function.get("name"):
                            entry["name"] += function["name"]
                        args = function.get("arguments")
                        if args:
                            entry["arguments"] += _as_text(args)
        except _RETRYABLE_EXC as exc:  # streaming broke midway: do not retry
            raise ProviderError(
                f"stream interrupted after {len(''.join(content_parts))} chars: {exc}",
                retryable=not emitted,
            ) from exc
        finally:
            await response.aclose()

        parsed_calls: list[ToolCall] = []
        for index in sorted(tool_calls):
            entry = tool_calls[index]
            parsed_calls.append(
                ToolCall(
                    id=entry["id"] or f"call_{index}",
                    name=entry["name"],
                    arguments=_parse_arguments(entry["arguments"]),
                    raw_arguments=entry["arguments"],
                )
            )

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=parsed_calls,
            finish_reason=finish_reason,
            reasoning_content="".join(reasoning_parts),
            usage=usage,
            model=model_name,
        )

    # -- transport ---------------------------------------------------------
    async def _post(self, payload: dict[str, Any], *, stream: bool) -> httpx.Response:
        url = self._endpoint()
        attempts = self.config.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                request = self._client.build_request("POST", url, json=payload)
                response = await self._client.send(request, stream=stream)
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    await response.aclose()
                    error = ProviderError(
                        f"HTTP {response.status_code}: {_snippet(body)}",
                        retryable=response.status_code in RETRYABLE_STATUS,
                        status_code=response.status_code,
                    )
                    if not error.retryable or attempt == attempts - 1:
                        raise error
                    last_error = error
                    await self._backoff(attempt, response.headers.get("retry-after"))
                    continue
                return response
            except _RETRYABLE_EXC as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise ProviderError(f"connection failed: {exc}", retryable=True) from exc
                await self._backoff(attempt, None)

        raise ProviderError(f"request failed after {attempts} attempts: {last_error}", retryable=True)

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = self.config.retry_backoff * (2**attempt) + random.uniform(0, 0.4)
        delay = min(delay, 30.0)
        log.warning(
            "retrying provider request in %.2fs (attempt %d)", delay, attempt + 1
        )
        await asyncio.sleep(delay)

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return f"{base}/chat/completions"

    @staticmethod
    def _require_key(config: ProviderConfig) -> str:
        if not config.api_key:
            raise ProviderError(
                "provider.api_key is empty; set it in anyaclaw.toml or the environment"
            )
        return config.api_key


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean_message(message: Message, include_reasoning: bool) -> dict[str, Any]:
    cleaned: dict[str, Any] = {
        key: value
        for key, value in message.items()
        if key in {"role", "content", "tool_calls", "tool_call_id", "name"}
    }
    if include_reasoning and message.get("reasoning_content"):
        cleaned["reasoning_content"] = message["reasoning_content"]
    if cleaned.get("content") is None:
        cleaned["content"] = ""
    return cleaned


def _supports_thinking(model: str) -> bool:
    name = (model or "").lower()
    return any(
        marker in name
        for marker in ("deepseek", "qwen3", "glm-4", "kimi-k2", "reasoner", "/r1", "thinking")
    )


def _is_siliconflow(base_url: str) -> bool:
    return "siliconflow" in (base_url or "").lower()


async def _iter_sse(response: httpx.Response) -> AsyncIterator[str]:
    async for line in response.aiter_lines():
        if not line:
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[5:].strip()
        elif line.startswith("{"):
            # Some gateways omit the "data:" prefix.
            yield line.strip()


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    return {"_value": raw}


def _parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    details = raw.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or raw.get("cached_tokens") or 0),
    )


def _merge_usage(current: Usage, raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return current
    incoming = _parse_usage(raw)
    if not (incoming.prompt_tokens or incoming.completion_tokens or incoming.total_tokens):
        return current
    return Usage(
        prompt_tokens=max(current.prompt_tokens, incoming.prompt_tokens),
        completion_tokens=max(current.completion_tokens, incoming.completion_tokens),
        total_tokens=max(current.total_tokens, incoming.total_tokens),
        cached_tokens=max(current.cached_tokens, incoming.cached_tokens),
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def _snippet(text: str, limit: int = 400) -> str:
    return redact(text)[:limit]
