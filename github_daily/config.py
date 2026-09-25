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

_TRUE_LITERALS = {"1", "true", "yes", "y", "on", "是", "开启", "启用"}
_FALSE_LITERALS = {"0", "false", "no", "n", "off", "否", "关闭", "禁用"}


def _as_bool(value: Any, default: bool) -> bool:
    """Parse a boolean without treating the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_LITERALS:
            return True
        if normalized in _FALSE_LITERALS:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    """Parse an integer, falling back to the default for invalid input."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _as_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    """Parse a float, falling back to the default for invalid input."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _as_string_tuple(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Normalize a comma/Chinese-comma separated string or list into a tuple."""
    if isinstance(value, str):
        items = value.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    cleaned = (str(item).strip() for item in items)
    return tuple(dict.fromkeys(item for item in cleaned if item))


def _as_masked_token(value: Any) -> str:
    """Read the token value, treating non-string input as unset."""
    return value.strip() if isinstance(value, str) else ""


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
    github_token: str = field(default="", repr=False)
    allow_self_bind: bool = True
    allow_public_query: bool = True
    allowed_group_ids: tuple[str, ...] = ()
    max_accounts_per_scope: int = 20

    def is_group_allowed(self, group_id: str | None) -> bool:
        """Return whether a group is included in the configured whitelist."""
        normalized = str(group_id or "").strip()
        return bool(normalized) and normalized in self.allowed_group_ids

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "PluginConfig":
        """Create a validated configuration from AstrBot config data."""
        values = data or {}
        events = _as_string_tuple(values.get("code_event_types")) or DEFAULT_CODE_EVENT_TYPES
        return cls(
            window_hours=_as_int(values.get("window_hours"), 24, minimum=1),
            cache_ttl_seconds=_as_int(values.get("cache_ttl_seconds"), 300, minimum=0),
            request_cooldown_seconds=_as_int(values.get("request_cooldown_seconds"), 15, minimum=0),
            request_timeout_seconds=_as_float(values.get("request_timeout_seconds"), 10.0, minimum=1.0),
            max_retries=_as_int(values.get("max_retries"), 2, minimum=0),
            code_event_types=events,
            auto_check_enabled=_as_bool(values.get("auto_check_enabled"), False),
            auto_check_interval_seconds=_as_int(values.get("auto_check_interval_seconds"), 3600, minimum=60),
            announce_only_on_change=_as_bool(values.get("announce_only_on_change"), True),
            min_announce_interval_seconds=_as_int(values.get("min_announce_interval_seconds"), 3600, minimum=0),
            github_token=_as_masked_token(values.get("github_token")),
            allow_self_bind=_as_bool(values.get("allow_self_bind"), True),
            allow_public_query=_as_bool(values.get("allow_public_query"), True),
            allowed_group_ids=_as_string_tuple(values.get("allowed_group_ids")),
            max_accounts_per_scope=_as_int(values.get("max_accounts_per_scope"), 20, minimum=1),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation with the token masked."""
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
            "github_token": "***" if self.github_token else "",
            "allow_self_bind": self.allow_self_bind,
            "allow_public_query": self.allow_public_query,
            "allowed_group_ids": list(self.allowed_group_ids),
            "max_accounts_per_scope": self.max_accounts_per_scope,
        }
