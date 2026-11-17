"""The agent loop: model ⇄ tools until the model answers without tool calls.

Compared to the reference implementation this loop:

* retries/timeouts live in the provider, so a transient 429 no longer surfaces as
  a fake assistant message;
* tool failures become structured ``ToolResult`` errors the model can recover
  from, and every tool call has a hard timeout;
* repeated identical calls are *warned about first* (the model gets a nudge) and
  only aborted after a higher threshold, instead of killing the turn at the first
  suspicion;
* read-only tool calls in the same step run concurrently;
* token usage, latency and tool counters are recorded as metrics;
* cancellation (``/cancel``) propagates cleanly instead of leaving a zombie task.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from anyaclaw.config import AgentConfig
from anyaclaw.core.compaction import ConversationCompactor
from anyaclaw.core.context import ContextBuilder
from anyaclaw.core.errors import ProviderError
from anyaclaw.core.memory import MemoryStore
from anyaclaw.core.policy import ConfirmationRequest, Confirmer, ToolPolicy
from anyaclaw.core.provider import LLMProvider
from anyaclaw.core.registry import ToolRegistry
from anyaclaw.core.session import SessionStore
from anyaclaw.core.types import (
    AgentResult,
    Event,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    Usage,
)
from anyaclaw.logging import get_logger, set_trace
from anyaclaw.metrics import Metrics

log = get_logger("agent")

EventSink = Callable[[Event], Any]


class LoopAborted(Exception):
    """Raised internally when the loop fuse trips."""


class AgentLoop:
    """One agent instance bound to one session key."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        sessions: SessionStore,
        session_key: str,
        settings: AgentConfig,
        model: str | None = None,
        memory: MemoryStore | None = None,
        metrics: Metrics | None = None,
        policy: ToolPolicy | None = None,
        confirmer: Confirmer | None = None,
        compactor: ConversationCompactor | None = None,
        event_sink: EventSink | None = None,
        channel: str = "",
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context
        self.sessions = sessions
        self.session_key = session_key
        self.settings = settings
        self.model = model or getattr(provider, "model", "")
        self.memory = memory
        self.metrics = metrics
        self.policy = policy
        self.confirmer = confirmer
        self.compactor = compactor
        self.event_sink = event_sink
        self.channel = channel

        self._recent_calls: deque[str] = deque(maxlen=max(settings.loop_window, 10))
        self._lock = asyncio.Lock()
        self._trace_id = ""

    # -- public ------------------------------------------------------------
    async def run(self, user_message: str) -> AgentResult:
        """Process one user message, executing tools until a final answer."""
        async with self._lock:
            return await self._run(user_message)

    async def clear(self) -> None:
        await self.sessions.clear(self.session_key)
        self._recent_calls.clear()

    async def history(self, limit: int | None = None) -> list[Message]:
        return await self.sessions.history(self.session_key, limit)

    # -- loop --------------------------------------------------------------
    async def _run(self, user_message: str) -> AgentResult:
        self._trace_id = uuid.uuid4().hex
        set_trace(self._trace_id, self.session_key)
        started = time.perf_counter()
        usage = Usage()
        files: list[str] = []
        tool_call_count = 0
        iterations = 0
        self._recent_calls.clear()

        self._emit("turn_start", message=user_message)
        log.info(
            "turn start",
            extra={"extra_fields": {"message_chars": len(user_message), "channel": self.channel}},
        )

        try:
            history = await self.sessions.history(
                self.session_key, limit=self.settings.max_history_messages
            )
            memories = await self._retrieve_memories(user_message)
            messages = self.context.build_messages(
                history,
                user_message,
                channel=self.channel,
                session_key=self.session_key,
                memories=memories,
            )
            await self.sessions.append(
                self.session_key, {"role": "user", "content": user_message}
            )

            for iteration in range(1, self.settings.max_iterations + 1):
                iterations = iteration
                if self.compactor is not None:
                    messages = await self.compactor.maybe_compact(
                        messages, self.settings.context_token_budget
                    )

                self._emit("thinking", iteration=iteration)
                response = await self._call_provider(messages)
                usage.add(response.usage)

                if response.failed:
                    return await self._fail(
                        response.content or "provider error",
                        usage=usage,
                        iterations=iterations,
                        tool_calls=tool_call_count,
                        files=files,
                    )

                if response.has_tool_calls:
                    await self._persist_assistant(messages, response)
                    if tool_call_count + len(response.tool_calls) > self.settings.max_tool_calls:
                        return await self._fail(
                            f"tool call budget exceeded ({self.settings.max_tool_calls})",
                            usage=usage,
                            iterations=iterations,
                            tool_calls=tool_call_count,
                            files=files,
                        )
                    results = await self._execute_batch(response.tool_calls)
                    for call, result in results:
                        tool_call_count += 1
                        files.extend(result.files)
                        payload = self._truncate(result.to_text())
                        message: Message = {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": payload or "(no output)",
                        }
                        messages.append(message)
                        await self.sessions.append(self.session_key, message)
                    continue

                content = (response.content or "").strip()
                if not content:
                    return await self._fail(
                        "model returned an empty response",
                        usage=usage,
                        iterations=iterations,
                        tool_calls=tool_call_count,
                        files=files,
                    )

                final: Message = {"role": "assistant", "content": content}
                if self.settings.send_reasoning and response.reasoning_content:
                    final["reasoning_content"] = response.reasoning_content
                messages.append(final)
                await self.sessions.append(self.session_key, final)

                elapsed = time.perf_counter() - started
                if self.metrics is not None:
                    self.metrics.observe("turn_latency", elapsed, channel=self.channel)
                    self.metrics.incr("turns", channel=self.channel)
                self._emit(
                    "turn_end",
                    content=content,
                    iterations=iterations,
                    tool_calls=tool_call_count,
                    files=files,
                    usage=usage.to_dict(),
                    duration_ms=round(elapsed * 1000, 1),
                )
                log.info(
                    "turn end",
                    extra={
                        "extra_fields": {
                            "iterations": iterations,
                            "tool_calls": tool_call_count,
                            "duration_ms": round(elapsed * 1000, 1),
                            "usage": usage.to_dict(),
                            "files": files,
                        }
                    },
                )
                return AgentResult(
                    content=content,
                    files=files,
                    iterations=iterations,
                    tool_calls=tool_call_count,
                    usage=usage,
                )

            return await self._fail(
                f"max iterations reached ({self.settings.max_iterations})",
                usage=usage,
                iterations=iterations,
                tool_calls=tool_call_count,
                files=files,
            )

        except LoopAborted as exc:
            return await self._fail(str(exc), usage=usage, iterations=iterations, tool_calls=tool_call_count, files=files)
        except asyncio.CancelledError:
            self._emit("cancelled", reason="task cancelled")
            log.info("turn cancelled")
            raise
        except ProviderError as exc:
            return await self._fail(str(exc), usage=usage, iterations=iterations, tool_calls=tool_call_count, files=files)
        except Exception as exc:  # noqa: BLE001 - the loop must not crash the gateway
            log.exception("agent turn crashed")
            return await self._fail(f"internal error: {exc}", usage=usage, iterations=iterations, tool_calls=tool_call_count, files=files)

    # -- model call --------------------------------------------------------
    async def _call_provider(self, messages: Sequence[Message]) -> LLMResponse:
        def on_delta(kind: str, text: str) -> None:
            self._emit(f"{kind}_delta", content=text)

        stream = self.settings.stream
        try:
            response = await self.provider.chat(
                messages,
                self.tools.definitions(),
                model=self.model or None,
                on_delta=on_delta if stream else None,
            )
        except TypeError:
            # Provider without streaming support (e.g. simple test fakes).
            response = await self.provider.chat(
                messages, self.tools.definitions(), model=self.model or None
            )

        if self.metrics is not None and response.usage:
            self.metrics.record_usage(
                response.model or self.model or "unknown",
                response.usage,
                session_key=self.session_key,
            )
        self._emit(
            "model_response",
            content_chars=len(response.content or ""),
            reasoning_chars=len(response.reasoning_content or ""),
            tool_calls=len(response.tool_calls),
            finish_reason=response.finish_reason,
            usage=response.usage.to_dict(),
            model=response.model or self.model,
        )
        if response.reasoning_content:
            self._emit("reasoning", content=response.reasoning_content)
        return response

    async def _persist_assistant(self, messages: list[Message], response: LLMResponse) -> None:
        message = response.to_assistant_message(include_reasoning=self.settings.send_reasoning)
        messages.append(message)
        await self.sessions.append(self.session_key, message)

    # -- tools -------------------------------------------------------------
    async def _execute_batch(self, calls: list[ToolCall]) -> list[tuple[ToolCall, ToolResult]]:
        prepared: list[tuple[ToolCall, Any, ToolResult | None]] = []

        for call in calls:
            tool = self.tools.get(call.name)
            signature = _signature(call)
            repeats = self._recent_calls.count(signature)

            if repeats >= self.settings.loop_abort_after:
                raise LoopAborted(
                    f"tool '{call.name}' was called {repeats + 1} times with identical "
                    "arguments; aborting to avoid an infinite loop"
                )
            self._recent_calls.append(signature)

            self._emit(
                "tool_call",
                tool=call.name,
                arguments=call.arguments,
                call_id=call.id,
                readonly=bool(getattr(tool, "readonly", False)),
            )

            if tool is None:
                prepared.append((call, None, ToolResult.error(f"unknown tool: {call.name}")))
                continue

            if repeats >= self.settings.loop_warn_after:
                prepared.append(
                    (
                        call,
                        tool,
                        ToolResult.error(
                            f"this exact {call.name} call was already made {repeats} times "
                            "with the same arguments. Do not repeat it — change the arguments "
                            "or try a different approach."
                        ),
                    )
                )
                continue

            prepared.append((call, tool, None))

        if self.policy is not None:
            prepared = await self._apply_policy(prepared)

        parallel = (
            self.settings.parallel_tool_calls
            and len(prepared) > 1
            and all(result is None and getattr(tool, "readonly", False) for _c, tool, result in prepared)
        )

        if parallel:
            results = await asyncio.gather(
                *(self._execute_one(call) for call, _tool, _r in prepared)
            )
        else:
            results = []
            for call, _tool, pre_result in prepared:
                if pre_result is not None:
                    self._emit(
                        "tool_result",
                        tool=call.name,
                        call_id=call.id,
                        content=self._truncate(pre_result.content)[:600],
                        is_error=True,
                        files=[],
                    )
                    results.append(pre_result)
                else:
                    results.append(await self._execute_one(call))

        return [(call, result) for (call, _tool, _r), result in zip(prepared, results, strict=True)]

    async def _apply_policy(
        self, prepared: list[tuple[ToolCall, Any, ToolResult | None]]
    ) -> list[tuple[ToolCall, Any, ToolResult | None]]:
        assert self.policy is not None
        checked: list[tuple[ToolCall, Any, ToolResult | None]] = []
        for call, tool, pre_result in prepared:
            if pre_result is not None or tool is None:
                checked.append((call, tool, pre_result))
                continue

            summary = ""
            if hasattr(tool, "summary"):
                try:
                    summary = tool.summary(call.arguments)
                except Exception:  # noqa: BLE001 - summaries are cosmetic
                    summary = call.name
            action, reason = self.policy.evaluate(
                call.name,
                call.arguments,
                readonly=bool(getattr(tool, "readonly", False)),
                summary=summary,
            )

            if action == "allow":
                checked.append((call, tool, None))
                continue
            if action == "deny":
                checked.append(
                    (call, tool, ToolResult.error(f"tool '{call.name}' is blocked by policy"))
                )
                continue

            approved = False
            if self.confirmer is not None:
                request = ConfirmationRequest(
                    tool=call.name,
                    arguments=_json_safe(call.arguments),
                    reason=reason,
                    session_key=self.session_key,
                    channel=self.channel,
                )
                self._emit("approval_request", **request.to_dict())
                try:
                    approved = bool(await self.confirmer(request))
                except Exception as exc:  # noqa: BLE001 - a broken UI must not hang the turn
                    log.warning("confirmer failed: %s", exc)
                    approved = False
                self._emit("approval_result", id=request.id, approved=approved)

            if approved:
                checked.append((call, tool, None))
            else:
                checked.append(
                    (call, tool, ToolResult.error(f"user did not approve '{call.name}'"))
                )
        return checked

    async def _execute_one(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        result = await self.tools.execute(
            call.name, call.arguments, timeout=self.settings.tool_timeout
        )
        duration = (time.perf_counter() - started) * 1000
        self._emit(
            "tool_result",
            tool=call.name,
            call_id=call.id,
            content=self._truncate(result.content)[:600],
            is_error=result.is_error,
            files=result.files,
            duration_ms=round(duration, 1),
        )
        return result

    # -- helpers -----------------------------------------------------------
    def _truncate(self, text: str) -> str:
        limit = self.settings.max_tool_output_chars
        if limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + f"\n\n…[truncated {len(text) - limit} chars]"

    async def _retrieve_memories(self, query: str) -> list:
        if self.memory is None or not query.strip():
            return []
        try:
            return await self.memory.search(query, limit=5)
        except Exception as exc:  # noqa: BLE001 - memory is best-effort
            log.warning("memory retrieval failed: %s", exc)
            return []

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.event_sink is None:
            return
        event = Event(
            kind,
            {
                "session_key": self.session_key,
                "trace_id": self._trace_id,
                **payload,
            },
        )
        try:
            outcome = self.event_sink(event)
            if inspect.isawaitable(outcome):
                # A sync sink is required for streaming; schedule async ones.
                asyncio.create_task(_await_safely(outcome))
        except Exception:  # noqa: BLE001 - observers must never break the loop
            log.exception("event sink failed")

    async def _fail(
        self,
        error: str,
        *,
        usage: Usage,
        iterations: int,
        tool_calls: int,
        files: list[str],
    ) -> AgentResult:
        self._emit("error", message=error)
        log.warning("turn failed: %s", error)
        if self.metrics is not None:
            self.metrics.incr("turn_errors", channel=self.channel)
        return AgentResult(
            content="",
            files=files,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason="error",
            error=error,
        )


def _signature(call: ToolCall) -> str:
    try:
        args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args = repr(call.arguments)
    return f"{call.name}:{args}"


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"value": repr(value)}


async def _await_safely(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001
        log.exception("async event sink failed")
