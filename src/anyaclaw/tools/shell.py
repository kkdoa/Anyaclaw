"""Shell execution tool with guard rails.

Executing arbitrary commands is the most dangerous thing an agent can do, so the
defaults are conservative: commands run inside the workspace, environment
variables that look like secrets are stripped, output is capped, timeouts kill
the whole process tree, and the tool is marked non-readonly so the policy layer
asks for confirmation unless the user opts out.
"""

from __future__ import annotations

import asyncio
import contextlib
import locale
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool
from anyaclaw.tools.paths import human_size, resolve_workspace_path

_SECRET_HINTS = ("API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

DEFAULT_DENY_PATTERNS = (
    "rm -rf /",
    "rm -rf ~",
    "del /f /s /q c:\\",
    "format c:",
    "mkfs",
    ":(){",
    "shutdown",
    "reboot",
)


class ExecTool(Tool):
    name = "exec"
    readonly = False
    description = (
        "Run a shell command inside the workspace and return stdout/stderr. "
        "Destructive commands require user approval. Prefer dedicated tools "
        "(read_file, write_file, edit_file, glob, grep) when possible."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "cwd": {
                "type": "string",
                "description": "Working directory relative to the workspace (default '.')",
            },
            "timeout": {
                "type": "number",
                "description": "Seconds before the process tree is killed (default 120)",
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        workspace: str | Path,
        *,
        allowed_commands: Sequence[str] = (),
        deny_patterns: Sequence[str] = (),
        default_timeout: float = 120.0,
        max_timeout: float = 1800.0,
        max_output_chars: int = 20000,
    ) -> None:
        self.workspace = Path(workspace)
        self.allowed_commands = {_normalise_command(name) for name in allowed_commands}
        self.deny_patterns = tuple(DEFAULT_DENY_PATTERNS) + tuple(deny_patterns)
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_output_chars = max_output_chars

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = str(kwargs.get("command") or "").strip()
        if not command:
            raise ToolError("command must not be empty")

        self._guard(command)

        cwd = resolve_workspace_path(self.workspace, kwargs.get("cwd") or ".")
        if not cwd.is_dir():
            raise ToolError(f"cwd is not a directory: {kwargs.get('cwd')}")

        timeout = float(kwargs.get("timeout") or self.default_timeout)
        timeout = max(1.0, min(timeout, self.max_timeout))

        started = time.perf_counter()
        process = await self._spawn(command, cwd)
        stdout: bytes = b""
        stderr: bytes = b""
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            await _kill_tree(process)
            elapsed = time.perf_counter() - started
            return ToolResult.error(
                f"command timed out after {timeout:.0f}s (killed process tree)\n"
                f"partial output:\n{self._clip(_decode(stdout))}",
                meta={"timeout": True, "duration_s": round(elapsed, 2)},
            )
        except asyncio.CancelledError:
            await _kill_tree(process)
            raise

        elapsed = time.perf_counter() - started
        out = self._clip(_decode(stdout))
        err = self._clip(_decode(stderr))
        code = process.returncode or 0

        lines = [f"exit_code: {code}", f"duration: {elapsed:.2f}s"]
        if out:
            lines += ["", "stdout:", out]
        if err:
            lines += ["", "stderr:", err]
        if not out and not err:
            lines += ["", "(no output)"]
        result = ToolResult.ok(
            "\n".join(lines),
            meta={"exit_code": code, "duration_s": round(elapsed, 2)},
        )
        if code != 0:
            result.is_error = True
        return result

    # -- guards ------------------------------------------------------------
    def _guard(self, command: str) -> None:
        lowered = command.lower()
        for pattern in self.deny_patterns:
            if pattern and pattern.lower() in lowered:
                raise ToolError(f"command blocked by deny pattern: {pattern!r}")

        if self.allowed_commands:
            first = _first_token(command)
            if _normalise_command(first) not in self.allowed_commands:
                allowed = ", ".join(sorted(self.allowed_commands))
                raise ToolError(
                    f"command {first!r} is not in security.allowed_commands ({allowed})"
                )

    # -- process -----------------------------------------------------------
    async def _spawn(self, command: str, cwd: Path) -> asyncio.subprocess.Process:
        env = _sanitized_env()
        if os.name == "nt":
            # ``shell=True`` lets cmd.exe handle quoted executable paths that
            # contain spaces; passing a pre-quoted argv breaks them.
            return await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        return await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        head = text[: self.max_output_chars]
        return f"{head}\n…[truncated {human_size(len(text) - self.max_output_chars)} of output]"


def _normalise_command(token: str) -> str:
    name = Path(token.strip().strip("\"'")).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _first_token(command: str) -> str:
    """Return the executable token, handling quoted paths with spaces."""
    text = command.strip()
    while True:
        if text[:1] in {'"', "'"}:
            quote = text[0]
            end = text.find(quote, 1)
            token = text[1:end] if end > 0 else text[1:]
            rest = text[end + 1 :].strip() if end > 0 else ""
        else:
            parts = text.split(None, 1)
            token = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ""
        # Skip leading "VAR=value" environment assignments.
        if "=" in token and not token.startswith("=") and rest:
            head, _, _value = token.partition("=")
            if head.replace("_", "").isalnum():
                text = rest
                continue
        return token


def _sanitized_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if any(hint in key.upper() for hint in _SECRET_HINTS):
            env.pop(key, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _decode(data: bytes) -> str:
    if not data:
        return ""
    candidates = ["utf-8", locale.getpreferredencoding(False), "gbk", "cp1252"]
    seen: set[str] = set()
    for encoding in candidates:
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, ProcessLookupError):
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
