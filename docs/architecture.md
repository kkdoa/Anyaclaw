# Architecture

## Layers

```
channels/      adapters: CLI, Web (FastAPI+SSE), QQ, Feishu, DingTalk
bus.py         asyncio message bus + EventHub (fan-out to UIs)
gateway.py     routing, per-session queues, cancellation, task records
core/agent.py  the agent loop (provider ⇄ tools ⇄ session store)
core/*         provider, context, policy, memory, compaction, skills
tools/         tool implementations behind a single Tool interface
services/      task centre, generated-file workspace, knowledge base
config.py      one validated config object, env overrides
runtime.py     wiring; the only place that constructs everything
cli.py         argument parsing and process lifecycle
```

Dependencies point inwards: `channels → gateway → core → tools`, and `services` is used by both
`core` and `gateway`. Optional third-party packages are imported lazily so the core runs with only
`httpx`.

## One turn, end to end

```
channel ──publish_inbound──► MessageBus
                                │
                    Gateway._inbound_loop
                                │  commands (/clear, /cancel, /memory, …) handled inline
                                ▼
                    per-session asyncio.Queue ──► Gateway._process
                                                     │ create task record
                                                     ▼
                                              AgentLoop.run(message)
                                                     │
             ┌───────────────────────────────────────┤
             ▼                                       │
   ContextBuilder.build_messages                     │  history + memory + skills + identity
             │                                       │
             ▼                                       │
   provider.chat(stream)  ──deltas──► EventHub ──► SSE / CLI renderer
             │                                       │
             ├─ tool_calls ──► ToolPolicy ──► ToolRegistry.execute (timeout, metrics)
             │                     │                 │
             │                     └─ confirm ──► ApprovalBroker ──► web approval card / CLI prompt
             ▼                                       │
        final answer ──► SessionStore (SQLite) ──► OutboundMessage ──► channel.send
                                                     │
                                             TaskCenter status/events
```

Events emitted by the loop (`Event.kind`):

| Event | Payload |
|---|---|
| `turn_start` | `message` |
| `thinking` | `iteration` |
| `reasoning_delta` / `content_delta` | `content` |
| `tool_call` | `tool`, `arguments`, `call_id`, `readonly` |
| `tool_result` | `tool`, `call_id`, `content` (≤600 chars), `is_error`, `files`, `duration_ms` |
| `approval_request` / `approval_result` | `id`, `tool`, `arguments`, `reason` / `approved` |
| `model_response` | `usage`, `finish_reason`, `model`, character counts |
| `turn_end` | `content`, `files`, `iterations`, `tool_calls`, `usage`, `duration_ms` |
| `error` / `cancelled` | `message` / `reason` |

Note that the agent emits events **synchronously** into a sink; sinks must be non-blocking (they
typically push into an `asyncio.Queue`). This keeps streaming ordering deterministic and lets the
provider's delta callback run without awaiting.

## Key design decisions

**One authoritative config.** `load_config()` is called once at the entry point. `Runtime` receives
a `Config` and passes it down; the reference implementation re-read the file inside the agent
factory, which silently ignored injected configs and made tests impossible.

**Typed tool results instead of magic strings.** Tools return `ToolResult(content, files, is_error)`.
Chat attachments are the structured `files` list, not `[[FILE:...]]` markers scraped out of the
answer text.

**Serial queue per session.** Two messages in a row are queued and answered in order, with a notice
to the user. Cancellation cancels the *turn*, not the worker, so the session survives `/cancel`.

**Policy separate from execution.** `ToolPolicy` maps a call to `allow`/`confirm`/`deny`, and
`ApprovalBroker` turns "confirm" into a real human interaction. Chat bots default to *deny* rather
than silently approving.

**SQLite for everything durable.** Sessions, tasks and memory are separate SQLite databases in
`<data_dir>` with WAL enabled. Sessions are replayable transcripts; tasks are an audit trail;
memory uses FTS5 with a trigram tokenizer so Chinese search works.

**Streaming is a provider concern, not a loop concern.** The provider assembles the full response
while forwarding deltas. Retries are only allowed before the first delta, so a retry can never
duplicate text the user already saw.

## Extension points

| Want to… | Do this |
|---|---|
| Add a tool | Subclass `anyaclaw.tools.base.Tool`, register it in `tools/builder.py` |
| Add a model provider | Implement `anyaclaw.core.provider.LLMProvider` |
| Add a chat platform | Implement `anyaclaw.channels.base.Channel`; optionally expose `router()` for webhooks |
| Replace storage | Implement `SessionStore` (see `core/session.py`) |
| Observe events | Subscribe to `runtime.hub`, or pass a custom `event_sink` to `AgentLoop` |
| Build a desktop shell | Use `Runtime` + `Gateway.run_turn(sink=...)`; the Qt shell in the original project is intentionally not bundled (see below) |

### Desktop shell contract

The original project shipped a 105 KB PySide6 file. Anyaclaw keeps the runtime UI-agnostic so a
desktop shell can be added without touching the core:

```python
runtime = Runtime(config)
await runtime.setup()
gateway = runtime.ensure_gateway()

def loginctl_sink(event):          # called from the agent thread
    queue.put_nowait(event.to_dict())

result, task_id = await gateway.run_turn(
    session_key="desktop:local", channel="desktop", content=text, sink=sink
)
```

`GET /api/sessions`, `/api/tasks`, `/api/tools` and `/api/metrics` give a shell everything it needs
for sidebars and status widgets; `runtime.approvals` powers approval dialogs.

## Failure behaviour

* Provider errors surface as `AgentResult(error=...)` and one `error` event; the turn ends but the
  session stays usable.
* A crashing tool becomes `ToolResult(is_error=True)` with the exception message — the model can
  recover instead of the process dying.
* A channel that fails to start is logged and skipped; the gateway keeps serving other channels.
* On shutdown, in-flight turns are cancelled and stores are closed; SQLite WAL keeps data coherent.
