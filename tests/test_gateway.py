"""Gateway: direct turns, task records, slash commands and per-session queueing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from anyaclaw.bus import InboundMessage
from anyaclaw.channels.base import Channel
from anyaclaw.gateway import _SessionWorker
from anyaclaw.runtime import Runtime
from tests.conftest import FakeProvider, make_config, text_response


class StubChannel(Channel):
    name = "stub"

    def __init__(self) -> None:
        self.sent: list = []
        self.started = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.sleep(3600)

    async def stop(self) -> None:
        return None

    async def send(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


def _runtime(tmp_path: Path, responses) -> Runtime:
    config = make_config(tmp_path)
    runtime = Runtime(config)
    runtime.provider = FakeProvider(responses)
    return runtime


async def test_run_turn_records_task(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [text_response("direct answer")])
    await runtime.setup()
    runtime.ensure_gateway()

    result, task_id = await runtime.gateway.run_turn(
        session_key="web:direct", channel="web", content="hello"
    )
    assert result.content == "direct answer"
    assert task_id
    task = await runtime.task_center.get(task_id)
    assert task is not None and task["status"] == "done"
    assert task["response"] == "direct answer"
    await runtime.shutdown()


async def test_commands(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    runtime.ensure_gateway()
    gateway = runtime.gateway
    assert gateway is not None

    message = InboundMessage(channel="stub", sender_id="u1", chat_id="c1", content="/help")
    handled = await gateway._handle_command(message)
    assert handled
    outbound = await asyncio.wait_for(runtime.bus.consume_outbound(), timeout=2)
    assert "/clear" in outbound.content

    clear = InboundMessage(channel="stub", sender_id="u1", chat_id="c1", content="/clear")
    assert await gateway._handle_command(clear)
    outbound = await asyncio.wait_for(runtime.bus.consume_outbound(), timeout=2)
    assert "已清空" in outbound.content

    status = InboundMessage(channel="stub", sender_id="u1", chat_id="c1", content="/status")
    assert await gateway._handle_command(status)
    outbound = await asyncio.wait_for(runtime.bus.consume_outbound(), timeout=2)
    assert "状态" in outbound.content
    await runtime.shutdown()


async def test_memory_commands(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    runtime.ensure_gateway()
    gateway = runtime.gateway
    assert gateway is not None

    remember = InboundMessage(
        channel="stub", sender_id="u1", chat_id="c1", content="/remember 用户喜欢 Rust"
    )
    assert await gateway._handle_command(remember)
    outbound = await asyncio.wait_for(runtime.bus.consume_outbound(), timeout=2)
    assert "已记住" in outbound.content

    listing = InboundMessage(channel="stub", sender_id="u1", chat_id="c1", content="/memory")
    assert await gateway._handle_command(listing)
    outbound = await asyncio.wait_for(runtime.bus.consume_outbound(), timeout=2)
    assert "Rust" in outbound.content
    await runtime.shutdown()


async def test_sender_whitelist(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.security.enforce_sender_whitelist = True
    config.security.allowed_senders = {"stub": ["allowed"]}
    runtime = Runtime(config)
    runtime.provider = FakeProvider([])
    runtime.ensure_gateway()

    allowed = InboundMessage(channel="stub", sender_id="allowed", chat_id="c", content="hi")
    denied = InboundMessage(channel="stub", sender_id="nope", chat_id="c", content="hi")
    assert runtime.gateway._sender_allowed(allowed) is True
    assert runtime.gateway._sender_allowed(denied) is False
    await runtime.shutdown()


async def test_cancel_returns_false_when_idle(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    runtime.ensure_gateway()
    assert runtime.gateway.cancel_session("web:none") is False
    await runtime.shutdown()


async def test_queue_notifies_when_busy(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [text_response("ok")])
    runtime.ensure_gateway()
    gateway = runtime.gateway
    assert gateway is not None

    blocking = asyncio.create_task(asyncio.sleep(0.5))
    worker = gateway._workers.setdefault("stub:u1", _SessionWorker())
    worker.current = blocking
    message = InboundMessage(channel="stub", sender_id="u1", chat_id="c", content="second")
    await gateway._enqueue(message)
    outbound = await asyncio.wait_for(runtime.bus.consume_outbound(), timeout=2)
    assert "完成后处理" in outbound.content
    blocking.cancel()
    await asyncio.gather(blocking, return_exceptions=True)
    await runtime.shutdown()
