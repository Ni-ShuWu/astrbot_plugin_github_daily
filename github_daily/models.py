"""Serializable data models used by the contribution watcher."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import InvalidRepositoryError

REPOSITORY_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    """Serialize a datetime as an ISO-8601 UTC timestamp."""
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


@dataclass(slots=True, frozen=True)
class RepositoryRef:
    """A GitHub repository identified by its owner and name."""

    owner: str
    name: str

    @property
    def slug(self) -> str:
        """Return the canonical ``owner/name`` slug used by the events API."""
        return f"{self.owner}/{self.name}"

    @classmethod
    def parse(cls, value: str) -> "RepositoryRef":
        """Parse ``owner/name``, a github.com URL or an SSH remote into a reference."""
        text = str(value or "").strip()
        host = ""
        if text.startswith("git@"):
            host, _, path = text.partition(":")
            host = host[len("git@"):]
        elif "://" in text:
            host, _, path = text.split("://", 1)[1].partition("/")
            host = host.split("@")[-1]  # Drop credentials such as user:token@.
        else:
            path = text
        if not host and path.lower().startswith("github.com/"):
            host, _, path = path.partition("/")
        if host and host.lower() != "github.com":
            raise InvalidRepositoryError("目前只支持 github.com 上的仓库")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        segments = [part for part in path.strip("/").split("/") if part]
        if host:
            segments = segments[:2]  # A pasted URL may carry a /tree/main suffix.
        slug = "/".join(segments)
        if not REPOSITORY_SLUG_PATTERN.fullmatch(slug):
            raise InvalidRepositoryError(
                "仓库格式无效，请使用 owner/repo，例如 Ni-ShuWu/astrbot_plugin_github_daily",
            )
        owner, name = slug.split("/", 1)
        return cls(owner=owner, name=name)


@dataclass(slots=True, frozen=True)
class WatchedAccount:
    """A GitHub account monitored in one chat scope."""

    username: str
    display_name: str | None = None
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    owner_id: str | None = None

    @property
    def label(self) -> str:
        """Return the preferred human-readable account name."""
        return self.display_name or self.username

    def is_owned_by(self, actor_id: str | None) -> bool:
        """Return whether a chat user bound this account."""
        return bool(self.owner_id) and self.owner_id == actor_id

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "username": self.username,
            "display_name": self.display_name,
            "added_at": _format_datetime(self.added_at),
            "owner_id": self.owner_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchedAccount":
        """Build an account from persisted JSON data."""
        return cls(
            username=str(data["username"]),
            display_name=data.get("display_name"),
            added_at=_parse_datetime(data.get("added_at")) or datetime.now(timezone.utc),
            owner_id=str(data["owner_id"]) if data.get("owner_id") else None,
        )


@dataclass(slots=True, frozen=True)
class GitHubActivity:
    """A normalized event returned by GitHub's public events API."""

    event_id: str
    event_type: str
    actor_login: str
    repository: str | None
    created_at: datetime
    url: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor_login": self.actor_login,
            "repository": self.repository,
            "created_at": _format_datetime(self.created_at),
            "url": self.url,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GitHubActivity":
        """Build an activity from persisted JSON data."""
        created_at = _parse_datetime(data.get("created_at"))
        if created_at is None:
            raise ValueError("activity created_at is required")
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            actor_login=str(data.get("actor_login", "")),
            repository=data.get("repository"),
            created_at=created_at,
            url=data.get("url"),
            message=data.get("message"),
        )


@dataclass(slots=True, frozen=True)
class ActivitySummary:
    """Counts of activities in a requested time window."""

    total_count: int
    code_count: int
    ordinary_count: int
    event_counts: dict[str, int] = field(default_factory=dict)
    latest_activity: GitHubActivity | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "total_count": self.total_count,
            "code_count": self.code_count,
            "ordinary_count": self.ordinary_count,
            "event_counts": dict(self.event_counts),
            "latest_activity": self.latest_activity.to_dict() if self.latest_activity else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActivitySummary":
        """Build a summary from persisted JSON data."""
        latest = data.get("latest_activity")
        return cls(
            total_count=int(data.get("total_count", 0)),
            code_count=int(data.get("code_count", 0)),
            ordinary_count=int(data.get("ordinary_count", 0)),
            event_counts={str(key): int(value) for key, value in data.get("event_counts", {}).items()},
            latest_activity=GitHubActivity.from_dict(latest) if latest else None,
        )


@dataclass(slots=True, frozen=True)
class AccountCheckResult:
    """Classification result for one watched account."""

    account: WatchedAccount
    checked_at: datetime
    window_hours: int
    summary: ActivitySummary
    is_coding: bool
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "account": self.account.to_dict(),
            "checked_at": _format_datetime(self.checked_at),
            "window_hours": self.window_hours,
            "summary": self.summary.to_dict(),
            "is_coding": self.is_coding,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountCheckResult":
        """Build a result from persisted JSON data."""
        checked_at = _parse_datetime(data.get("checked_at"))
        if checked_at is None:
            raise ValueError("checked_at is required")
        return cls(
            account=WatchedAccount.from_dict(data["account"]),
            checked_at=checked_at,
            window_hours=int(data["window_hours"]),
            summary=ActivitySummary.from_dict(data["summary"]),
            is_coding=bool(data["is_coding"]),
            status=str(data["status"]),
            error=data.get("error"),
        )


@dataclass(slots=True, frozen=True)
class WatchState:
    """Last announced state for one account in one chat scope."""

    status: str
    fingerprint: str
    announced_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "status": self.status,
            "fingerprint": self.fingerprint,
            "announced_at": _format_datetime(self.announced_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchState":
        """Build state from persisted JSON data."""
        return cls(
            status=str(data["status"]),
            fingerprint=str(data["fingerprint"]),
            announced_at=_parse_datetime(data.get("announced_at")),
        )


@dataclass(slots=True, frozen=True)
class RepoContributionReport:
    """Contributions of one scope's bound members inside a single repository.

    ``contributions`` holds members with activity in the repository, sorted by
    code activity. ``silent_members`` lists bound members without any activity
    there, and ``failures`` carries members whose events could not be read.
    """

    repository: RepositoryRef
    checked_at: datetime
    window_hours: int
    contributions: tuple[AccountCheckResult, ...] = ()
    silent_members: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def member_count(self) -> int:
        """Return how many bound members the report accounts for."""
        return len(self.contributions) + len(self.silent_members) + len(self.failures)
