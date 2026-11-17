#!/usr/bin/env python3
"""End-to-end smoke test against a running Anyaclaw web server.

    python examples/mock_openai_server.py            # terminal 1
    anyaclaw serve --config examples/config.mock.toml --no-cli   # terminal 2
    python scripts/smoke_test.py http://127.0.0.1:8081

Exits 0 when every check passes, 1 otherwise. No third-party dependencies.
"""

from __future__ import annotations

import contextlib
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8081"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def get(path: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(f"{BASE}{path}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", "replace")}


def post_sse(path: str, payload: dict) -> list[dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=120) as response:
        buffer = b""
        while True:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += chunk
            if buffer.endswith(b"\n\n"):
                block = buffer.decode("utf-8", "replace").strip()
                buffer = b""
                for line in block.splitlines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        return events
                    with contextlib.suppress(json.JSONDecodeError):
                        events.append(json.loads(raw))
    return events


def main() -> int:
    print(f"target: {BASE}\n")

    status, body, _ = get("/api/health")
    health = json.loads(body or b"{}")
    check("health endpoint", status == 200 and health.get("status") == "ok", f"{status} {health}")
    check("health reports a model", bool(health.get("model")), str(health))

    status, body, headers = get("/")
    check("console HTML served", status == 200 and b"Anyaclaw" in body, str(status))

    status, body, _ = get("/static/app.js")
    check("static JS served", status == 200 and len(body) > 10_000, f"{status} {len(body)} bytes")
    status, body, _ = get("/static/style.css")
    check("static CSS served", status == 200 and len(body) > 5_000, f"{status} {len(body)} bytes")

    status, body, _ = get("/api/tools")
    tools = json.loads(body or b"{}").get("tools", [])
    names = {tool["name"] for tool in tools}
    check("tool list", status == 200 and len(tools) >= 5, f"{status} {sorted(names)[:6]}")
    check("read_file registered", "read_file" in names, str(sorted(names)))

    status, body, _ = get("/api/config")
    config = json.loads(body or b"{}")
    check("config masked", status == 200 and config["provider"]["api_key"] == "***", str(config)[:120])

    events = post_sse("/api/chat", {"session_key": "web:smoke", "message": "hello smoke test"})
    kinds = [event.get("type") for event in events]
    check("chat stream returns events", bool(events), str(kinds))
    check("stream contains content deltas", "content_delta" in kinds, str(kinds))
    final = next((event for event in events if event.get("type") == "turn_end"), None)
    check("turn_end with content", bool(final and final.get("content")), str(final)[:200])
    check("usage reported", bool(final and final.get("usage", {}).get("total_tokens")), str(final)[:200])

    tool_events = post_sse(
        "/api/chat", {"session_key": "web:tool", "message": "please use the tool"}
    )
    tool_kinds = [event.get("type") for event in tool_events]
    check("tool call executed", "tool_call" in tool_kinds, str(tool_kinds))
    check("tool result returned", "tool_result" in tool_kinds, str(tool_kinds))
    tool_result = next(
        (event for event in tool_events if event.get("type") == "tool_result"), {}
    )
    check("tool result not an error", not tool_result.get("is_error"), str(tool_result)[:200])

    status, body, _ = get("/api/sessions")
    sessions = json.loads(body or b"{}").get("sessions", [])
    check("session persisted", any(item["key"] == "web:smoke" for item in sessions), str(sessions)[:200])

    status, body, _ = get("/api/sessions/web:smoke/messages")
    messages = json.loads(body or b"{}").get("messages", [])
    check("transcript persisted", len(messages) >= 2, str(messages)[:200])

    status, body, _ = get("/api/metrics")
    metrics = json.loads(body or b"{}")
    check("metrics recorded", status == 200 and metrics.get("models"), str(metrics)[:200])

    status, body, _ = get("/api/tasks")
    tasks = json.loads(body or b"{}").get("tasks", [])
    check("task records", status == 200 and len(tasks) >= 2, f"{status} {len(tasks)} tasks")

    status, _payload = post_json("/api/approvals/does-not-exist", {"approved": True})
    check("unknown approval rejected", status == 200, str(status))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
