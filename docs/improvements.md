# Design notes: what shaped Anyaclaw

Anyaclaw was built around one question: what should a local-first agent runtime look like if it had
to survive daily use? This page records the concrete failure modes that shaped the design, grouped
by area, and how Anyaclaw handles each one.

## Reliability

| # | Failure mode | Why it hurts | Anyaclaw |
|---|---|---|---|
| 1 | The agent factory calls `load_config()` again at the top | An injected config is discarded; the file is re-read per session; environment overrides apply twice; a missing key calls `sys.exit(1)` from library code | `load_config()` runs once in `cli.py`; the resulting `Config` is passed down. Providers raise `ConfigError` instead of exiting |
| 2 | Model calls have no timeout and no retry; `except Exception` returns `LLMResponse(content="错误: API 调用失败 - …")` | A 429 or a 30 s hiccup becomes a **permanent assistant message in the transcript**, and the model is asked to continue from its own error text | `ProviderError` with `retryable`; exponential back-off + jitter, `Retry-After` honoured; retries only before the first streamed delta; HTTP status and redacted body surfaced |
| 3 | Tool calls have no per-call timeout | One hung `exec` blocks the whole session forever | Every call goes through `asyncio.wait_for(tool_timeout)`; timeouts return a structured error the model can react to |
| 4 | A loop fuse aborts the **entire turn** once a signature repeats ≥ 20 times, and returns the fuse message as the final answer | Legitimate retries with tiny differences get killed; users see a system error as if it were the model's answer | Two thresholds: at `loop_warn_after` the model receives a "you already did this" tool result; only at `loop_abort_after` does the turn end with an explicit error |
| 5 | Tool exceptions are stringified into `错误: 执行工具 … 失败` | The loop cannot distinguish a tool bug from a model mistake; metrics cannot count failures | `ToolResult(is_error=True, meta={…})`; registry records counts and latency |
| 6 | A `_flush_stream_state()` helper mutates stream buffers from several tasks and returns inconsistent flags after popping state | Duplicate or dropped chat messages; hard to reason about | Streaming is per-request: the web SSE response owns its queue; IM channels receive one final message (no coalescing bugs) |
| 7 | The gateway rejects a second message while a turn runs: "当前会话已有任务正在执行" | People type two messages in a row constantly | Serial queue per session: the second message is queued with a notice and answered in order |
| 8 | `/cancel` kills the asyncio task and prints "任务已打断", leaving a half-written assistant message | The transcript can end with an unmatched tool call | Cancellation is explicit: `cancelled` event, task status, lock released, session remains usable |
| 9 | The main loop uses blocking `input()` inside async code and `os._exit(0)` to kill leftover threads | Ungraceful shutdown, lost SQLite writes | CLI reads with `asyncio.to_thread`, exit goes through `Gateway.request_stop()`, stores close cleanly |
| 10 | `asyncio.gather` over all channels with no isolation | One misconfigured bot kills the web UI | Each channel runs in an isolated task; startup failures are logged and skipped |

## Storage

| # | Failure mode | Anyaclaw |
|---|---|---|
| 11 | Sessions are one JSONL file per session named by `key.replace(":", "_")` | `SqliteSessionStore` (WAL) with indexed ordering; JSONL kept as an alternative and importable via `import_jsonl_dir()` |
| 12 | `list_sessions()` reverses `_` → `:` and mislabels keys containing underscores | Session metadata is stored explicitly (channel, title, timestamps, message count) |
| 13 | Long-term memory is a `MEMORY.md` file appended through `write_file` | `MemoryStore` on SQLite FTS5 (trigram tokenizer for Chinese), deduplicated, searchable, exportable back to Markdown |
| 14 | No task history: once the console scrollback is gone, a run is unaccountable | `TaskCenter` records status, response, files, tool-call count and an event timeline, exposed via `/api/tasks` and `/tasks` |

## Security

| # | Failure mode | Anyaclaw |
|---|---|---|
| 15 | Paths are joined with the workspace then passed to `open()`; `../` escapes | `resolve_workspace_path()` resolves symlinks and rejects anything outside the workspace |
| 16 | `exec` runs with the full process environment | Secrets (`*_API_KEY`, `*TOKEN*`, `*SECRET*`, …) are stripped; `PYTHONUTF8` forced for predictable output |
| 17 | `exec` has no working-directory restriction, no output cap, no process-tree kill | CWD is validated, output truncated, timeouts kill the tree (`taskkill /T` on Windows), optional command allow-list and deny patterns |
| 18 | A webhook-reachable bot with no endpoint auth | `exec`, `delete_path`, mail and webhook sends are confirmation-gated; chat channels auto-deny by default |
| 19 | API keys live only in a JSON config file | `env:VAR` / `${VAR}` interpolation, conventional env fallbacks, masked config endpoint, log redaction |
| 20 | `web_fetch` will fetch anything, including `http://127.0.0.1:…` | SSRF guard: literal and resolved private/loopback/link-local addresses are refused |

## Observability

| # | Failure mode | Anyaclaw |
|---|---|---|
| 21 | `print()` everywhere; no levels, no correlation ids | JSON (or compact text) logging with `trace_id` and `session` from contextvars |
| 22 | No token accounting — "what did yesterday cost?" is unanswerable | `Metrics` records prompt/completion/cached tokens per model and session, turn latency, tool latency and error counts; `/api/metrics` exposes JSON and Prometheus text |
| 23 | Tool output only visible in console previews | Structured `tool_call` / `tool_result` events, task timeline, SSE stream to the browser |
| 24 | No health check | `/api/health`, `GET /api/metrics`, `anyaclaw doctor` |

## Architecture & developer experience

| # | Failure mode | Anyaclaw |
|---|---|---|
| 25 | A 105 KB `desktop_app.py` monolith; PySide6 is a hard dependency of everything | Runtime is UI-agnostic; the desktop shell is a documented contract (`Gateway.run_turn(sink=…)`); PySide6 is not a dependency |
| 26 | No `pyproject.toml`, unpinned `requirements.txt`, no package entry point | PEP 621 packaging, `pip install -e ".[web,office,mcp]"`, `anyaclaw` console script, optional extras |
| 27 | Duplicated provider construction (three copies of `OpenAICompatProvider(...)` in `main.py`) | `Runtime.build_provider()` is the only factory; subagents get their own provider so closing it cannot break the parent |
| 28 | Config is a JSON dict with ad-hoc `get()` calls; typos silently ignored | Typed dataclasses with validation; unknown keys are errors with the full path |
| 29 | No tests, no CI, no linters | 108 pytest tests (config, loop, policy, tools, web API, gateway, memory, tasks), ruff-clean, GitHub Actions |
| 30 | "Run it and see" debugging only | Deterministic unit tests with a scripted `FakeProvider`; tools testable in isolation |

## Deliberate non-goals

* **Not a framework.** No plugin marketplace, no graph orchestration, no multi-tenant web app.
* **No silent autonomy.** Mutating actions are confirm-gated by default; a bot that can `rm -rf`
  without asking is not a feature.
* **No vendored desktop UI.** A Qt shell belongs in a sibling package consuming this runtime.
