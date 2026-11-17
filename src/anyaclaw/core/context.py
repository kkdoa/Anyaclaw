"""System-prompt assembly and history shaping."""

from __future__ import annotations

import platform
import sys
from collections.abc import Sequence
from datetime import datetime

from anyaclaw.config import Config
from anyaclaw.core.memory import Memory, MemoryStore
from anyaclaw.core.provider import estimate_messages_tokens
from anyaclaw.core.skills import SkillLoader
from anyaclaw.core.types import Message

DEFAULT_IDENTITY = """You are Anyaclaw, a local-first AI assistant with real tools.

You help with engineering work, research, documents and everyday operations.
Be direct and accurate: prefer doing the work with tools over describing it.
"""


class ContextBuilder:
    """Builds the system prompt and the message list sent to the model."""

    def __init__(
        self,
        config: Config,
        *,
        memory: MemoryStore | None = None,
        skills: SkillLoader | None = None,
        identity_override: str = "",
    ) -> None:
        self.config = config
        self.memory = memory
        self.skills = skills or SkillLoader(config.workspace / config.agent.skills_dir)
        self.identity_override = identity_override

    # -- system prompt -----------------------------------------------------
    def identity(self) -> str:
        if self.identity_override:
            return self.identity_override
        path = self.config.workspace / self.config.agent.identity_file
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except OSError:
                pass
        return DEFAULT_IDENTITY

    def system_prompt(
        self,
        *,
        channel: str = "",
        session_key: str = "",
        memories: Sequence[Memory] | None = None,
    ) -> str:
        config = self.config
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z (%A)")
        parts: list[str] = [self.identity().strip(), "", "## Environment", ""]
        parts += [
            f"- Current time: {now}",
            f"- Channel: {channel or 'local'}",
            f"- Workspace: {config.workspace}",
            f"- Generated files: {config.files_dir}",
            f"- Platform: {platform.system()} {platform.release()} / Python {sys.version.split()[0]}",
        ]
        if session_key:
            parts.append(f"- Session: {session_key}")

        parts += [
            "",
            "## Operating rules",
            "",
            "1. Reply in the user's language. Be concise; use Markdown when helpful.",
            "2. Never invent file contents, command output or search results — call the tool.",
            "3. All filesystem paths are relative to the workspace unless absolute paths are allowed.",
            "4. Destructive or outbound actions (delete, shell, mail, webhooks) may require user approval; "
            "if approval is denied, stop and explain instead of retrying.",
            "5. Files you create with document tools are attached to the reply automatically — "
            "never paste base64 or whole-file dumps into the chat.",
            "6. When you use web_search / web_fetch, cite the source URL.",
            "7. If a tool fails, read the error and try a different approach; do not repeat the same call.",
            "8. For multi-step tasks: state a short plan, then execute it step by step.",
            "9. Store durable user facts with the memory tools; do not store small talk.",
        ]

        if memories:
            parts += ["", "## Relevant memory", ""]
            parts += [f"- {memory.content}" for memory in memories]

        summary = self.skills.summary() if self.skills else ""
        if summary:
            parts += [
                "",
                "## Available skills",
                "",
                "Read the skill file with read_file when the task matches it.",
                "",
                summary,
            ]

        return "\n".join(parts)

    # -- messages ----------------------------------------------------------
    def build_messages(
        self,
        history: Sequence[Message],
        user_message: str,
        *,
        channel: str = "",
        session_key: str = "",
        memories: Sequence[Memory] | None = None,
        budget_tokens: int | None = None,
    ) -> list[Message]:
        trimmed = self.trim_history(
            history,
            budget_tokens=budget_tokens or self.config.agent.context_token_budget,
        )
        messages: list[Message] = [
            {
                "role": "system",
                "content": self.system_prompt(
                    channel=channel, session_key=session_key, memories=memories
                ),
            }
        ]
        messages.extend(trimmed)
        if user_message:
            messages.append({"role": "user", "content": user_message})
        return messages

    def trim_history(
        self,
        history: Sequence[Message],
        *,
        budget_tokens: int | None = None,
    ) -> list[Message]:
        budget = budget_tokens or self.config.agent.context_token_budget
        max_messages = self.config.agent.max_history_messages

        kept: list[Message] = []
        used = 0
        for message in reversed(list(history)):
            if len(kept) >= max_messages:
                break
            cost = estimate_messages_tokens([message]) + 4
            if kept and used + cost > budget:
                break
            kept.append(message)
            used += cost
        kept.reverse()
        return sanitize_history(kept)


def sanitize_history(history: Sequence[Message]) -> list[Message]:
    """Drop orphan tool messages so the transcript stays API-valid.

    After trimming, the first kept message can be a ``tool`` result whose
    ``assistant`` tool-call message was cut. Providers reject that, so leading
    tool messages (and any tool message without a preceding matching call) are
    removed.
    """
    valid: list[Message] = []
    pending_calls: set[str] = set()
    for message in history:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = str(call.get("id", ""))
                if call_id:
                    pending_calls.add(call_id)
            valid.append(message)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            if call_id and call_id in pending_calls:
                pending_calls.discard(call_id)
                valid.append(message)
            continue
        pending_calls.clear()
        valid.append(message)
    return valid
