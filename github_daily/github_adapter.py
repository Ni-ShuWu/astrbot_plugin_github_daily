"""Asynchronous adapter for GitHub's public Events API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .errors import GitHubApiError
from .models import GitHubActivity
from .rate_limit import RateLimitTracker


class GitHubAdapter:
    """Fetch and normalize public GitHub events for a user."""

    BASE_URL = "https://api.github.com"
    MAX_COMMIT_MESSAGES = 5
    #: One request returns the same number of events whatever this is set to,
    #: so ask for a full page: it costs the same quota and keeps the time-window
    #: summary accurate for busy accounts.
    EVENTS_PER_PAGE = 100
    #: Never sleep longer than this inside a retry; fail fast instead.
    MAX_RETRY_SLEEP_SECONDS = 30.0

    def __init__(self, token: str = "", timeout_seconds: float = 10.0, max_retries: int = 2) -> None:
        """Configure authentication, request timeout and retry behavior."""
        self._token = token.strip()
        self._timeout = max(1.0, timeout_seconds)
        self._max_retries = max(0, max_retries)
        self.rate_limit = RateLimitTracker()

    async def fetch_user_events(self, username: str) -> list[GitHubActivity]:
        """Fetch up to the first ``EVENTS_PER_PAGE`` public events for a user."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "astrbot-plugin-github-daily",
        }
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        url = f"{self.BASE_URL}/users/{username}/events/public"
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
                    response = await client.get(url, params={"per_page": self.EVENTS_PER_PAGE})
                self.rate_limit.observe(response.headers)
                if response.status_code == 200:
                    payload = response.json()
                    return [self._parse_event(item) for item in payload if isinstance(item, dict)]
                retry_after = self._retry_after(response)
                delay = retry_after if retry_after is not None else float(2**attempt)
                retryable = response.status_code >= 500 or (
                    # A 429 is only worth retrying when the budget is not simply
                    # spent - retrying an exhausted quota just burns more time.
                    response.status_code == 429 and self.rate_limit.wait_seconds() <= 0
                )
                if retryable and delay <= self.MAX_RETRY_SLEEP_SECONDS and attempt < self._max_retries:
                    await asyncio.sleep(delay)
                    continue
                message = self._error_message(response)
                raise GitHubApiError(message, status_code=response.status_code, retry_after_seconds=retry_after)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise GitHubApiError(f"GitHub request failed: {exc}") from exc
                await asyncio.sleep(2**attempt)
        raise GitHubApiError("GitHub request failed after retries")

    @classmethod
    def _parse_event(cls, item: dict[str, Any]) -> GitHubActivity:
        """Convert one GitHub event payload into a normalized activity."""
        created_raw = item.get("created_at") or datetime.now(timezone.utc).isoformat()
        created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        repo = item.get("repo") if isinstance(item.get("repo"), dict) else {}
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        event_type = str(item.get("type", "UnknownEvent"))
        repository = repo.get("name") or None
        detail = cls._extract_detail(event_type, payload, repository)
        commits = detail.pop("commits", ())
        return GitHubActivity(
            event_id=str(item.get("id", "")),
            event_type=event_type,
            actor_login=str(actor.get("login", "")),
            repository=repository,
            created_at=created_at.astimezone(timezone.utc),
            url=detail.pop("url", None) or payload.get("html_url") or item.get("url"),
            message=commits[0] if commits else None,
            commits=commits,
            **detail,
        )

    @classmethod
    def _extract_detail(
        cls,
        event_type: str,
        payload: dict[str, Any],
        repository: str | None,
    ) -> dict[str, Any]:
        """Pull the optional fields rendered by the ``detail`` command.

        GitHub payloads differ per event type, so every branch stays defensive:
        a missing or unexpectedly typed field is dropped rather than failing the
        whole check.
        """
        detail: dict[str, Any] = {}
        if event_type == "PushEvent":
            commits = cls._commit_messages(payload)
            size = payload.get("size")
            if not isinstance(size, int) or size < 0:
                size = len(commits)
            detail["ref"] = cls._clean_text(payload.get("ref"), 120) or None
            detail["ref_type"] = "branch"  # PushEvent refs are always branches.
            detail["commits"] = commits
            detail["commit_count"] = size
            head = cls._clean_text(payload.get("head"), 120)
            if head and repository:
                detail["url"] = f"https://github.com/{repository}/commit/{head}"
        elif event_type in {"CreateEvent", "DeleteEvent"}:
            ref = cls._clean_text(payload.get("ref"), 120) or None
            ref_type = cls._clean_text(payload.get("ref_type"), 32) or None
            detail["ref"] = ref
            detail["ref_type"] = ref_type
            detail["action"] = "created" if event_type == "CreateEvent" else "deleted"
            if repository:
                detail["url"] = cls._repository_url(repository, ref, ref_type, event_type)
        elif event_type in {"PullRequestEvent", "PullRequestReviewEvent", "PullRequestReviewThreadEvent"}:
            pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            detail["action"] = cls._clean_text(payload.get("action"), 32) or None
            detail["number"] = pull.get("number") if isinstance(pull.get("number"), int) else None
            detail["title"] = cls._clean_text(pull.get("title"), 200) or None
            detail["url"] = cls._clean_text(pull.get("html_url"), 300) or None
        elif event_type == "PullRequestReviewCommentEvent":
            pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
            detail["action"] = cls._clean_text(payload.get("action"), 32) or None
            detail["number"] = pull.get("number") if isinstance(pull.get("number"), int) else None
            detail["title"] = cls._clean_text(pull.get("title"), 200) or None
            detail["url"] = cls._clean_text(comment.get("html_url"), 300) or None
        elif event_type == "IssuesEvent":
            issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
            detail["action"] = cls._clean_text(payload.get("action"), 32) or None
            detail["number"] = issue.get("number") if isinstance(issue.get("number"), int) else None
            detail["title"] = cls._clean_text(issue.get("title"), 200) or None
            detail["url"] = cls._clean_text(issue.get("html_url"), 300) or None
        elif event_type == "IssueCommentEvent":
            issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
            comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
            detail["action"] = cls._clean_text(payload.get("action"), 32) or None
            detail["number"] = issue.get("number") if isinstance(issue.get("number"), int) else None
            detail["title"] = cls._clean_text(issue.get("title"), 200) or None
            detail["url"] = cls._clean_text(comment.get("html_url"), 300) or None
        elif event_type == "ReleaseEvent":
            release = payload.get("release") if isinstance(payload.get("release"), dict) else {}
            detail["action"] = cls._clean_text(payload.get("action"), 32) or None
            detail["title"] = (
                cls._clean_text(release.get("name"), 200)
                or cls._clean_text(release.get("tag_name"), 200)
                or None
            )
            detail["url"] = cls._clean_text(release.get("html_url"), 300) or None
        elif event_type == "ForkEvent":
            forkee = payload.get("forkee") if isinstance(payload.get("forkee"), dict) else {}
            detail["title"] = cls._clean_text(forkee.get("full_name"), 200) or None
            detail["url"] = cls._clean_text(forkee.get("html_url"), 300) or None
        elif event_type == "MemberEvent":
            member = payload.get("member") if isinstance(payload.get("member"), dict) else {}
            detail["action"] = cls._clean_text(payload.get("action"), 32) or None
            detail["title"] = cls._clean_text(member.get("login"), 100) or None
        elif event_type == "CommitCommentEvent":
            comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
            detail["title"] = cls._clean_text(comment.get("body"), 200) or None
            detail["url"] = cls._clean_text(comment.get("html_url"), 300) or None
        elif event_type == "GollumEvent":
            pages = payload.get("pages") if isinstance(payload.get("pages"), list) else []
            first = pages[0] if pages and isinstance(pages[0], dict) else {}
            detail["action"] = cls._clean_text(first.get("action"), 32) or None
            detail["title"] = cls._clean_text(first.get("page_name"), 200) or None
            detail["url"] = cls._clean_text(first.get("html_url"), 300) or None
        return detail

    @classmethod
    def _commit_messages(cls, payload: dict[str, Any]) -> tuple[str, ...]:
        """Return the first lines of a push payload's commit messages."""
        raw = payload.get("commits")
        if not isinstance(raw, list):
            return ()
        messages: list[str] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            line = cls._clean_text(entry.get("message"), 200)
            if line:
                messages.append(line)
            if len(messages) >= cls.MAX_COMMIT_MESSAGES:
                break
        return tuple(messages)

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        """Collapse a payload string into one trimmed, length-limited line."""
        if not isinstance(value, str):
            return ""
        first_line = value.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0]
        return " ".join(first_line.split())[:limit]

    @staticmethod
    def _repository_url(repository: str, ref: str | None, ref_type: str | None, event_type: str) -> str:
        """Build a browseable URL for the ref-based create/delete events."""
        if event_type == "CreateEvent" and ref:
            if ref_type == "tag":
                return f"https://github.com/{repository}/releases/tag/{ref}"
            return f"https://github.com/{repository}/tree/{ref}"
        return f"https://github.com/{repository}"

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Read Retry-After as seconds when supplied by GitHub."""
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                return max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _error_message(self, response: httpx.Response) -> str:
        """Create a concise, accurate API error message."""
        if response.status_code == 404:
            return "GitHub user was not found"
        if response.status_code == 429 or self.rate_limit.state.remaining <= 0:
            return "GitHub API rate limit was reached"
        if response.status_code == 401:
            return "GitHub API rejected the configured token"
        if response.status_code == 403:
            return "GitHub API authorization failed (token missing or lacking permission)"
        return f"GitHub API returned HTTP {response.status_code}"
