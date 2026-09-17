"""Configuration model and safe normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_CODE_EVENT_TYPES = (
    "PushEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
)
OPTIONAL_CODE_EVENT_TYPES = ("CreateEvent", "ReleaseEvent")


@dataclass(slots=True, frozen=True)
class PluginConfig:
    """Runtime configuration for the watcher service."""

    window_hours: int = 24
    cache_ttl_seconds: int = 300
    request_cooldown_seconds: int = 15
    request_timeout_seconds: float = 10.0
    max_retries: int = 2
    code_event_types: tuple[str, ...] = field(default_factory=lambda: DEFAULT_CODE_EVENT_TYPES)
    auto_check_enabled: bool = False
    auto_check_interval_seconds: int = 3600
    announce_only_on_change: bool = True
    min_announce_interval_seconds: int = 3600
    github_token: str = ""
    admin_only: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "PluginConfig":
        """Create a validated configuration from AstrBot config data."""
        values = data or {}
        configured_events = values.get("code_event_types", DEFAULT_CODE_EVENT_TYPES)
        if isinstance(configured_events, str):
            configured_events = [item.strip() for item in configured_events.split(",")]
        events = tuple(dict.fromkeys(str(item).strip() for item in configured_events if str(item).strip()))
        if not events:
            events = DEFAULT_CODE_EVENT_TYPES
        return cls(
            window_hours=max(1, int(values.get("window_hours", 24))),
            cache_ttl_seconds=max(0, int(values.get("cache_ttl_seconds", 300))),
            request_cooldown_seconds=max(0, int(values.get("request_cooldown_seconds", 15))),
            request_timeout_seconds=max(1.0, float(values.get("request_timeout_seconds", 10.0))),
            max_retries=max(0, int(values.get("max_retries", 2))),
            code_event_types=events,
            auto_check_enabled=bool(values.get("auto_check_enabled", False)),
            auto_check_interval_seconds=max(60, int(values.get("auto_check_interval_seconds", 3600))),
            announce_only_on_change=bool(values.get("announce_only_on_change", True)),
            min_announce_interval_seconds=max(0, int(values.get("min_announce_interval_seconds", 3600))),
            github_token=str(values.get("github_token", "") or "").strip(),
            admin_only=bool(values.get("admin_only", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation without hiding runtime values."""
        return {
            "window_hours": self.window_hours,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "request_cooldown_seconds": self.request_cooldown_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "code_event_types": list(self.code_event_types),
            "auto_check_enabled": self.auto_check_enabled,
            "auto_check_interval_seconds": self.auto_check_interval_seconds,
            "announce_only_on_change": self.announce_only_on_change,
            "min_announce_interval_seconds": self.min_announce_interval_seconds,
            "github_token": self.github_token,
            "admin_only": self.admin_only,
        }
