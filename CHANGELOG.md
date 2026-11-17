# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/).

## [Unreleased]

### Planned
- Desktop shell package, vision input, semantic memory (see `docs/roadmap.md`).

## [0.1.0] — 2026-11-17

First public alpha: a from-scratch runtime built around a gateway + channels + provider + agent loop
architecture, with SQLite storage, a policy-gated tool layer and a web console.

### Added
- Agent loop with streaming deltas, tool budgets, per-tool timeouts, loop-fuse warning/abort
  thresholds, parallel read-only tool calls and context compaction.
- OpenAI-compatible provider over `httpx`: SSE parsing, reasoning content, tool-call deltas,
  usage accounting, retries with exponential back-off and `Retry-After`.
- 19 built-in tools: files (`read`/`write`/`edit`/`list`/`glob`/`grep`/`delete`), sandboxed `exec`,
  `web_search`/`web_fetch`, memory (`remember`/`search`/`forget`/`list`), documents
  (`create`/`read` for md/txt/html/docx/pptx/xlsx/pdf/csv), `spawn_subagent`, `workspace_info`,
  `system_info`.
- MCP client support (optional `mcp` extra) with per-server status.
- Channels: CLI REPL, Web console (FastAPI + SSE + vanilla-JS UI with approvals and uploads),
  QQ / Feishu / DingTalk adapters.
- Storage: SQLite session store (WAL), task centre with event timeline, FTS5 memory with trigram
  tokenizer, JSONL backend and importer.
- Security: workspace path sandbox (symlink-safe), tool policy engine, approval broker,
  command allow/deny lists, secret-scrubbed environment, SSRF guard, optional web auth token.
- Observability: structured JSON logs with trace/session ids, per-model and per-session token
  metrics, tool latency counters, `/api/metrics` (JSON + Prometheus text).
- Packaging and DX: PEP 621 `pyproject.toml`, console entry point, extras
  (`web`, `office`, `mcp`, `dingtalk`, `all`, `dev`), `anyaclaw init`, `anyaclaw doctor`,
  Dockerfile, docker-compose, GitHub Actions CI, rename script for forks.
- 108 pytest tests covering configuration, sessions, memory, tasks, tools, policy, the agent loop,
  the gateway and the HTTP API.

### Changed versus the reference implementation
- Config is loaded once and validated with typed dataclasses; unknown keys are errors.
- Tool results are structured (`ToolResult.files`) instead of `[[FILE:…]]` markers in text.
- Sessions, tasks and memory are SQLite-backed instead of JSONL + `MEMORY.md`.
- Streaming is owned by the provider; retries cannot duplicate already-streamed text.
- Per-session message queues replace "a task is already running" rejections.

See `docs/improvements.md` for the full list of 30 concrete differences.

[Unreleased]: https://github.com/kkdoa/Anyaclaw/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kkdoa/Anyaclaw/releases/tag/v0.1.0
