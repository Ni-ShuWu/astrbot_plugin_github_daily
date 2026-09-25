"""Classify GitHub activities as coding or ordinary activity."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import DEFAULT_CODE_EVENT_TYPES
from .models import ActivitySummary, GitHubActivity


def filter_by_repository(activities: Iterable[GitHubActivity], repository: str) -> list[GitHubActivity]:
    """Return the activities that happened in one ``owner/name`` repository."""
    target = repository.strip().lower()
    return [item for item in activities if (item.repository or "").strip().lower() == target]


def summarize_activities(
    activities: Iterable[GitHubActivity],
    *,
    window_hours: int,
    code_event_types: Iterable[str] = DEFAULT_CODE_EVENT_TYPES,
    now: datetime | None = None,
) -> ActivitySummary:
    """Summarize activities that fall within the configured time window."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current - timedelta(hours=window_hours)
    code_types = set(code_event_types)
    recent = sorted(
        (item for item in activities if item.created_at.astimezone(timezone.utc) >= cutoff),
        key=lambda item: item.created_at,
        reverse=True,
    )
    counts = Counter(item.event_type for item in recent)
    code_count = sum(1 for item in recent if item.event_type in code_types)
    return ActivitySummary(
        total_count=len(recent),
        code_count=code_count,
        ordinary_count=len(recent) - code_count,
        event_counts=dict(counts),
        latest_activity=recent[0] if recent else None,
    )


def classify_summary(summary: ActivitySummary) -> tuple[bool, str]:
    """Return whether a summary supports a coding conclusion and its status."""
    if summary.code_count > 0:
        return True, "coding"
    if summary.total_count > 0:
        return False, "active"
    return False, "idle"
