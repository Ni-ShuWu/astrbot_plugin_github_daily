"""Small in-memory TTL cache and request cooldown tracker."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float


class ActivityCache(Generic[T]):
    """Cache GitHub activity responses and enforce per-user cooldowns."""

    def __init__(self, ttl_seconds: int, cooldown_seconds: int) -> None:
        self._ttl_seconds = max(0, ttl_seconds)
        self._cooldown_seconds = max(0, cooldown_seconds)
        self._entries: dict[str, _Entry[T]] = {}
        self._last_request: dict[str, float] = {}

    def get(self, key: str) -> T | None:
        """Return an unexpired value, or ``None`` when absent."""
        entry = self._entries.get(key)
        if entry is None or entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: T) -> None:
        """Store a value using the configured TTL."""
        if self._ttl_seconds <= 0:
            return
        self._entries[key] = _Entry(value, time.monotonic() + self._ttl_seconds)

    def cooldown_remaining(self, key: str) -> float:
        """Return remaining cooldown seconds for a key."""
        remaining = self._cooldown_seconds - (time.monotonic() - self._last_request.get(key, 0.0))
        return max(0.0, remaining)

    def mark_requested(self, key: str) -> None:
        """Record a request timestamp for a key."""
        self._last_request[key] = time.monotonic()

    def clear(self) -> None:
        """Clear cached values and request timestamps."""
        self._entries.clear()
        self._last_request.clear()
