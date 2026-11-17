"""Tool implementations and the registry factory.

Importing this package must stay cheap and dependency-free: it is imported from
``anyaclaw.services`` (which is imported by the gateway), so heavy optional
imports live inside :mod:`anyaclaw.tools.builder` and :mod:`anyaclaw.tools.documents`.
"""

from __future__ import annotations

from anyaclaw.tools.base import FunctionTool, Tool

__all__ = ["Tool", "FunctionTool"]
