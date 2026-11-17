"""Anyaclaw — a local-first, multi-channel AI agent runtime.

Public entry points live in :mod:`anyaclaw.cli`. The package is organised as:

* :mod:`anyaclaw.core`      — provider abstraction, agent loop, sessions, policy
* :mod:`anyaclaw.tools`     — built-in tools (files, shell, web, office, MCP …)
* :mod:`anyaclaw.channels`  — CLI / HTTP-Web / QQ / Feishu / DingTalk adapters
* :mod:`anyaclaw.services`  — task centre, file workspace, memory/knowledge base
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
