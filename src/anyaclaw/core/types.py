"""Datatypes exchanged between the provider, the agent loop and the channels.

The runtime deliberately keeps conversation messages as plain ``dict`` objects in
the OpenAI wire format: they are persisted verbatim, replayed to the model and
serialised over HTTP without lossy conversions. Only the *results* of a model
turn are typed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
Message = dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    reasoning_content: str = ""

    def to_message_part(self) -> dict[str, Any]:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments
                or json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class Usage:
    """Token accounting for one or more model calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens

    def __add__(self, other: Usage) -> Usage:
        merged = Usage(
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.cached_tokens,
        )
        merged.add(other)
        return merged

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass(slots=True)
class LLMResponse:
    """A completed (possibly streamed) model turn."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    reasoning_content: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def failed(self) -> bool:
        return self.finish_reason == "error"

    def to_assistant_message(self, *, include_reasoning: bool = False) -> Message:
        """Convert the turn into an assistant message for the transcript."""
        message: Message = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [tc.to_message_part() for tc in self.tool_calls]
        reasoning = self.reasoning_content or (
            self.tool_calls[0].reasoning_content if self.tool_calls else ""
        )
        if include_reasoning and reasoning:
            message["reasoning_content"] = reasoning
        return message


@dataclass(slots=True)
class ToolResult:
    """Outcome of one tool execution.

    Tools communicate *structured* information instead of smuggling file paths
    through magic markers in the text: ``files`` are forwarded to the channel so
    a chat client can attach them to the reply.
    """

    content: str = ""
    files: list[str] = field(default_factory=list)
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        content: str,
        *,
        files: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ToolResult:
        return cls(content=content, files=files or [], meta=meta or {})

    @classmethod
    def error(cls, content: str, *, meta: dict[str, Any] | None = None) -> ToolResult:
        return cls(content=content, is_error=True, meta=meta or {})

    def to_text(self) -> str:
        """Render the result for the model transcript."""
        text = self.content
        if self.files:
            listing = "\n".join(f"- {path}" for path in self.files)
            text = f"{text}\n\n[generated files]\n{listing}" if text else f"[generated files]\n{listing}"
        return text.strip()


@dataclass(slots=True)
class Event:
    """A structured progress event streamed to channels and monitors."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, **self.payload}


@dataclass(slots=True)
class AgentResult:
    """Final result of one agent turn."""

    content: str = ""
    files: list[str] = field(default_factory=list)
    iterations: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error
