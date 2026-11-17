"""Shell tool: guards, exit codes, timeouts, command allow-listing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anyaclaw.core.errors import ToolError
from anyaclaw.tools.shell import ExecTool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


async def test_echo_command(workspace: Path) -> None:
    tool = ExecTool(workspace)
    result = await tool.execute(command=f'"{sys.executable}" -c "print(42)"')
    assert "exit_code: 0" in result.content
    assert "42" in result.content
    assert not result.is_error


async def test_non_zero_exit_marks_error(workspace: Path) -> None:
    tool = ExecTool(workspace)
    result = await tool.execute(command=f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    assert result.is_error
    assert "exit_code: 3" in result.content


async def test_deny_pattern_blocks(workspace: Path) -> None:
    tool = ExecTool(workspace, deny_patterns=["forbidden"])
    with pytest.raises(ToolError, match="deny pattern"):
        await tool.execute(command="echo forbidden")


async def test_allowed_commands(workspace: Path) -> None:
    tool = ExecTool(workspace, allowed_commands=["python"])
    result = await tool.execute(command=f'"{sys.executable}" -c "print(1)"')
    assert not result.is_error

    with pytest.raises(ToolError, match="not in security.allowed_commands"):
        await tool.execute(command="echo nope")


async def test_timeout_kills_process(workspace: Path) -> None:
    tool = ExecTool(workspace, default_timeout=0.4, max_timeout=5)
    result = await tool.execute(
        command=f'"{sys.executable}" -c "import time; time.sleep(10)"',
        timeout=0.4,
    )
    assert result.is_error
    assert "timed out" in result.content


async def test_output_is_truncated(workspace: Path) -> None:
    tool = ExecTool(workspace, max_output_chars=50)
    result = await tool.execute(command=f'"{sys.executable}" -c "print(\'x\' * 500)"')
    assert "truncated" in result.content


async def test_cwd_stays_in_workspace(workspace: Path) -> None:
    tool = ExecTool(workspace)
    with pytest.raises(ToolError, match="escapes the workspace"):
        await tool.execute(command="echo hi", cwd="..")


async def test_secrets_are_stripped(workspace: Path) -> None:
    import os

    os.environ["ANYACLAW_TEST_API_KEY"] = "sk-very-secret"
    try:
        tool = ExecTool(workspace)
        result = await tool.execute(
            command=f'"{sys.executable}" -c "import os; print(os.environ.get(\'ANYACLAW_TEST_API_KEY\', \'absent\'))"'
        )
        assert "absent" in result.content
        assert "sk-very-secret" not in result.content
    finally:
        os.environ.pop("ANYACLAW_TEST_API_KEY", None)
