"""Context compaction.

Long conversations do not fit a model window, and replaying everything is both
slow and expensive. Once the estimated transcript exceeds the configured budget,
older turns are summarised by the *cheap* model and replaced with a compact
summary message; the recent tail is kept verbatim so the model stays grounded.

Failure is always non-fatal: if summarisation errors out we fall back to
dropping the oldest turns.
"""

from __future__ import annotations

from collections.abc import Sequence

from anyaclaw.core.context import sanitize_history
from anyaclaw.core.provider import LLMProvider, estimate_messages_tokens
from anyaclaw.core.types import Message
from anyaclaw.logging import get_logger

log = get_logger("compaction")

_SUMMARY_PROMPT = """Summarise the conversation excerpt for future reference.

Rules:
- Keep facts, decisions, file paths, names, numbers and pending tasks.
- Drop chit-chat, repeated tool output and reasoning.
- Write in the language the conversation mainly uses.
- Maximum 400 words. Plain text, no preamble."""


class ConversationCompactor:
    """Summarises the middle of a long transcript."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str = "",
        keep_recent: int = 6,
        max_summary_tokens: int = 700,
        enabled: bool = True,
    ) -> None:
        self.provider = provider
        self.model = model or None
        self.keep_recent = keep_recent
        self.max_summary_tokens = max_summary_tokens
        self.enabled = enabled

    async def maybe_compact(
        self,
        messages: Sequence[Message],
        budget_tokens: int,
    ) -> list[Message]:
        if not self.enabled or estimate_messages_tokens(messages) <= budget_tokens:
            return list(messages)
        if len(messages) <= self.keep_recent + 2:
            return list(messages)

        system = messages[0] if messages[0].get("role") == "system" else None
        body = list(messages[1:] if system else messages)

        keep = max(self.keep_recent, 0)
        older = body[:-keep] if keep else body
        recent = sanitize_history(body[-keep:]) if keep else []
        if not older:
            return list(messages)

        summary = await self._summarize(older)
        if not summary:
            compacted = ([system] if system else []) + recent
            return compacted or list(messages)

        summary_message: Message = {
            "role": "system",
            "content": "## Summary of earlier conversation\n\n" + summary,
        }
        result = ([system] if system else []) + [summary_message] + recent
        log.info(
            "compacted transcript",
            extra={
                "extra_fields": {
                    "before_tokens": estimate_messages_tokens(messages),
                    "after_tokens": estimate_messages_tokens(result),
                }
            },
        )
        return result

    async def _summarize(self, messages: Sequence[Message]) -> str:
        transcript = _render(messages)
        if not transcript:
            return ""
        try:
            response = await self.provider.chat(
                [
                    {"role": "system", "content": _SUMMARY_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                tools=None,
                model=self.model,
            )
        except Exception as exc:  # noqa: BLE001 - compaction must never break a turn
            log.warning("summarisation failed: %s", exc)
            return ""
        text = (response.content or "").strip()
        if not text or len(text) > self.max_summary_tokens * 6:
            return text[: self.max_summary_tokens * 6]
        return text


def _render(messages: Sequence[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content.strip()[:1500]}")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            lines.append(f"{role} called {function.get('name')}({function.get('arguments')})")
    text = "\n".join(lines)
    return text[-12000:]
