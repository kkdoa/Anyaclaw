"""Shared pytest fixtures: a throwaway config, a fake provider and helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from anyaclaw.config import AgentConfig, Config
from anyaclaw.core.provider import LLMProvider
from anyaclaw.core.types import LLMResponse, Message, ToolCall, Usage


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    """Build a Config isolated in ``tmp_path`` with sensible test defaults."""
    config = Config()
    config.provider.api_key = "test-key"
    config.provider.model = "fake-model"
    config.provider.base_url = "http://127.0.0.1:9/v1"
    config.provider.max_retries = 0
    config.provider.timeout = 2.0
    config.storage.data_dir = str(tmp_path / "data")
    config.security.workspace = str(tmp_path / "ws")
    config.security.auto_approve = True
    config.web.enabled = False
    config.agent.stream = True
    config.agent.tool_timeout = 5.0
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            setattr(getattr(config, section), field, value)
        else:
            setattr(config, key, value)
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return config


class FakeProvider(LLMProvider):
    """Scripted provider: pops one response per ``chat`` call."""

    model = "fake-model"

    def __init__(
        self,
        responses: Sequence[LLMResponse | Callable[[Sequence[Message]], LLMResponse]] = (),
        *,
        chunk_size: int = 4,
        delay: float = 0.0,
    ) -> None:
        self.responses = list(responses)
        self.chunk_size = chunk_size
        self.delay = delay
        self.calls: list[list[Message]] = []
        self.default = LLMResponse(content="done", finish_reason="stop")

    async def chat(
        self,
        messages: Sequence[Message],
        tools: list[dict] | None = None,
        *,
        model: str | None = None,
        on_delta: Callable[[str, str], None] | None = None,
    ) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.responses.pop(0) if self.responses else self.default
        response = item(messages) if callable(item) else item
        if on_delta and response.content:
            for index in range(0, len(response.content), self.chunk_size):
                on_delta("content", response.content[index : index + self.chunk_size])
                await asyncio.sleep(0)
        if on_delta and response.reasoning_content:
            on_delta("reasoning", response.reasoning_content)
        return response

    async def aclose(self) -> None:  # pragma: no cover - nothing to release
        return None


def tool_call_response(name: str, arguments: dict | None = None, call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments or {})],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def text_response(text: str, *, reasoning: str = "") -> LLMResponse:
    return LLMResponse(
        content=text,
        finish_reason="stop",
        reasoning_content=reasoning,
        usage=Usage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def agent_config(config: Config) -> AgentConfig:
    return config.agent
