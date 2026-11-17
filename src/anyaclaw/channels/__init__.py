"""Channels: terminal, web and messaging adapters."""

from __future__ import annotations

from anyaclaw.channels.base import Channel
from anyaclaw.channels.cli import CLIChannel

__all__ = ["Channel", "CLIChannel"]
