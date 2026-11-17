"""Channel adapter interface.

A channel only has to do three things: start listening, stop cleanly and deliver an
:class:`~anyaclaw.bus.OutboundMessage`. Everything else (routing, agents, tools,
tasks, metrics) is shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from anyaclaw.bus import OutboundMessage


class Channel(ABC):
    """Base class for CLI, Web and messaging adapters."""

    name: str = ""

    @abstractmethod
    async def start(self) -> None:
        """Begin receiving messages. Should block until the channel stops."""

    async def stop(self) -> None:
        """Release resources. Must be idempotent."""
        return None

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """Deliver a reply to the user."""
