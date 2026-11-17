"""Agent loop: tool calls, loop fuse, policy, cancellation, metrics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anyaclaw.config import Config
from anyaclaw.core.agent import AgentLoop
from anyaclaw.core.context import ContextBuilder
from anyaclaw.core.policy import ConfirmationRequest, ToolPolicy
from anyaclaw.core.registry import ToolRegistry
from anyaclaw.core.session import SqliteSessionStore
from anyaclaw.metrics import Metrics
from anyaclaw.tools.base import FunctionTool
from tests.conftest import FakeProvider, make_config, text_response, tool_call_response


def build_agent(
    tmp_path: Path,
    responses,
    *,
    config: Config | None = None,
    tools: ToolRegistry | None = None,
    policy: ToolPolicy | None = None,
    confirmer=None,
    metrics: Metrics | None = None,
) -> tuple[AgentLoop, FakeProvider]:
    config = config or make_config(tmp_path)
    provider = FakeProvider(responses)
    registry = tools or ToolRegistry(
        [
            FunctionTool(
                "echo",
                "Echo text",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                lambda text: f"echo:{text}",
                readonly=True,
            )
        ]
    )
    agent = AgentLoop(
        provider=provider,
        tools=registry,
        context=ContextBuilder(config),
        sessions=SqliteSessionStore(tmp_path / "sessions.sqlite"),
        session_key="cli:test",
        settings=config.agent,
        model="fake-model",
        metrics=metrics or Metrics(),
        policy=policy,
        confirmer=confirmer,
    )
    return agent, provider


async def test_simple_answer_persists_conversation(tmp_path: Path) -> None:
    agent, provider = build_agent(tmp_path, [text_response("hello there")])
    result = await agent.run("hi")

    assert result.content == "hello there"
    assert result.error == ""
    assert result.iterations == 1
    assert provider.calls[0][0]["role"] == "system"
    assert provider.calls[0][-1] == {"role": "user", "content": "hi"}

    history = await agent.sessions.history("cli:test")
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"] == "hello there"
    await agent.sessions.close()


async def test_tool_call_then_answer(tmp_path: Path) -> None:
    agent, provider = build_agent(
        tmp_path,
        [tool_call_response("echo", {"text": "ping"}), text_response("final answer")],
    )
    result = await agent.run("run the tool")

    assert result.content == "final answer"
    assert result.tool_calls == 1
    assert result.iterations == 2

    history = await agent.sessions.history("cli:test")
    roles = [message["role"] for message in history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert history[1]["tool_calls"][0]["function"]["name"] == "echo"
    assert history[2]["content"] == "echo:ping"
    await agent.sessions.close()


async def test_usage_and_metrics_recorded(tmp_path: Path) -> None:
    metrics = Metrics()
    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("echo", {"text": "a"}), text_response("done")],
        metrics=metrics,
    )
    result = await agent.run("go")
    snapshot = metrics.snapshot()
    assert snapshot["models"]["fake-model"]["calls"] == 2
    assert result.usage.total_tokens == 26
    assert any(key.startswith("turns") for key in snapshot["counters"])
    await agent.sessions.close()


def _mutating_tools() -> ToolRegistry:
    async def record(text: str) -> str:
        return f"wrote:{text}"

    return ToolRegistry(
        [
            FunctionTool(
                "record",
                "Record text (mutating)",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                record,
                readonly=False,
            )
        ]
    )


async def test_policy_deny_blocks_tool(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.security.auto_approve = False
    config.security.tool_policies = {"record": "deny"}
    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("record", {"text": "x"}), text_response("blocked")],
        config=config,
        tools=_mutating_tools(),
        policy=ToolPolicy(config.security),
    )
    await agent.run("try")
    history = await agent.sessions.history("cli:test")
    tool_message = next(message for message in history if message["role"] == "tool")
    assert "blocked by policy" in tool_message["content"]
    await agent.sessions.close()


async def test_confirmation_denied(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.security.auto_approve = False
    config.security.tool_policies = {"record": "confirm"}
    requests: list[ConfirmationRequest] = []

    async def deny(request: ConfirmationRequest) -> bool:
        requests.append(request)
        return False

    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("record", {"text": "x"}), text_response("no")],
        config=config,
        tools=_mutating_tools(),
        policy=ToolPolicy(config.security),
        confirmer=deny,
    )
    await agent.run("try")
    assert requests and requests[0].tool == "record"
    history = await agent.sessions.history("cli:test")
    tool_message = next(message for message in history if message["role"] == "tool")
    assert "did not approve" in tool_message["content"]
    await agent.sessions.close()


async def test_confirmation_approved(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.security.auto_approve = False
    config.security.tool_policies = {"record": "confirm"}

    async def approve(_request: ConfirmationRequest) -> bool:
        return True

    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("record", {"text": "yes"}), text_response("ok")],
        config=config,
        tools=_mutating_tools(),
        policy=ToolPolicy(config.security),
        confirmer=approve,
    )
    await agent.run("try")
    history = await agent.sessions.history("cli:test")
    tool_message = next(message for message in history if message["role"] == "tool")
    assert tool_message["content"] == "wrote:yes"
    await agent.sessions.close()


async def test_loop_fuse_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.agent.loop_warn_after = 1
    config.agent.loop_abort_after = 2
    config.agent.max_iterations = 8
    responses = [tool_call_response("echo", {"text": "same"}, f"call_{index}") for index in range(8)]
    agent, _ = build_agent(tmp_path, responses, config=config)
    result = await agent.run("loop")
    assert result.error
    assert "identical" in result.error
    await agent.sessions.close()


async def test_max_iterations(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.agent.max_iterations = 2
    config.agent.loop_warn_after = 99
    config.agent.loop_abort_after = 99
    responses = [
        tool_call_response("echo", {"text": str(index)}, f"call_{index}") for index in range(5)
    ]
    agent, _ = build_agent(tmp_path, responses, config=config)
    result = await agent.run("keep going")
    assert result.error and "max iterations" in result.error
    await agent.sessions.close()


async def test_unknown_tool_becomes_error_result(tmp_path: Path) -> None:
    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("nope", {}), text_response("recovered")],
    )
    result = await agent.run("call missing tool")
    assert result.content == "recovered"
    history = await agent.sessions.history("cli:test")
    tool_message = next(message for message in history if message["role"] == "tool")
    assert "unknown tool" in tool_message["content"]
    await agent.sessions.close()


async def test_cancellation_releases_lock(tmp_path: Path) -> None:
    provider = FakeProvider([text_response("late")], delay=0.4)
    config = make_config(tmp_path)
    agent = AgentLoop(
        provider=provider,
        tools=ToolRegistry(),
        context=ContextBuilder(config),
        sessions=SqliteSessionStore(tmp_path / "s.sqlite"),
        session_key="cli:cancel",
        settings=config.agent,
        model="fake-model",
    )
    task = asyncio.create_task(agent.run("wait"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    history = await agent.sessions.history("cli:cancel")
    assert history and history[0]["content"] == "wait"
    # The agent must still be usable after cancellation.
    provider.responses = [text_response("after cancel")]
    result = await agent.run("again")
    assert result.content == "after cancel"
    await agent.sessions.close()


async def test_empty_response_is_reported(tmp_path: Path) -> None:
    agent, _ = build_agent(tmp_path, [text_response("")])
    result = await agent.run("hello")
    assert result.error and "empty" in result.error
    await agent.sessions.close()


async def test_tool_timeout(tmp_path: Path) -> None:
    async def slow() -> str:
        await asyncio.sleep(2)
        return "too late"

    tools = ToolRegistry(
        [FunctionTool("slow", "sleep", {"type": "object", "properties": {}}, slow, readonly=True)]
    )
    config = make_config(tmp_path)
    config.agent.tool_timeout = 0.1
    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("slow", {}), text_response("finished")],
        config=config,
        tools=tools,
    )
    result = await agent.run("timeout please")
    assert result.content == "finished"
    history = await agent.sessions.history("cli:test")
    tool_message = next(message for message in history if message["role"] == "tool")
    assert "timed out" in tool_message["content"]
    await agent.sessions.close()


async def test_history_trimming_keeps_recent(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.agent.max_history_messages = 2
    agent, provider = build_agent(tmp_path, [text_response("1")], config=config)
    assert (await agent.run("first")).content == "1"
    provider.responses = [text_response("2")]
    await agent.run("second")
    provider.responses = [text_response("3")]
    await agent.run("third")

    sent = provider.calls[-1]
    user_messages = [message["content"] for message in sent if message["role"] == "user"]
    assert "first" not in user_messages
    assert "third" in user_messages
    await agent.sessions.close()


async def test_tool_result_marks_error(tmp_path: Path) -> None:
    def explode(text: str) -> str:
        raise ValueError("bad input")

    tools = ToolRegistry(
        [
            FunctionTool(
                "explode",
                "always fails",
                {"type": "object", "properties": {"text": {"type": "string"}}},
                explode,
                readonly=True,
            )
        ]
    )
    agent, _ = build_agent(
        tmp_path,
        [tool_call_response("explode", {"text": "x"}), text_response("recovered")],
        tools=tools,
    )
    result = await agent.run("boom")
    assert result.content == "recovered"
    history = await agent.sessions.history("cli:test")
    tool_message = next(message for message in history if message["role"] == "tool")
    assert "crashed" in tool_message["content"] or "bad input" in tool_message["content"]
    await agent.sessions.close()
