"""Asynchronous adapter for GitHub's public Events API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .errors import GitHubApiError
from .models import GitHubActivity


class GitHubAdapter:
    """Fetch and normalize public GitHub events for a user."""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str = "", timeout_seconds: float = 10.0, max_retries: int = 2) -> None:
        self._token = token.strip()
        self._timeout = max(1.0, timeout_seconds)
        self._max_retries = max(0, max_retries)

    async def fetch_user_events(self, username: str) -> list[GitHubActivity]:
        """Fetch up to the first 100 public events for a GitHub username."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "astrbot-plugin-github-daily",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self.BASE_URL}/users/{username}/events/public"
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
                    response = await client.get(url, params={"per_page": 100})
                if response.status_code == 200:
                    payload = response.json()
                    return [self._parse_event(item) for item in payload if isinstance(item, dict)]
                retry_after = self._retry_after(response)
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < self._max_retries:
                    await asyncio.sleep(retry_after or 2**attempt)
                    continue
                message = self._error_message(response)
                raise GitHubApiError(message, status_code=response.status_code, retry_after_seconds=retry_after)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise GitHubApiError(f"GitHub request failed: {exc}") from exc
                await asyncio.sleep(2**attempt)
        raise GitHubApiError("GitHub request failed after retries")

    @staticmethod
    def _parse_event(item: dict[str, Any]) -> GitHubActivity:
        """Convert one GitHub event payload into a normalized activity."""
        created_raw = item.get("created_at") or datetime.now(timezone.utc).isoformat()
        created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        repo = item.get("repo") or {}
        actor = item.get("actor") or {}
        payload = item.get("payload") or {}
        message = None
        commits = payload.get("commits") or []
        if commits and isinstance(commits[0], dict):
            message = commits[0].get("message")
        return GitHubActivity(
            event_id=str(item.get("id", "")),
            event_type=str(item.get("type", "UnknownEvent")),
            actor_login=str(actor.get("login", "")),
            repository=repo.get("name"),
            created_at=created_at.astimezone(timezone.utc),
            url=payload.get("html_url") or item.get("url"),
            message=message,
        )

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

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        """Create a concise human-readable API error."""
        if response.status_code == 404:
            return "GitHub user was not found"
        if response.status_code in (401, 403):
            return "GitHub API authorization failed or rate limit was reached"
        if response.status_code == 429:
            return "GitHub API rate limit was reached"
        return f"GitHub API returned HTTP {response.status_code}"
