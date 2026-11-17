"""Provider abstraction and token estimation.

A provider is anything that turns a transcript into an :class:`LLMResponse`.
Keeping this interface tiny means the agent loop can be tested with a fake
provider (no network) and extra backends are easy to add.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from anyaclaw.core.types import LLMResponse, Message

#: ``callback(kind, text)`` where kind is ``"reasoning"`` or ``"content"``.
DeltaCallback = Callable[[str, str], None]

_CJK = re.compile(r"[\u3000-\u9fff\uf900-\ufaff\uff00-\uffef]")


class LLMProvider(ABC):
    """Base class for chat-completion providers."""

    model: str = ""

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[Message],
        tools: list[dict] | None = None,
        *,
        model: str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        """Run one completion.

        When ``on_delta`` is given the provider may stream; it must still return
        the fully assembled :class:`LLMResponse`.
        """

    async def aclose(self) -> None:
        """Release network resources (optional)."""
        return None


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate.

    CJK characters are roughly one token each; ASCII text averages ~4 chars per
    token. The estimate is intentionally slightly conservative.
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = max(0, len(text) - cjk)
    return cjk + math.ceil(rest / 4)


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    total = 0
    for message in messages:
        total += 4  # role/formatting overhead
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += estimate_tokens(str(function.get("name", "")))
            total += estimate_tokens(str(function.get("arguments", "")))
    return total
