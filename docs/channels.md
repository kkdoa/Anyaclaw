# Channels

Everything below shares one runtime: the same agent, tools, sessions, task records and approval
policy. A message from QQ and a message from the browser differ only in the adapter.

## CLI

```bash
anyaclaw chat                 # resume session cli:local
anyaclaw serve                # terminal REPL + web console together
```

Commands: `/help`, `/clear`, `/tools`, `/tasks`, `/task <id>`, `/status`, `/cancel`, `/memory`,
`/remember <text>`, `/forget <text>`, `/exit`.

Streaming output shows reasoning (dim), tool calls and the final answer. `Ctrl+C` exits cleanly.

## Web console

`anyaclaw serve` → <http://127.0.0.1:8080>.

The UI is plain HTML/CSS/ES2020 served from `src/anyaclaw/channels/static/` — no build step, no CDN,
works offline. It supports session switching, streaming Markdown, collapsible tool cards, approval
cards, attachment upload/download, cancellation and a metrics drawer.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness, version, model |
| GET | `/api/config` | Masked configuration |
| GET | `/api/tools` | Tool list (+ MCP status) |
| GET | `/api/sessions` | Session list |
| GET | `/api/sessions/{key}/messages` | Transcript |
| DELETE | `/api/sessions/{key}` | Delete a session |
| POST | `/api/chat` | `{session_key, message}` → SSE event stream |
| POST | `/api/chat/{key}/cancel` | Cancel the running turn |
| GET | `/api/approvals` | Pending confirmations |
| POST | `/api/approvals/{id}` | `{approved: bool}` |
| GET | `/api/tasks` | Task history |
| GET | `/api/tasks/{id}` | Task detail with event timeline |
| GET | `/api/metrics` | Metrics JSON (`?format=prometheus` for text) |
| GET | `/api/events` | Global SSE monitor feed |
| GET/POST | `/api/files` | List / upload (multipart field `files`) |
| GET | `/api/files/{name}` | Download (confined to the files dir) |

SSE frames are `data: {json}` lines terminated by a blank line and `data: [DONE]`. Event types are
documented in [architecture.md](architecture.md#one-turn-end-to-end).

## QQ (official bot platform)

Send path: `bots.qq.com/app/getAppAccessToken` → `api.sgroup.qq.com/channels/{id}/messages`
(or `/v2/users/{openid}/messages`). Target format: `channel:<id>`, `group:<openid>`, `user:<openid>`.

Inbound: point the bot's webhook at `https://<host>/api/channels/qq/webhook`. Set
`channels.qq.webhook_secret` and configure the same token in your gateway if you need a shared-secret
check. Full Ed25519 verification is intentionally left to a proxy or bot gateway.

```toml
[channels.qq]
enabled = true
app_id = "${QQ_APP_ID}"
app_secret = "${QQ_APP_SECRET}"
default_target = "channel:123456"
```

## Feishu / Lark

Send path: tenant access token + `im/v1/messages`. Target format: `chat:oc_xxx` or `open:ou_xxx`.

Inbound: webhook at `/api/channels/feishu/webhook`. Plaintext `im.message.receive_v1` events are
supported; set `verification_token`. URL verification challenges are answered automatically.
Encrypted events (`encrypt`) are rejected — disable encryption or bridge with the official SDK.

```toml
[channels.feishu]
enabled = true
app_id = "cli_xxx"
app_secret = "xxx"
verification_token = "xxx"
default_target = "chat:oc_xxx"
```

## DingTalk

Replies go through a group-robot webhook (keyword optional). Inbound requires Stream mode and the
optional SDK:

```bash
pip install "anyaclaw[dingtalk]"   # or: pip install dingtalk-stream
```

Without it the channel starts in send-only mode and logs a warning.

```toml
[channels.dingtalk]
enabled = true
webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=xxx"
webhook_keyword = "claw"
```

## Writing an adapter

```python
from anyaclaw.channels.base import Channel
from anyaclaw.bus import InboundMessage, OutboundMessage

class MyChannel(Channel):
    name = "mychat"

    async def start(self) -> None:      # block until stopped
        ...

    async def send(self, message: OutboundMessage) -> None:
        await my_api.post(message.chat_id, message.content, files=message.files)

    # publish inbound messages like this:
    # await bus.publish_inbound(InboundMessage(channel=self.name,
    #     sender_id=user_id, chat_id=chat_id, content=text))
```

Register it in `Runtime._build_channels()`. If the platform pushes events over HTTP, also implement
`router()` returning a `fastapi.APIRouter`; the web app mounts it automatically.

## Attachments

Tools that produce files return them in `ToolResult.files`. They are stored in `<data_dir>/files`,
attached to replies where the platform supports uploads, and always downloadable from
`/api/files/{name}`. Chat adapters that cannot upload reply with a listing instead of silently
dropping them.
