# Configuration reference

All settings live in `anyaclaw.toml` (or `config.toml` / `.json`). Every value can be overridden
with an environment variable named `ANYACLAW_<SECTION>__<KEY>` (two underscores between the section
and the key), for example `ANYACLAW_WEB__PORT=9000`. Locate the file explicitly with
`--config PATH` or `ANYACLAW_CONFIG=path`.

Secrets may be indirected with `env:NAME` or `${NAME}`:

```toml
[provider]
api_key = "${ANYACLAW_API_KEY}"
```

Unknown keys are rejected with the full dotted path, so typos do not silently fall back to defaults.

## `[provider]`

| Key | Default | Description |
|---|---|---|
| `api_key` | `""` | Bearer token. Falls back to `ANYACLAW_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `SILICONFLOW_API_KEY` |
| `base_url` | `https://api.siliconflow.cn/v1` | Any OpenAI-compatible base URL (the client appends `/chat/completions`) |
| `model` | `deepseek-ai/DeepSeek-V3.2` | Main model |
| `subagent_model` | `""` | Model for `spawn_subagent`; empty reuses a fresh client with `model` |
| `cheap_model` | `""` | Model used for context compaction summaries |
| `temperature` | `0.7` | Omit with `temperature = null` to use the server default |
| `top_p` | `null` | Optional |
| `max_tokens` | `null` | Optional output cap |
| `timeout` | `120.0` | Read/write timeout in seconds |
| `connect_timeout` | `15.0` | Connection timeout |
| `max_retries` | `3` | Retries for 408/409/425/429/5xx, timeouts and connection errors |
| `retry_backoff` | `0.8` | Base for exponential back-off (jitter added, capped at 30 s) |
| `thinking` | `true` | Send thinking controls for models that support them |
| `reasoning_effort` | `""` | Passed through when set (`low`/`medium`/`high`) |
| `reasoning_budget` | `0` | SiliconFlow `thinking_budget` when non-zero |
| `stream_usage` | `true` | Request `stream_options.include_usage`; disable for gateways that reject it |
| `send_reasoning` | `false` | Replay `reasoning_content` back to the model on later turns |
| `extra_headers` | `{}` | Extra HTTP headers |
| `extra_body` | `{}` | Extra JSON fields merged into the request body |

## `[agent]`

| Key | Default | Description |
|---|---|---|
| `max_iterations` | `32` | Maximum model calls per turn |
| `max_tool_calls` | `64` | Maximum tool calls per turn |
| `tool_timeout` | `300.0` | Seconds per tool call |
| `max_history_messages` | `80` | Transcript window replayed from storage |
| `context_token_budget` | `24000` | Estimated prompt budget before trimming/compaction |
| `summarize` | `true` | Summarise older turns with `cheap_model` when over budget |
| `identity_file` | `identity.md` | Persona file, relative to the workspace |
| `skills_dir` | `skills` | Directory of `*/SKILL.md` files |
| `loop_warn_after` | `4` | Identical-call count that triggers a warning tool result |
| `loop_abort_after` | `10` | Identical-call count that aborts the turn |
| `loop_window` | `40` | Size of the recent-call window used for loop detection |
| `max_tool_output_chars` | `20000` | Tool output truncation limit |
| `parallel_tool_calls` | `true` | Run independent read-only calls concurrently |
| `show_reasoning` | `true` | Render reasoning deltas in the CLI |
| `send_reasoning` | `false` | Persist `reasoning_content` in the transcript sent back to the model |
| `stream` | `true` | Use streaming completions |

## `[security]`

| Key | Default | Description |
|---|---|---|
| `workspace` | `"."` | Hard sandbox for file tools; relative to the config file |
| `tool_policy` | `"confirm"` | Default for mutating tools: `allow`, `confirm` or `deny` |
| `tool_policies` | `{}` | Per-tool overrides, e.g. `{ exec = "allow" }` |
| `auto_approve` | `false` | Skip confirmations entirely (not recommended) |
| `shell_enabled` | `true` | Register the `exec` tool |
| `allowed_commands` | `[]` | If non-empty, only these executables may run |
| `deny_patterns` | `[]` | Substring rules that block a command (destructive defaults are always added) |
| `enforce_sender_whitelist` | `false` | Only allow listed sender ids |
| `allowed_senders` | `{}` | Per-channel allow-list, e.g. `{ qq = ["12345"] }`; `"*"` allows everyone |
| `max_file_bytes` | `5000000` | Read/write/upload cap |
| `web_auth_token` | `""` | If set, `/api/*` (except `/api/health`) requires `Authorization: Bearer <token>` |

## `[storage]`

| Key | Default | Description |
|---|---|---|
| `data_dir` | `.anyaclaw` | Sessions, tasks, memory and generated files |
| `session_backend` | `sqlite` | `sqlite` or `jsonl` |

`<data_dir>/anyaclaw.db` (sessions), `tasks.db`, `memory.db`, `files/`.

## `[web]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Serve the console |
| `host` | `127.0.0.1` | Bind address (use `0.0.0.0` in Docker) |
| `port` | `8080` | TCP port |
| `cors_origins` | `[]` | Allowed origins for browser calls |
| `sse_heartbeat` | `15.0` | SSE keep-alive interval |
| `chat_timeout` | `1800.0` | Maximum duration of one web turn |

## `[channels.cli]`

| Key | Default |
|---|---|
| `enabled` | `true` |
| `prompt` | `"you"` |

## `[channels.qq]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | |
| `app_id` / `app_secret` | `""` | QQ bot credentials (`bots.qq.com/app/getAppAccessToken`) |
| `token` | `""` | Reserved |
| `sandbox` | `false` | Use the sandbox API host |
| `webhook_secret` | `""` | Optional shared token checked by the webhook handler |
| `default_target` | `""` | Fallback target, e.g. `channel:123` |

## `[channels.feishu]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | |
| `app_id` / `app_secret` | `""` | Feishu app credentials |
| `verification_token` | `""` | Checked against `header.token` on webhook events |
| `encrypt_key` | `""` | Reserved for encrypted events (not implemented) |
| `default_target` | `""` | e.g. `chat:oc_xxx` |

## `[channels.dingtalk]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | |
| `client_id` / `client_secret` | `""` | Enables Stream mode when `dingtalk-stream` is installed |
| `webhook_url` | `""` | Group robot webhook used for replies |
| `webhook_keyword` | `""` | Keyword prepended when the robot requires one |
| `default_target` | `""` | Reserved |

## `[tools]`

| Key | Default | Description |
|---|---|---|
| `files` | `true` | File tools |
| `shell` | `true` | Exec tool (also requires `security.shell_enabled`) |
| `web` | `true` | Search/fetch tools |
| `memory` | `true` | Memory tools |
| `subagent` | `true` | Subagent tool |
| `documents` | `true` | Office/PDF tools |
| `mcp` | `true` | Connect configured MCP servers |
| `mcp_servers` | `{}` | `{name = { command, args, env }}` |
| `user_agent` | `Anyaclaw/0.1 (+…)` | Sent with web requests |
| `max_web_chars` | `40000` | Fetch result cap |

## Top-level

| Key | Default | Description |
|---|---|---|
| `log_level` | `INFO` | Root log level |
| `log_json` | `true` | JSON logs to stderr; `false` for compact text |

## Examples

```toml
[provider]
api_key = "${ANYACLAW_API_KEY}"
base_url = "http://127.0.0.1:11434/v1"   # Ollama
model = "qwen2.5:14b"
temperature = 0.2
max_retries = 2
stream_usage = false                     # Ollama does not send usage in streams

[agent]
max_iterations = 16
tool_timeout = 60

[security]
tool_policy = "confirm"
tool_policies = { exec = "confirm", web_fetch = "allow" }
allowed_commands = ["git", "python", "pytest", "rg"]
enforce_sender_whitelist = true
allowed_senders = { qq = ["12345678"] }

[web]
host = "0.0.0.0"
web_auth_token = "${ANYACLAW_WEB_TOKEN}"
```
