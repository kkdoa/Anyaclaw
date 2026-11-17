"""Typed exceptions shared across the runtime.

The rule of thumb: user/config errors are :class:`ConfigError`, upstream model
failures are :class:`ProviderError` (carrying a ``retryable`` flag), and any
misuse of a tool raises :class:`ToolError`, which the registry converts into a
``ToolResult(is_error=True)`` so the model can recover instead of crashing the
whole turn.
"""

from __future__ import annotations


class AnyaclawError(Exception):
    """Base class for all runtime errors."""


class ConfigError(AnyaclawError):
    """Raised when configuration is missing or malformed."""


class ProviderError(AnyaclawError):
    """Raised when an LLM request fails.

    Attributes:
        retryable: Whether an identical request may be retried safely.
        status_code: Upstream HTTP status when available.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ToolError(AnyaclawError):
    """Raised by tools for expected, recoverable failures."""


class PolicyDenied(ToolError):
    """Raised when a tool call is blocked by policy."""


class SessionError(AnyaclawError):
    """Raised for session persistence problems."""
