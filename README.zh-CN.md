# Anyaclaw

> 本地优先、多渠道的 AI Agent 运行时：真正的 Agent Loop、工具系统、MCP、Web 控制台与聊天机器人。

**简体中文** · [English](README.md)

[![CI](https://github.com/kkdoa/Anyaclaw/actions/workflows/ci.yml/badge.svg)](https://github.com/kkdoa/Anyaclaw/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Anyaclaw 运行在你自己的机器上，可接入任意 OpenAI 兼容模型服务，并通过终端、浏览器或聊天机器人与你交互。
它背后的设计取舍，以及这些取舍要解决的失败模式，都记录在 [docs/improvements.md](docs/improvements.md)。

**状态**：alpha。核心 Agent Loop、工具与 Web 控制台已稳定，覆盖 100+ 个测试；消息渠道适配器为 beta
（见 [docs/channels.md](docs/channels.md)）。

---

## 功能特性

| 模块 | 能力 |
|---|---|
| **Agent Loop** | 流式工具调用循环；迭代/工具预算、单次工具超时、重复调用熔断、只读工具并发、上下文压缩 |
| **模型接入** | 任意 OpenAI 兼容 `/chat/completions`（硅基流动、DeepSeek、OpenAI、Kimi、Qwen、vLLM、Ollama…）；带抖动的指数退避重试、支持 `Retry-After`、`reasoning_content` 透传 |
| **工具** | 文件（读写/编辑/glob/grep/删除）、受限 Shell、网页搜索与抓取、长期记忆、文档生成（docx/pptx/xlsx/pdf/csv/md/html）、子 Agent、MCP Server |
| **渠道** | CLI REPL、Web 控制台（SSE 流式、审批卡片）、QQ / 飞书 / 钉钉 Webhook |
| **存储** | SQLite 保存会话、任务与记忆（FTS5 检索）；可选 JSONL 后端 |
| **可观测性** | 带 trace/session id 的 JSON 日志、按模型与会话的 Token 统计、工具耗时指标、`/api/metrics`（JSON + Prometheus 文本）、可追溯的任务历史 |
| **安全** | 工作区路径沙箱、工具策略引擎（`allow`/`confirm`/`deny`）、交互式审批、命令黑白名单、密钥清洗、SSRF 防护 |
| **运维** | 单进程运行、Docker 镜像、`anyaclaw doctor`、一键改名为你自己的 fork |

## 快速开始

要求 Python 3.11+。

```bash
git clone https://github.com/kkdoa/Anyaclaw.git
cd anyaclaw
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[web,office]"

anyaclaw init                     # 生成 anyaclaw.toml、identity.md、skills/example
# 编辑 anyaclaw.toml：provider.api_key = "${ANYACLAW_API_KEY}"
export ANYACLAW_API_KEY=sk-...    # Windows: set ANYACLAW_API_KEY=sk-...

anyaclaw doctor                   # 检查配置、路径与可选依赖
anyaclaw chat                     # 终端对话
anyaclaw serve                    # Web 控制台 http://127.0.0.1:8080
```

最小配置：

```toml
[provider]
api_key = "${ANYACLAW_API_KEY}"
base_url = "https://api.siliconflow.cn/v1"
model = "deepseek-ai/DeepSeek-V3.2"

[web]
enabled = true
port = 8080
```

所有配置项都可以用环境变量覆盖，规则是 `ANYACLAW_<段>__<键>`：

```bash
ANYACLAW_PROVIDER__MODEL=deepseek-chat ANYACLAW_WEB__PORT=9000 anyaclaw serve
```

### 离线体验（无需 API Key）

仓库自带一个 OpenAI 兼容的 mock 服务，不配置任何凭据也能看到完整链路——流式输出、工具调用、
会话与指标：

```bash
python examples/mock_openai_server.py                          # 终端 1：假模型
anyaclaw serve --config examples/config.mock.toml --no-cli     # 终端 2：UI 在 8081
python scripts/smoke_test.py http://127.0.0.1:8081             # 20 项端到端检查
```

打开 <http://127.0.0.1:8081>，输入 "please use the tool"，即可看到一次工具调用往返。

### Docker

```bash
docker build -t anyaclaw .
docker run --rm -p 8080:8080 \
  -e ANYACLAW_API_KEY=sk-... \
  -v "$PWD/data:/data" \
  anyaclaw
```

容器会把会话、任务与生成的文件写入 `/data`。

## 架构

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

* **Gateway** 负责路由：每个会话一条串行队列，取消只作用于正在执行的那一轮，单个适配器故障被隔离。
* **AgentLoop** 与具体 Provider 无关，可用假 Provider 测试；它产出结构化事件
  （`thinking`、`content_delta`、`tool_call`、`tool_result`、`approval_request`、`turn_end`…）。
* **Policy** 对每次工具调用决定放行、询问还是拒绝；CLI 在终端询问，Web 控制台弹审批卡片，
  聊天机器人默认拒绝。
* **EventHub** 通过 SSE 把事件推给浏览器，无需轮询。

更多细节：[docs/architecture.md](docs/architecture.md)。

## 工具

| 工具 | 只读 | 说明 |
|---|---|---|
| `read_file` / `write_file` / `edit_file` | 混合 | 带行号读取、覆盖/追加/新建、精确匹配编辑 |
| `list_dir` / `glob` / `grep` | ✅ | 工作区内的目录与内容检索 |
| `delete_path` | ❌ | 需确认后删除 |
| `exec` | ❌ | Shell 命令，带超时、输出上限、拒绝规则、环境变量清洗、进程树清理 |
| `web_search` / `web_fetch` | ✅ | DuckDuckGo 搜索；HTML→文本抓取，带 SSRF 防护 |
| `remember` / `search_memory` / `forget` / `list_memory` | 混合 | SQLite FTS5 长期记忆 |
| `create_document` / `read_document` | 混合 | Markdown 风格源文本 → md/txt/html/docx/pptx/xlsx/pdf/csv，以及反向读取 |
| `spawn_subagent` | ❌ | 把聚焦的子任务交给带独立上下文与预算的子 Agent |
| `workspace_info` / `system_info` | ✅ | 环境自省 |

在 `[tools]` 中开关工具组；MCP Server 配置在 `[tools.mcp_servers]`：

```toml
[tools.mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

## 安全模型

* 所有文件工具都会解析软链接，并拒绝 `security.workspace` 之外的任何路径。
* 有副作用的工具都要经过策略引擎；默认 `tool_policy = "confirm"`。
* `exec` 在清洗过的环境中运行（移除 API Key/Token），超时会杀掉进程树，并可选
  `allowed_commands` / `deny_patterns`。
* `web_fetch` 拒绝回环、链路本地与私有地址。
* Web 控制台可用 `security.web_auth_token` 保护。

细节与威胁模型：[docs/security.md](docs/security.md)。

## 可观测性

```bash
curl -s localhost:8080/api/metrics | jq
curl -s "localhost:8080/api/metrics?format=prometheus"
```

* 每一轮都会记录按模型与会话维度的 prompt / completion / cached tokens。
* 日志是 JSON 行，带 `trace_id` 与 `session`，可以完整追踪一次对话。
* `/api/tasks` 暴露持久化任务记录（状态、工具调用、事件时间线）。

## 开发

```bash
pip install -e ".[dev]"
pytest -q            # 100+ 个测试，不需要联网
ruff check src tests
```

目录结构：

```
src/anyaclaw/
  core/        Provider、Agent Loop、上下文、会话、策略、记忆、压缩
  tools/       内置工具 + 注册表工厂
  channels/    base、CLI、Web（FastAPI + 静态 UI）、QQ、飞书、钉钉
  services/    任务中心、生成文件工作区、知识库
  gateway.py   渠道与 Agent 之间的路由
  runtime.py   组件装配
  cli.py       命令行入口
tests/         pytest 测试（配置、循环、工具、Web API、网关）
docs/          架构、配置、安全、改进说明、路线图
```

## 设计说明

Anyaclaw 采用「网关 + 渠道 + Provider + Agent Loop」架构，存储、安全、可观测性与打包方式都按
长期日常使用的标准设计。每一项决策及其要避免的失败模式，都列在
[docs/improvements.md](docs/improvements.md) 中。

## 路线图

见 [docs/roadmap.md](docs/roadmap.md)。简版：同一运行时上的桌面端外壳、图片/视觉输入、
基于 LanceDB 的语义记忆、按会话的成本预算，以及扩展 API。

## 更多文档

- [架构设计](docs/architecture.md)
- [设计说明与失败模式](docs/improvements.md)
- [配置项手册](docs/configuration.md)
- [安全模型](docs/security.md)
- [渠道说明](docs/channels.md)
- [路线图](docs/roadmap.md)

## 致谢与许可证

MIT 许可证。使用 [httpx](https://www.python-httpx.org/)、[FastAPI](https://fastapi.tiangolo.com/)
与 Python 标准库构建。依赖与灵感说明见 [CREDITS.md](CREDITS.md)。
