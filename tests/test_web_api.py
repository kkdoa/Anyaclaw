"""HTTP API: health, tools, chat streaming, sessions, approvals, downloads."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from anyaclaw.channels.web import create_app  # noqa: E402
from anyaclaw.runtime import Runtime  # noqa: E402
from tests.conftest import FakeProvider, make_config, text_response, tool_call_response  # noqa: E402


def build_client(tmp_path: Path, responses) -> tuple[TestClient, Runtime]:
    config = make_config(tmp_path)
    runtime = Runtime(config)
    runtime.provider = FakeProvider(responses)
    runtime.ensure_gateway()
    return TestClient(create_app(runtime)), runtime


def test_health_and_tools(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [])
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["model"] == "fake-model"

    tools = client.get("/api/tools").json()["tools"]
    names = {tool["name"] for tool in tools}
    assert {"read_file", "write_file", "exec", "web_search", "create_document"} <= names
    assert all("readonly" in tool for tool in tools)


def test_config_is_masked(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [])
    payload = client.get("/api/config").json()
    assert payload["provider"]["api_key"] == "***"


def test_chat_streams_sse(tmp_path: Path) -> None:
    client, runtime = build_client(tmp_path, [text_response("hello from model")])
    response = client.post(
        "/api/chat", json={"session_key": "web:test", "message": "hi"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "content_delta" in body
    assert "hello from model" in body
    assert "[DONE]" in body

    sessions = client.get("/api/sessions").json()["sessions"]
    assert any(item["key"] == "web:test" for item in sessions)

    messages = client.get("/api/sessions/web:test/messages").json()["messages"]
    assert messages[0] == {"role": "user", "content": "hi"}
    assert messages[-1]["content"] == "hello from model"


def test_chat_runs_tools(tmp_path: Path) -> None:
    client, _runtime = build_client(
        tmp_path,
        [tool_call_response("workspace_info", {}), text_response("all done")],
    )
    body = client.post("/api/chat", json={"session_key": "web:tools", "message": "look"}).text
    assert "tool_call" in body
    assert "workspace_info" in body
    assert "all done" in body


def test_validation_errors(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [])
    assert client.post("/api/chat", json={"message": ""}).status_code == 400
    assert client.post("/api/chat", content=b"not-json").status_code == 400


def test_session_delete(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [text_response("x")])
    client.post("/api/chat", json={"session_key": "web:gone", "message": "hi"})
    assert client.delete("/api/sessions/web:gone").json()["ok"] is True
    assert client.get("/api/sessions/web:gone/messages").json()["messages"] == []


def test_cancel_without_running_turn(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [])
    assert client.post("/api/chat/web:idle/cancel").json()["ok"] is False


def test_approvals_endpoint(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [])
    assert client.get("/api/approvals").json() == {"pending": []}
    assert client.post("/api/approvals/unknown", json={"approved": True}).json()["ok"] is False


def test_metrics_endpoint(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [text_response("metrics")])
    client.post("/api/chat", json={"session_key": "web:m", "message": "hi"})
    snapshot = client.get("/api/metrics").json()
    assert snapshot["models"]["fake-model"]["calls"] == 1
    text = client.get("/api/metrics?format=prometheus").text
    assert "anyaclaw_model_tokens_total" in text


def test_file_download_is_confined(tmp_path: Path) -> None:
    client, runtime = build_client(tmp_path, [])
    target = runtime.config.files_dir / "report.txt"
    target.write_text("content", encoding="utf-8")
    response = client.get("/api/files/report.txt")
    assert response.status_code == 200 and response.text == "content"
    assert client.get("/api/files/..%2F..%2Fsecret.txt").status_code == 404


def test_upload_roundtrip(tmp_path: Path) -> None:
    client, runtime = build_client(tmp_path, [])
    response = client.post(
        "/api/files", files={"files": ("note.txt", b"uploaded bytes", "text/plain")}
    )
    assert response.status_code == 200, response.text
    saved = response.json()["files"][0]
    assert saved["name"] == "note.txt"
    assert (runtime.config.files_dir / "note.txt").read_bytes() == b"uploaded bytes"


def test_index_page_served(tmp_path: Path) -> None:
    client, _runtime = build_client(tmp_path, [])
    response = client.get("/")
    assert response.status_code == 200
    assert "Anyaclaw" in response.text
