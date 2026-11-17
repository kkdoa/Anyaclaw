"""Terminal channel: an interactive REPL with live streaming output."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from anyaclaw.bus import EventHub, InboundMessage, MessageBus, OutboundMessage
from anyaclaw.channels.base import Channel
from anyaclaw.logging import get_logger

log = get_logger("channel.cli")

_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


class CLIChannel(Channel):
    name = "cli"

    def __init__(
        self,
        bus: MessageBus,
        hub: EventHub,
        *,
        prompt: str = "you",
        show_reasoning: bool = True,
        user_id: str = "local",
        on_exit: Any = None,
    ) -> None:
        self.bus = bus
        self.hub = hub
        self.prompt = prompt
        self.show_reasoning = show_reasoning
        self.user_id = user_id
        self.on_exit = on_exit
        self._closed = False
        self._streamed: dict[str, int] = {}
        self._reasoning_open = False
        self._renderer: asyncio.Task | None = None
        self._colour = sys.stdout.isatty()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        queue = self.hub.subscribe()
        self._renderer = asyncio.create_task(self._render_events(queue), name="cli:render")
        self._print_banner()
        try:
            while not self._closed:
                try:
                    line = await asyncio.to_thread(
                        input, f"\n{self._colourise(self.prompt + ' > ', _CYAN)}"
                    )
                except (EOFError, KeyboardInterrupt):
                    self._closed = True
                    break
                text = line.strip()
                if not text:
                    continue
                if text in {"/exit", "/quit", "exit", "quit"}:
                    self._closed = True
                    break
                if text == "/new":
                    await self.bus.publish_inbound(
                        InboundMessage(
                            channel=self.name,
                            sender_id=self.user_id,
                            chat_id=self.user_id,
                            content="/clear",
                        )
                    )
                    continue
                await self.bus.publish_inbound(
                    InboundMessage(
                        channel=self.name,
                        sender_id=self.user_id,
                        chat_id=self.user_id,
                        content=text,
                    )
                )
        finally:
            self.hub.unsubscribe(queue)
            if self._renderer is not None:
                self._renderer.cancel()
            if self.on_exit is not None:
                try:
                    # Give the gateway a moment to pick up the last message.
                    await asyncio.sleep(0.2)
                    outcome = self.on_exit(f"{self.name}:{self.user_id}")
                    if asyncio.iscoroutine(outcome):
                        await outcome
                except Exception:  # noqa: BLE001 - shutdown best effort
                    log.exception("cli on_exit failed")

    async def stop(self) -> None:
        self._closed = True

    async def send(self, message: OutboundMessage) -> None:
        key = f"{message.channel}:{self.user_id}"
        streamed = self._streamed.pop(key, 0)
        if self._reasoning_open:
            print()
            self._reasoning_open = False
        if streamed:
            # The answer already streamed to the terminal; just terminate the line.
            print()
        elif message.content:
            print(f"\n{self._colourise('assistant > ', _GREEN)}{message.content}")
        for path in message.files or []:
            print(self._colourise(f"  [file] {path}", _DIM))

    # -- rendering ---------------------------------------------------------
    async def _render_events(self, queue: asyncio.Queue) -> None:
        while not self._closed:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                break
            if event.get("channel") != self.name:
                continue
            kind = event.get("type")
            key = str(event.get("session_key", ""))
            try:
                if kind == "reasoning_delta" and self.show_reasoning:
                    if not self._reasoning_open:
                        print(self._colourise("\n  .. thinking", _DIM))
                        self._reasoning_open = True
                    print(self._colourise(str(event.get("content", "")), _DIM), end="", flush=True)
                elif kind == "content_delta":
                    if self._reasoning_open:
                        print()
                        self._reasoning_open = False
                    if not self._streamed.get(key):
                        print(f"\n{self._colourise('assistant > ', _GREEN)}", end="", flush=True)
                    self._streamed[key] = self._streamed.get(key, 0) + len(
                        str(event.get("content", ""))
                    )
                    print(str(event.get("content", "")), end="", flush=True)
                elif kind == "tool_call":
                    args = event.get("arguments") or {}
                    preview = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
                    print(
                        self._colourise(f"\n  [tool] {event.get('tool')}({preview})", _YELLOW),
                        flush=True,
                    )
                elif kind == "tool_result":
                    status = "error" if event.get("is_error") else "ok"
                    print(self._colourise(f"    -> {status}", _YELLOW), flush=True)
                elif kind == "error":
                    print(self._colourise(f"\n  x {event.get('message')}", _RED), flush=True)
            except Exception:  # noqa: BLE001 - rendering must not crash the app
                log.exception("cli render failed")

    # -- helpers -----------------------------------------------------------
    def _print_banner(self) -> None:
        print(self._colourise("Anyaclaw", _CYAN) + " - /help 查看命令，/exit 退出")
        print(self._colourise("输入消息开始对话；Ctrl+C 退出。", _DIM))

    def _colourise(self, text: str, colour: str) -> str:
        return f"{colour}{text}{_RESET}" if self._colour else text
