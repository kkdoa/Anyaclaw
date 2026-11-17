"""Web channel: FastAPI + Server-Sent Events + a dependency-free UI.

The browser talks to the same :class:`~anyaclaw.core.agent.AgentLoop` as every
other channel, but streaming is handled per request (the SSE response carries the
agent's own event stream), while a hub-wide ``/api/events`` feed is available for
monitoring. Uploads land in the generated-file workspace, and downloads can only
resolve inside it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anyaclaw.bus import OutboundMessage
from anyaclaw.channels.base import Channel
from anyaclaw.logging import get_logger

try:  # optional dependency
    from fastapi import FastAPI, File, HTTPException, Request, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import (
        FileResponse,
        JSONResponse,
        PlainTextResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when the extra is absent
    FastAPI = None  # type: ignore[assignment]
    FASTAPI_AVAILABLE = False

if TYPE_CHECKING:  # pragma: no cover
    from anyaclaw.runtime import Runtime

log = get_logger("channel.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebChannel(Channel):
    name = "web"

    def __init__(self, runtime: Runtime, *, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self._server: Any = None

    async def start(self) -> None:
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "the web UI needs fastapi + uvicorn: pip install 'anyaclaw[web]'"
            ) from exc

        app = create_app(self.runtime)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        log.info("web UI on http://%s:%s", self.host, self.port)
        await self._server.serve()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    async def send(self, message: OutboundMessage) -> None:
        # Web replies are streamed by the request that produced them; nothing to do
        # here beyond exposing the message to monitor clients.
        self.runtime.hub.publish(
            {
                "type": "outbound",
                "channel": "web",
                "chat_id": message.chat_id,
                "content": message.content[:500],
                "files": message.files,
            }
        )


def create_app(runtime: Runtime) -> Any:
    """Build the FastAPI application for ``runtime``."""
    if not FASTAPI_AVAILABLE:
        raise RuntimeError("pip install 'anyaclaw[web]' to use the web UI")

    config = runtime.config
    heartbeat = max(5.0, float(config.web.sse_heartbeat))
    app = FastAPI(
        title="Anyaclaw",
        version=runtime.version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    if config.web.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.web.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    token = config.security.web_auth_token

    @app.middleware("http")
    async def _auth(request: Request, call_next):  # noqa: ANN001, ANN202
        if token and request.url.path.startswith("/api/") and request.url.path not in {
            "/api/health"
        }:
            import hmac

            supplied = request.headers.get("authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:]
            supplied = supplied or request.query_params.get("token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # -- meta --------------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": runtime.version,
            "model": config.provider.model,
            "uptime_seconds": round(time.time() - runtime.started_at, 1),
            "sessions": len(await runtime.sessions.list_sessions(limit=1000)),
        }

    @app.get("/api/config")
    async def public_config() -> dict[str, Any]:
        return config.public_dict()

    @app.get("/api/tools")
    async def tools() -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "readonly": bool(getattr(tool, "readonly", False)),
                }
                for tool in runtime.tools
            ],
            "mcp": runtime.mcp_status(),
        }

    @app.get("/api/metrics")
    async def metrics(request: Request) -> Any:
        if request.query_params.get("format") == "prometheus":
            return PlainTextResponse(runtime.metrics.prometheus())
        snapshot = runtime.metrics.snapshot()
        snapshot["gateway"] = runtime.gateway.snapshot() if runtime.gateway else {}
        return snapshot

    # -- sessions ----------------------------------------------------------
    @app.get("/api/sessions")
    async def sessions() -> dict[str, Any]:
        items = await runtime.sessions.list_sessions(limit=200)
        return {"sessions": [item.to_dict() for item in items]}

    @app.get("/api/sessions/{session_key}/messages")
    async def session_messages(session_key: str) -> dict[str, Any]:
        key = _normalise_session(session_key, allow_default=False)
        return {"messages": await runtime.sessions.history(key, limit=500)}

    @app.delete("/api/sessions/{session_key}")
    async def delete_session(session_key: str) -> dict[str, Any]:
        key = _normalise_session(session_key, allow_default=True)
        await runtime.sessions.clear(key)
        return {"ok": True}

    # -- chat --------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(request: Request) -> Any:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc

        message = str(body.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        session_key = _normalise_session(str(body.get("session_key") or ""))

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def sink(event) -> None:  # noqa: ANN001
            payload = event.to_dict()
            if payload.get("type") == "turn_end":
                payload["files"] = [
                    f"/api/files/{Path(path).name}" for path in payload.get("files", [])
                ]
            queue.put_nowait(payload)

        async def runner() -> None:
            try:
                result, task_id = await asyncio.wait_for(
                    runtime.gateway.run_turn(
                        session_key=session_key,
                        channel="web",
                        content=message,
                        sink=sink,
                    ),
                    timeout=max(30.0, float(config.web.chat_timeout)),
                )
                queue.put_nowait(
                    {
                        "type": "result",
                        "task_id": task_id,
                        "files": [f"/api/files/{Path(p).name}" for p in result.files],
                        "usage": result.usage.to_dict(),
                        "iterations": result.iterations,
                    }
                )
            except asyncio.CancelledError:
                queue.put_nowait({"type": "cancelled", "reason": "client disconnected"})
                raise
            except Exception as exc:  # noqa: BLE001 - report, never crash the app
                log.exception("web chat failed")
                queue.put_nowait({"type": "error", "message": str(exc)})
            finally:
                queue.put_nowait(None)

        turn = asyncio.create_task(runner(), name=f"web-turn:{session_key}")

        async def event_stream():  # noqa: ANN202
            try:
                yield _sse({"type": "turn_start", "session_key": session_key, "message": message})
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        break
                    yield _sse(item)
                yield "data: [DONE]\n\n"
            finally:
                if not turn.done():
                    turn.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/chat/{session_key}/cancel")
    async def cancel(session_key: str) -> dict[str, Any]:
        key = _normalise_session(session_key, allow_default=False)
        return {"ok": runtime.gateway.cancel_session(key)}

    # -- approvals ---------------------------------------------------------
    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"pending": runtime.approvals.pending()}

    @app.post("/api/approvals/{request_id}")
    async def resolve_approval(request_id: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        approved = bool(body.get("approved"))
        ok = runtime.approvals.resolve(request_id, approved)
        return {"ok": ok, "approved": approved}

    # -- tasks -------------------------------------------------------------
    @app.get("/api/tasks")
    async def tasks() -> dict[str, Any]:
        if runtime.task_center is None:
            return {"tasks": []}
        return {"tasks": await runtime.task_center.list(limit=50)}

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str) -> dict[str, Any]:
        if runtime.task_center is None:
            raise HTTPException(status_code=404, detail="task centre disabled")
        task = await runtime.task_center.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    # -- files -------------------------------------------------------------
    @app.get("/api/files")
    async def list_files() -> dict[str, Any]:
        return {"files": runtime.workspace.list_files(limit=100)}

    @app.get("/api/files/{name}")
    async def download(name: str) -> Any:
        try:
            path = runtime.workspace.resolve(name)
        except Exception as exc:  # noqa: BLE001 - ToolError for traversal/missing
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path, filename=path.name)

    try:
        @app.post("/api/files")
        async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:  # noqa: B008
            saved = []
            for item in files:
                data = await item.read()
                saved.append(await runtime.workspace.save_bytes(item.filename or "upload.bin", data))
            return {"files": saved}

    except Exception:  # pragma: no cover - python-multipart missing
        log.warning("multipart upload disabled: pip install python-multipart")

    # -- monitoring feed ---------------------------------------------------
    @app.get("/api/events")
    async def events(request: Request) -> Any:
        queue = runtime.hub.subscribe()

        async def stream():  # noqa: ANN202
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield _sse(item)
            finally:
                runtime.hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # -- IM webhooks (QQ / Feishu) -----------------------------------------
    for channel in getattr(runtime, "channels", []):
        router_factory = getattr(channel, "router", None)
        if not callable(router_factory):
            continue
        try:
            router = router_factory()
            if router is not None:
                app.include_router(router)
        except Exception:  # noqa: BLE001 - a broken adapter must not break the app
            log.exception("failed to mount webhook router for %s", channel.name)

    # -- static UI ---------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> Any:
        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():  # pragma: no cover - packaging issue
            return PlainTextResponse(
                "Web UI assets are missing. Reinstall anyaclaw or run from a source checkout.",
                status_code=500,
            )
        return FileResponse(index_file)

    @app.get("/favicon.ico")
    async def favicon() -> Any:
        icon = STATIC_DIR / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return PlainTextResponse("", status_code=204)

    return app


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _normalise_session(raw: str, *, allow_default: bool = True) -> str:
    """Validate/sanitise a session key so it cannot address other channels."""
    value = (raw or "").strip()
    if not value:
        if not allow_default:
            raise ValueError("session key is required")
        import uuid

        return f"web:{uuid.uuid4().hex[:12]}"
    if value.startswith("web:"):
        value = value[4:]
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_:.")[:120]
    if not cleaned:
        raise ValueError("invalid session key")
    return f"web:{cleaned}"
