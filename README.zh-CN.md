# Anyaclaw（中文）

> 本地优先、多渠道的 AI Agent 运行时：真正的 Agent Loop、工具系统、MCP、Web 控制台与聊天机器人。

Anyaclaw 运行在你自己的机器上，可接入任意 OpenAI 兼容模型服务，并通过终端、浏览器或聊天机器人
（QQ / 飞书 / 钉钉）与你交互。设计取舍与所针对的失败模式见 [docs/improvements.md](docs/improvements.md)。

**状态**：alpha。核心 Agent Loop、工具与 Web 控制台已稳定，100+ 测试覆盖；消息渠道适配器为 beta。

## 功能一览

- **Agent Loop**：流式工具调用循环，迭代/工具预算、单次工具超时、重复调用熔断、只读工具并发、上下文压缩。
- **模型接入**：任意 OpenAI 兼容 `/chat/completions`（硅基流动、DeepSeek、OpenAI、Kimi、Qwen、vLLM、Ollama…），
  带指数退避重试、`Retry-After` 支持、`reasoning_content` 透传。
- **工具**：文件读写编辑、glob/grep、受限 Shell、网页搜索与抓取、长期记忆、文档生成（docx/pptx/xlsx/pdf/csv/md/html）、
  子 Agent、MCP Server。
- **渠道**：CLI REPL、Web 控制台（SSE 流式 + 审批卡片）、QQ / 飞书 / 钉钉 Webhook。
- **存储**：SQLite 保存会话、任务与记忆（FTS5 检索），可选 JSONL 历史。
- **可观测性**：带 trace/session 的 JSON 日志、按模型与会话的 Token 统计、工具耗时指标、
  `/api/metrics`（JSON + Prometheus）、可追溯的任务中心。
- **安全**：工作区路径沙箱、工具策略（allow/confirm/deny）、交互式审批、命令黑白名单、密钥清洗、SSRF 防护。

## 快速开始

要求 Python 3.11+。

```bash
git clone https://github.com/kkdoa/Anyaclaw.git
cd anyaclaw
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[web,office]"

anyaclaw init            # 生成 anyaclaw.toml、identity.md、skills/example
# 编辑 anyaclaw.toml：provider.api_key = "${ANYACLAW_API_KEY}"
export ANYACLAW_API_KEY=sk-...     # Windows: set ANYACLAW_API_KEY=sk-...

anyaclaw doctor          # 检查配置、路径与可选依赖
anyaclaw chat            # 终端对话
anyaclaw serve           # Web 控制台 http://127.0.0.1:8080
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

所有配置项都可用环境变量覆盖，规则是 `ANYACLAW_<段>__<键>`：

```bash
ANYACLAW_PROVIDER__MODEL=deepseek-chat ANYACLAW_WEB__PORT=9000 anyaclaw serve
```

### 离线体验（无需 API Key）

仓库自带一个 OpenAI 兼容的 mock 服务，可完整体验流式输出、工具调用、会话与指标：

```bash
python examples/mock_openai_server.py                          # 终端 1：假模型
anyaclaw serve --config examples/config.mock.toml --no-cli     # 终端 2：UI 在 8081
python scripts/smoke_test.py http://127.0.0.1:8081             # 20 项端到端检查
```

打开 <http://127.0.0.1:8081>，输入“please use the tool”即可看到工具调用往返。

### Docker

```bash
docker build -t anyaclaw .
docker run --rm -p 8080:8080 -e ANYACLAW_API_KEY=sk-... -v "$PWD/data:/data" anyaclaw
```

会话、任务与生成的文件都会写入 `/data`。

## 设计说明

Anyaclaw 采用「网关 + 渠道 + Provider + Agent Loop」架构，存储、安全、可观测性与打包方式都按
长期日常使用的标准设计。每一项取舍及其要避免的失败模式见 [docs/improvements.md](docs/improvements.md)。

## 更多文档

- [架构设计](docs/architecture.md)
- [为什么要重写 / 修复了什么](docs/improvements.md)
- [配置项手册](docs/configuration.md)
- [安全模型](docs/security.md)
- [渠道说明](docs/channels.md)
- [路线图](docs/roadmap.md)

## 开发

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

## 许可证

MIT。见 [LICENSE](LICENSE) 与 [CREDITS.md](CREDITS.md)。
