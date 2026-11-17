"""In-process metrics: token usage, tool latency and counters.

Deliberately dependency-free: a handful of dictionaries behind a lock is enough
for a single-node assistant, and the snapshot can be scraped as JSON
(``/api/metrics``) or Prometheus text (``/api/metrics?format=prometheus``).

The original project had no accounting at all — you could not answer "how many
tokens did yesterday cost?". That is a bug for anyone running this locally with
a paid API, so usage is recorded per model and per session.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from anyaclaw.core.types import Usage


@dataclass(slots=True)
class ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def add(self, usage: Usage) -> None:
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.cached_tokens += usage.cached_tokens


@dataclass(slots=True)
class Latency:
    count: int = 0
    total: float = 0.0
    worst: float = 0.0

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.worst = max(self.worst, seconds)

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


@dataclass(slots=True)
class Metrics:
    """Thread-safe metric registry."""

    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[str, float] = field(default_factory=dict, repr=False)
    _models: dict[str, ModelUsage] = field(default_factory=dict, repr=False)
    _sessions: dict[str, ModelUsage] = field(default_factory=dict, repr=False)
    _latencies: dict[str, Latency] = field(default_factory=dict, repr=False)

    # -- recording ---------------------------------------------------------
    def incr(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = _label_key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, seconds: float, **labels: Any) -> None:
        key = _label_key(name, labels)
        with self._lock:
            self._latencies.setdefault(key, Latency()).observe(seconds)

    def record_usage(self, model: str, usage: Usage, *, session_key: str = "") -> None:
        if not (usage.total_tokens or usage.prompt_tokens or usage.completion_tokens):
            return
        with self._lock:
            self._models.setdefault(model or "unknown", ModelUsage()).add(usage)
            if session_key:
                self._sessions.setdefault(session_key, ModelUsage()).add(usage)
            self._counters["tokens_total"] = self._counters.get("tokens_total", 0.0) + (
                usage.total_tokens or usage.prompt_tokens + usage.completion_tokens
            )

    # -- read --------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "counters": dict(sorted(self._counters.items())),
                "models": {
                    model: _usage_dict(usage) for model, usage in sorted(self._models.items())
                },
                "sessions": {
                    key: _usage_dict(usage) for key, usage in sorted(self._sessions.items())
                },
                "latency": {
                    key: {
                        "count": value.count,
                        "avg_seconds": round(value.average, 4),
                        "max_seconds": round(value.worst, 4),
                    }
                    for key, value in sorted(self._latencies.items())
                },
            }

    def prometheus(self) -> str:
        """Render counters as Prometheus text exposition format."""
        lines: list[str] = []
        snapshot = self.snapshot()
        lines.append("# HELP anyaclaw_uptime_seconds Process uptime.")
        lines.append("# TYPE anyaclaw_uptime_seconds gauge")
        lines.append(f"anyaclaw_uptime_seconds {snapshot['uptime_seconds']}")
        lines.append("# TYPE anyaclaw_counter_total counter")
        for key, value in snapshot["counters"].items():
            lines.append(f"anyaclaw_counter_total{{{key}}} {value:g}")
        lines.append("# TYPE anyaclaw_model_tokens_total counter")
        for model, usage in snapshot["models"].items():
            lines.append(
                f'anyaclaw_model_tokens_total{{model="{model}",kind="prompt"}} '
                f'{usage["prompt_tokens"]}'
            )
            lines.append(
                f'anyaclaw_model_tokens_total{{model="{model}",kind="completion"}} '
                f'{usage["completion_tokens"]}'
            )
        lines.append("# TYPE anyaclaw_tool_seconds summary")
        for key, value in snapshot["latency"].items():
            lines.append(f'anyaclaw_tool_seconds_sum{{{key}}} {value["avg_seconds"]:g}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._models.clear()
            self._sessions.clear()
            self._latencies.clear()
            self.started_at = time.time()


def _usage_dict(usage: ModelUsage) -> dict[str, int]:
    return {
        "calls": usage.calls,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cached_tokens,
    }


def _label_key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    parts: Iterable[str] = (
        f'{key}="{value}"' for key, value in sorted(labels.items()) if value is not None
    )
    return f"{name}{{{','.join(parts)}}}"
