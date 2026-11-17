"""Command-line entry point: ``anyaclaw {init,chat,serve,tools,doctor,version}``."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path

from anyaclaw import __version__
from anyaclaw.config import (
    Config,
    ConfigError,
    load_config,
    write_default_config,
)
from anyaclaw.logging import configure_logging

IDENTITY_SAMPLE = """# Anyaclaw 鈥?identity

You are **Anyaclaw**, a local-first assistant that gets things done with tools.

## Style
- Answer in the user's language, concisely and concretely.
- Show your reasoning only when it helps; never pad answers.
- Prefer acting (reading files, running searches) over guessing.

## Boundaries
- Never run destructive commands without explaining what will change.
- Keep files inside the workspace; ask before touching anything else.
- When you are unsure, say so and propose the smallest next step.
"""

SKILL_SAMPLE = """---
name: example
description: A template skill 鈥?copy this folder and rewrite it for your own workflow.
when_to_use: The user asks how to add a new skill to Anyaclaw.
---

# Example skill

Skills are plain Markdown files at `skills/<name>/SKILL.md` with optional
front matter (`name`, `description`, `when_to_use`).

Only the summary is loaded into the system prompt; the body is read on demand
with the `read_file` tool, so skills can be long without costing tokens.

## Steps

1. Create `skills/<name>/SKILL.md`.
2. Describe when to use it in `when_to_use`.
3. Start a conversation and ask for the task 鈥?Anyaclaw will read this file.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anyaclaw",
        description="Local-first multi-channel AI agent runtime",
    )
    parser.add_argument("--config", "-c", help="path to anyaclaw.toml/json")
    parser.add_argument("--version", action="version", version=f"anyaclaw {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # Allow ``--config`` both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    init = sub.add_parser("init", help="create a starter config and identity file")
    init.add_argument("--path", default="anyaclaw.toml", help="where to write the config")
    init.add_argument("--force", action="store_true", help="overwrite existing files")

    chat = sub.add_parser("chat", parents=[common], help="interactive terminal chat")
    chat.add_argument("--session", default="cli:local", help="session key to resume")

    serve = sub.add_parser("serve", parents=[common], help="run the web UI and enabled chat channels")
    serve.add_argument("--host", help="override web.host")
    serve.add_argument("--port", type=int, help="override web.port")
    serve.add_argument("--no-web", action="store_true", help="disable the web UI")
    serve.add_argument("--no-cli", action="store_true", help="disable the terminal REPL")

    sub.add_parser("tools", parents=[common], help="list registered tools")
    sub.add_parser("doctor", parents=[common], help="check configuration and optional dependencies")
    sub.add_parser("version", help="print the version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"anyaclaw {__version__}")
        return 0

    if args.command == "init":
        return _cmd_init(args)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return _cmd_doctor(config)

    if args.command == "tools":
        return _cmd_tools(config)

    try:
        config.require_api_key()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "chat":
        return _cmd_chat(config, args)
    if args.command == "serve":
        return _cmd_serve(config, args)
    parser.error(f"unknown command: {args.command}")
    return 2


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    try:
        path = write_default_config(target, overwrite=args.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"created {path}")
    base = path.parent
    _write_sample(base / "identity.md", IDENTITY_SAMPLE, force=args.force)
    _write_sample(base / "skills" / "example" / "SKILL.md", SKILL_SAMPLE, force=args.force)
    print(
        "\nnext steps:\n"
        "  1. set provider.api_key in the config (or export ANYACLAW_PROVIDER__API_KEY)\n"
        "  2. run `anyaclaw doctor`\n"
        "  3. run `anyaclaw chat` or `anyaclaw serve`"
    )
    return 0


def _write_sample(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        print(f"kept existing {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"created {path}")


def _cmd_chat(config: Config, args: argparse.Namespace) -> int:
    configure_logging(config.log_level, json_output=False)
    config.web.enabled = False
    config.channels.qq.enabled = False
    config.channels.feishu.enabled = False
    config.channels.dingtalk.enabled = False
    config.channels.cli.enabled = True

    from anyaclaw.runtime import Runtime

    runtime = Runtime(config)
    try:
        asyncio.run(runtime.run(only={"cli"}))
    except KeyboardInterrupt:
        print("\nbye")
    return 0


def _cmd_serve(config: Config, args: argparse.Namespace) -> int:
    configure_logging(config.log_level, json_output=config.log_json)
    if args.host:
        config.web.host = args.host
    if args.port:
        config.web.port = args.port
    if args.no_web:
        config.web.enabled = False
    if args.no_cli:
        config.channels.cli.enabled = False

    from anyaclaw.runtime import Runtime

    runtime = Runtime(config)
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        print("\nbye")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_tools(config: Config) -> int:
    from anyaclaw.metrics import Metrics
    from anyaclaw.services.workspace import FileWorkspace
    from anyaclaw.tools.builder import build_tool_registry

    workspace = FileWorkspace(config.files_dir)
    registry = build_tool_registry(
        config=config,
        memory=_NullMemory(),  # type: ignore[arg-type]
        workspace=workspace,
        metrics=Metrics(),
        provider_factory=None,
    )
    print(f"{len(registry)} tools")
    for tool in registry:
        mode = "read-only" if getattr(tool, "readonly", False) else "mutating"
        print(f"  {tool.name:<18} {mode:<10} {tool.description.splitlines()[0][:80]}")
    return 0


def _cmd_doctor(config: Config) -> int:
    print(f"anyaclaw {__version__}")
    print(f"config: {config.config_path or '(defaults, no config file found)'}")
    print(f"provider: {config.provider.model} @ {config.provider.base_url}")
    print(f"api key: {'set' if config.provider.api_key else 'MISSING'}")

    problems: list[str] = []
    warnings: list[str] = []

    if not config.provider.api_key:
        problems.append("provider.api_key is empty")

    for label, path in (
        ("workspace", config.workspace),
        ("data dir", config.data_dir),
        ("files dir", config.files_dir),
    ):
        status = _check_writable(path)
        print(f"{label}: {path} [{status}]")
        if status != "ok":
            problems.append(f"{label} is not writable: {path}")

    print("dependencies:")
    for module, label, required in (
        ("httpx", "httpx (required)", True),
        ("fastapi", "fastapi (web)", False),
        ("uvicorn", "uvicorn (web)", False),
        ("docx", "python-docx (docx)", False),
        ("pptx", "python-pptx (pptx)", False),
        ("openpyxl", "openpyxl (xlsx)", False),
        ("reportlab", "reportlab (pdf)", False),
        ("pypdf", "pypdf (pdf reader)", False),
        ("mcp", "mcp (MCP servers)", False),
    ):
        available = importlib.util.find_spec(module) is not None
        mark = "ok" if available else ("MISSING" if required else "optional, missing")
        print(f"  {label:<26} {mark}")
        if required and not available:
            problems.append(f"{module} is required: pip install anyaclaw")
        if not required and not available and module != "mcp":
            warnings.append(f"{label} unavailable")

    if config.tools.mcp_servers:
        print(f"mcp servers: {len(config.tools.mcp_servers)} configured")
    channels = [
        name
        for name in ("cli", "web", "qq", "feishu", "dingtalk")
        if (
            getattr(config.channels, name, None).enabled
            if hasattr(config.channels, name)
            else config.web.enabled
        )
    ]
    print(f"channels: {', '.join(channels) or 'none'}")
    print(f"tool policy: {config.security.tool_policy} (auto_approve={config.security.auto_approve})")

    if warnings:
        print("\nwarnings:")
        for item in warnings:
            print(f"  - {item}")
    if problems:
        print("\nproblems:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\nall checks passed")
    return 0


def _check_writable(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".anyaclaw-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "ok"
    except OSError as exc:
        return f"error: {exc}"


class _NullMemory:
    """Placeholder used by ``anyaclaw tools`` so no database file is created."""

    async def search(self, query: str, limit: int = 5):  # noqa: ANN201
        return []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
