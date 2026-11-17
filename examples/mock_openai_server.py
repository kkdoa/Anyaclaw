"""A tiny OpenAI-compatible mock server for offline demos and end-to-end tests.

Run it, then point Anyaclaw at it with ``examples/config.mock.toml``:

    python examples/mock_openai_server.py            # http://127.0.0.1:8001/v1
    anyaclaw serve --config examples/config.mock.toml --no-cli

Behaviour:

* if the latest user message asks for a tool (contains "tool" / "工具" / "system")
  and no tool result is present yet, it returns a ``system_info`` tool call;
* otherwise it streams a short Markdown answer that echoes the question.

It implements just enough of the protocol (streaming SSE, tool_calls deltas, usage)
to exercise the real provider code path.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Anyaclaw mock LLM")

MODEL = "mock-model"


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def _latest_user(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(message.get("role") == "tool" for message in messages)


def _answer(question: str) -> str:
    return (
        f"## Mock answer\n\n"
        f"You asked: **{question}**\n\n"
        f"This response came from `examples/mock_openai_server.py`, so no API key or "
        f"network access was used.\n\n"
        f"- streaming works\n"
        f"- tool calls work\n"
        f"- the whole runtime loop is exercised\n"
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    question = _latest_user(messages)
    wants_tool = (
        not _has_tool_result(messages)
        and bool(body.get("tools"))
        and any(word in question.lower() for word in ("tool", "工具", "system", "系统"))
    )

    if not body.get("stream"):
        if wants_tool:
            return JSONResponse(
                {
                    "id": _completion_id(),
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_mock_1",
                                        "type": "function",
                                        "function": {"name": "system_info", "arguments": "{}"},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )
        return JSONResponse(
            {
                "id": _completion_id(),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _answer(question)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 40, "total_tokens": 52},
            }
        )

    async def stream():  # noqa: ANN202
        completion_id = _completion_id()
        created = int(time.time())

        def frame(delta: dict[str, Any], finish: str | None = None) -> str:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if wants_tool:
            yield frame(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_mock_1",
                            "type": "function",
                            "function": {"name": "system_info", "arguments": "{}"},
                        }
                    ],
                }
            )
            yield frame({}, finish="tool_calls")
        else:
            text = _answer(question)
            yield frame({"role": "assistant", "content": ""})
            step = 12
            for index in range(0, len(text), step):
                yield frame({"content": text[index : index + step]})
            yield frame({}, finish="stop")

        usage = {
            "prompt_tokens": 12,
            "completion_tokens": 40,
            "total_tokens": 52,
        }
        yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': MODEL, 'choices': [], 'usage': usage})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
