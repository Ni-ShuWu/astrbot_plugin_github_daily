"""Small in-memory TTL cache and request cooldown tracker.

Every GitHub request costs one primary rate-limit unit, so a cached answer is
the cheapest request there is. Two details keep that budget honest:

* entries are capped (least recently used evicted) because the ``detail``
  command can ask about arbitrary usernames, not just bound ones;
* an expired entry survives for a while as a *stale* fallback, so a spent
  quota or a failing network degrades to a slightly old answer instead of an
  error - the caller is told the data is stale.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

#: Bound on cached users; each entry holds up to 100 normalized events.
DEFAULT_MAX_ENTRIES = 200
#: How long an expired entry may still answer when GitHub cannot be queried.
DEFAULT_STALE_SECONDS = 1800


@dataclass(slots=True)
class _Entry(Generic[T]):
    """Store one cached value and its fresh/stale timing metadata."""

    value: T
    expires_at: float
    stored_at: float


class ActivityCache(Generic[T]):
    """Cache GitHub activity responses and enforce per-user cooldowns."""

    def __init__(
        self,
        ttl_seconds: int,
        cooldown_seconds: int,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
    ) -> None:
        """Create a bounded cache with fresh, stale and cooldown windows."""
        self._ttl_seconds = max(0, ttl_seconds)
        self._cooldown_seconds = max(0, cooldown_seconds)
        self._max_entries = max(1, max_entries)
        # Setting the TTL to 0 disables caching entirely, stale reads included.
        self._stale_seconds = max(0, stale_seconds) if self._ttl_seconds > 0 else 0
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._last_request: dict[str, float] = {}

    @property
    def stale_seconds(self) -> int:
        """Return how long an expired entry may still answer."""
        return self._stale_seconds

    def get(self, key: str) -> T | None:
        """Return an unexpired value, or ``None`` when absent or expired."""
        entry = self._entries.get(key)
        if entry is None or entry.expires_at <= time.monotonic():
            return None
        self._entries.move_to_end(key)
        return entry.value

    def get_stale(self, key: str) -> T | None:
        """Return the last value even when expired, while it is still recent.

        Used as the fallback when GitHub refuses to answer; entries older than
        the TTL plus the stale window are dropped so the cache cannot grow into
        a source of misleadingly old data.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at > self._ttl_seconds + self._stale_seconds:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: T) -> None:
        """Store a value using the configured TTL."""
        if self._ttl_seconds <= 0:
            return
        now = time.monotonic()
        self._entries[key] = _Entry(value, now + self._ttl_seconds, now)
        self._entries.move_to_end(key)
        self._evict()

    def cooldown_remaining(self, key: str) -> float:
        """Return remaining cooldown seconds for a key."""
        remaining = self._cooldown_seconds - (time.monotonic() - self._last_request.get(key, 0.0))
        return max(0.0, remaining)

    def mark_requested(self, key: str) -> None:
        """Record a request timestamp for a key."""
        self._last_request[key] = time.monotonic()
        if len(self._last_request) > self._max_entries * 4:
            for stale_key in list(self._last_request)[: self._max_entries]:
                self._last_request.pop(stale_key, None)

    def clear(self) -> None:
        """Clear cached values and request timestamps."""
        self._entries.clear()
        self._last_request.clear()

    def _evict(self) -> None:
        """Drop the least recently used entries beyond the configured cap."""
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
