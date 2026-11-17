# Anyaclaw

> A local-first, multi-channel AI agent runtime with a real agent loop, tools, MCP, a web console and chat bots.
>
> 本地优先、多渠道的 AI Agent 运行时：真正的 Agent Loop、工具系统、MCP、Web 控制台与聊天机器人。

**English** · [简体中文](README.zh-CN.md)

[![CI](https://github.com/kkdoa/Anyaclaw/actions/workflows/ci.yml/badge.svg)](https://github.com/kkdoa/Anyaclaw/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Anyaclaw runs on your machine, talks to any OpenAI-compatible model endpoint, and reaches you
through the terminal, a browser, or a chat bot. The design decisions behind it — and the failure
modes they address — are documented in [docs/improvements.md](docs/improvements.md).

中文读者：完整的中文文档见 **[README.zh-CN.md](README.zh-CN.md)**，涵盖功能特性、快速开始、架构、工具、安全模型、可观测性与开发说明。

**Status:** alpha. The core loop, tools and web console are stable and covered by 100+ tests;
messaging-channel adapters are beta (see [docs/channels.md](docs/channels.md)).

---

## Features

| Area | What you get |
|---|---|
| **Agent loop** | Streaming tool-calling loop with iteration/tool budgets, per-call timeouts, loop-fuse protection, parallel read-only tools, context compaction |
| **Providers** | Any OpenAI-compatible `/chat/completions` (SiliconFlow, DeepSeek, OpenAI, Kimi, Qwen, vLLM, Ollama…), retries with jitter, `Retry-After` support, reasoning-content passthrough |
| **Tools** | Files (read/write/edit/glob/grep/delete), sandboxed shell, web search + fetch, long-term memory, documents (docx/pptx/xlsx/pdf/csv/md/html), subagents, MCP servers |
| **Channels** | CLI REPL, Web console (SSE streaming, approvals), QQ / Feishu / DingTalk webhooks |
| **Storage** | SQLite for sessions, tasks and memory (FTS5 search); optional JSONL backend |
| **Observability** | JSON logs with trace/session ids, token accounting per model and session, tool latency metrics, `/api/metrics` (JSON + Prometheus text), durable task history |
| **Security** | Workspace path sandbox, policy engine (`allow`/`confirm`/`deny`), interactive approvals, command allow/deny lists, secret scrubbing, SSRF guard |
| **Ops** | Single process, Docker image, `anyaclaw doctor`, one-command rename for your own fork |

## Quick start

Requires Python 3.11+.

```bash
git clone https://github.com/kkdoa/Anyaclaw.git
cd anyaclaw
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[web,office]"

anyaclaw init                     # writes anyaclaw.toml, identity.md, skills/example
# edit anyaclaw.toml → provider.api_key = "${ANYACLAW_API_KEY}"
export ANYACLAW_API_KEY=sk-...    # Windows: set ANYACLAW_API_KEY=sk-...

anyaclaw doctor                   # checks config, paths and optional deps
anyaclaw chat                     # terminal REPL
anyaclaw serve                    # web console at http://127.0.0.1:8080
```

Minimal config:

```toml
[provider]
api_key = "${ANYACLAW_API_KEY}"
base_url = "https://api.siliconflow.cn/v1"
model = "deepseek-ai/DeepSeek-V3.2"

[web]
enabled = true
port = 8080
```

Every value can be overridden by environment variables using `ANYACLAW_SECTION__KEY`:

```bash
ANYACLAW_PROVIDER__MODEL=deepseek-chat ANYACLAW_WEB__PORT=9000 anyaclaw serve
```

### Try it offline (no API key)

A mock OpenAI-compatible server is included, so you can see the whole pipeline — streaming,
tool calls, sessions, metrics — without any credentials:

```bash
python examples/mock_openai_server.py                          # terminal 1: fake model
anyaclaw serve --config examples/config.mock.toml --no-cli     # terminal 2: UI on :8081
python scripts/smoke_test.py http://127.0.0.1:8081             # 20 end-to-end checks
```

Open <http://127.0.0.1:8081> and ask "please use the tool" to watch a tool call round-trip.

### Docker

```bash
docker build -t anyaclaw .
docker run --rm -p 8080:8080 \
  -e ANYACLAW_API_KEY=sk-... \
  -v "$PWD/data:/data" \
  anyaclaw
```

The container writes sessions, tasks and generated files under `/data`.

## Architecture

```
                       ┌──────────────────────────────────────────────┐
 CLI ─┐                │                  Runtime                     │
 Web ─┼─ inbound ──►  ┌▼──────────┐  ┌─────────────┐  ┌────────────┐  │
 QQ  ─┤   MessageBus  │  Gateway  │─►│  AgentLoop  │─►│  Provider  │──┼──► LLM API
 Feishu┤   (asyncio)   │ per-session│  │ tools ⇄ LLM │  │ (httpx SSE)│  │
 Ding ─┘               │  queues   │◄─│  events     │  └────────────┘  │
                       └┬──────────┘  └──────┬──────┘                  │
                        │ outbound           │ tool calls              │
                        ▼                    ▼                         │
                 Channel adapters      ToolRegistry ── fs / shell / web / docs
                        │                    │           memory / subagent / MCP
                        ▼                    ▼                         │
                  EventHub (SSE)        SQLite sessions · tasks · memory
```

* **Gateway** owns routing: every session gets a serial queue, cancellation targets the running
  turn, and adapter failures are isolated.
* **AgentLoop** is provider-agnostic and testable with a fake provider; it emits structured events
  (`thinking`, `content_delta`, `tool_call`, `tool_result`, `approval_request`, `turn_end`…).
* **Policy** decides per tool call whether to allow, ask or deny; the CLI prompts on the terminal,
  the web console shows an approval card, chat bots deny by default.
* **EventHub** fans events out to the browser over SSE without polling.

More detail: [docs/architecture.md](docs/architecture.md).

## Tools

| Tool | Read-only | Description |
|---|---|---|
| `read_file` / `write_file` / `edit_file` | mixed | Numbered reads, overwrite/append/create, exact-match edits |
| `list_dir` / `glob` / `grep` | ✅ | Directory and content search inside the workspace |
| `delete_path` | ❌ | Confirmation-gated deletion |
| `exec` | ❌ | Shell command with timeout, output cap, deny rules, scrubbed env, process-tree kill |
| `web_search` / `web_fetch` | ✅ | DuckDuckGo search; HTML→text fetch with SSRF protection |
| `remember` / `search_memory` / `forget` / `list_memory` | mixed | SQLite FTS5 long-term memory |
| `create_document` / `read_document` | mixed | Markdown-ish source → md/txt/html/docx/pptx/xlsx/pdf/csv, and back |
| `spawn_subagent` | ❌ | Delegates a focused subtask with its own context and budget |
| `workspace_info` / `system_info` | ✅ | Environment introspection |

Enable/disable groups in `[tools]`; MCP servers in `[tools.mcp_servers]`:

```toml
[tools.mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

## Security model

* All file tools resolve symlinks and refuse anything outside `security.workspace`.
* Mutating tools go through the policy engine; default `tool_policy = "confirm"`.
* `exec` runs with a scrubbed environment (API keys/tokens removed), a timeout that kills the
  process tree, and optional `allowed_commands` / `deny_patterns`.
* `web_fetch` refuses loopback/link-local/private addresses.
* The web console can be protected with `security.web_auth_token`.

Details and threat model: [docs/security.md](docs/security.md).

## Observability

```bash
curl -s localhost:8080/api/metrics | jq
curl -s "localhost:8080/api/metrics?format=prometheus"
```

* Every turn records prompt/completion/cached tokens per model and session.
* `logs` are JSON lines with `trace_id` and `session` so one conversation can be followed end to end.
* `/api/tasks` exposes durable task records (status, tool calls, event timeline).

## Development

```bash
pip install -e ".[dev]"
pytest -q            # 100+ tests, no network access required
ruff check src tests
```

Layout:

```
src/anyaclaw/
  core/        provider, agent loop, context, sessions, policy, memory, compaction
  tools/       built-in tools + registry factory
  channels/    base, CLI, Web (FastAPI + static UI), QQ, Feishu, DingTalk
  services/    task centre, generated-file workspace, knowledge base
  gateway.py   routing between channels and agents
  runtime.py   wiring
  cli.py       command-line entry point
tests/         pytest suite (config, loop, tools, web API, gateway)
docs/          architecture, configuration, security, improvements, roadmap
```

## Design notes

Anyaclaw is built around a gateway + channels + provider + agent loop architecture, with storage,
security, observability and packaging designed to survive daily use. Each decision is listed in
[docs/improvements.md](docs/improvements.md) together with the failure mode it prevents.

## Roadmap

See [docs/roadmap.md](docs/roadmap.md). Short version: desktop shell over the same runtime,
image/vision input, LanceDB-backed semantic memory, per-session cost budgets, and an extension API.

## Credits & license

MIT licensed. Built with [httpx](https://www.python-httpx.org/), [FastAPI](https://fastapi.tiangolo.com/)
and the Python standard library. See [CREDITS.md](CREDITS.md) for dependencies and inspiration notes.
