# Contributing

Thanks for taking the time to help. This project optimises for a small core, clear errors and
tests for every behaviour change.

## Setup

```bash
git clone https://github.com/kkdoa/Anyaclaw.git
cd anyaclaw
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,web,office]"
pytest -q
ruff check src tests
```

## Ground rules

1. **Keep the core dependency-free.** `anyaclaw.core`, `anyaclaw.tools` (except documents/MCP) and
   `anyaclaw.gateway` must import with only `httpx` + stdlib. Optional features use lazy imports.
2. **No silent side effects.** Any tool that mutates state must be `readonly = False` and go through
   the policy layer.
3. **Errors are data.** Tool failures return `ToolResult.error(...)`; provider failures raise
   `ProviderError`; never `print()` an error and continue as if it succeeded.
4. **Tests first for bugs.** Add a failing test that reproduces the issue, then fix it.
5. **No new magic strings on the wire.** Extend `ToolResult`/`Event` rather than embedding markers in
   message text.

## Adding a tool

```python
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool
from anyaclaw.tools.paths import resolve_workspace_path

class WordCountTool(Tool):
    name = "word_count"
    readonly = True
    description = "Count words in a workspace file."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, workspace): self.workspace = workspace

    async def execute(self, **kwargs):
        path = resolve_workspace_path(self.workspace, kwargs["path"], must_exist=True)
        return ToolResult.ok(f"{len(path.read_text(encoding='utf-8').split())} words")
```

Register it in `src/anyaclaw/tools/builder.py`, add a test in `tests/`, and mention it in the README
tool table.

## Adding a channel

Implement `anyaclaw.channels.base.Channel` (`start`, `stop`, `send`) and, for webhook platforms, a
`router()` returning a FastAPI `APIRouter`. Wire it in `Runtime._build_channels()` and document the
setup in `docs/channels.md`. Adapters must not import heavy SDKs at module import time.

## Adding a provider

Implement `anyaclaw.core.provider.LLMProvider`. The agent only relies on:

* `chat(messages, tools, model=..., on_delta=...)` returning `LLMResponse`;
* raising `ProviderError(retryable=...)` on failure;
* forwarding `reasoning` and `content` deltas through `on_delta`.

## Pull requests

* One logical change per PR; include motivation and the behaviour before/after.
* Update `CHANGELOG.md` under "Unreleased".
* CI must be green (`ruff`, `pytest` on 3.11–3.13).
* Screenshots are welcome for UI changes.

## Commit messages

Conventional-ish: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`. Explain *why* in the body.

## Code of conduct

Be direct, be kind, assume good faith. Harassment or personal attacks are not welcome.
