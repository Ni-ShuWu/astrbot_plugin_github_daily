"""Track the GitHub REST API budget that responses advertise in their headers.

GitHub bills one primary-rate-limit unit per REST request, and a conditional
request answered with ``304 Not Modified`` is billed too (verified against the
live API), so the only way to stay inside the budget is to avoid sending
requests at all. This module keeps the most recent ``X-RateLimit-*`` snapshot
so the service can skip a request that is certain to be rejected, and can tell
the user when the budget comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping

#: Seconds to keep waiting after the advertised reset, to absorb clock skew.
RESET_GRACE_SECONDS = 1.0


def _as_int(value: Any) -> int | None:
    """Parse a header value into an int, returning ``None`` when unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(slots=True, frozen=True)
class RateLimitState:
    """The most recent ``X-RateLimit-*`` snapshot for the core resource."""

    limit: int = 0
    remaining: int = 0
    reset_at: datetime | None = None
    resource: str = ""

    @property
    def known(self) -> bool:
        """Return whether any GitHub response has reported a budget yet."""
        return self.reset_at is not None

    def seconds_until_reset(self, now: datetime | None = None) -> float:
        """Return how long until the current rate-limit window rolls over."""
        if self.reset_at is None:
            return 0.0
        current = now or datetime.now(timezone.utc)
        return max(0.0, (self.reset_at - current).total_seconds())

    def wait_seconds(self, reserve: int = 0, now: datetime | None = None) -> float:
        """Return how long the next request must wait, ``0.0`` when it may go.

        A past reset timestamp means the advertised window already rolled over,
        so the request is allowed through and the next response refreshes the
        snapshot.
        """
        if not self.known:
            return 0.0
        if self.remaining > max(0, reserve):
            return 0.0
        remaining = self.seconds_until_reset(now)
        if remaining <= 0:
            return 0.0
        return remaining + RESET_GRACE_SECONDS


class RateLimitTracker:
    """Fold every response's headers into the current budget snapshot."""

    def __init__(self) -> None:
        """Initialize an unknown rate-limit snapshot."""
        self._state = RateLimitState()

    @property
    def state(self) -> RateLimitState:
        """Return the current snapshot without refreshing it."""
        return self._state

    def observe(self, headers: Mapping[str, Any] | None) -> RateLimitState:
        """Update the snapshot from response headers, ignoring absent fields."""
        lowered = {str(key).lower(): value for key, value in dict(headers or {}).items()}
        limit = _as_int(lowered.get("x-ratelimit-limit"))
        remaining = _as_int(lowered.get("x-ratelimit-remaining"))
        reset = _as_int(lowered.get("x-ratelimit-reset"))
        resource = str(lowered.get("x-ratelimit-resource") or "").strip()
        if limit is None and remaining is None and reset is None:
            return self._state
        state = self._state
        self._state = replace(
            state,
            limit=state.limit if limit is None else max(0, limit),
            remaining=state.remaining if remaining is None else max(0, remaining),
            resource=resource or state.resource,
            reset_at=(
                state.reset_at
                if reset is None
                else datetime.fromtimestamp(reset, tz=timezone.utc)
            ),
        )
        return self._state

    def wait_seconds(self, reserve: int = 0) -> float:
        """Return how long the next request must wait under this budget."""
        return self._state.wait_seconds(reserve)
