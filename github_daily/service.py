"""Application service for account management and contribution checks."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .cache import ActivityCache
from .classifier import classify_summary, summarize_activities
from .config import PluginConfig
from .errors import AccountNotFoundError, InvalidAccountError
from .github_adapter import GitHubAdapter
from .models import AccountCheckResult, WatchedAccount, WatchState

PersistLoader = Callable[[], Awaitable[dict[str, list[dict]]]]
PersistSaver = Callable[[dict[str, list[dict]]], Awaitable[None]]


class ContributionService:
    """Coordinate persistence, GitHub access, caching and classification."""

    USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")

    def __init__(self, config: PluginConfig, loader: PersistLoader, saver: PersistSaver) -> None:
        self.config = config
        self._loader = loader
        self._saver = saver
        self._adapter = GitHubAdapter(config.github_token, config.request_timeout_seconds, config.max_retries)
        self._cache: ActivityCache = ActivityCache(config.cache_ttl_seconds, config.request_cooldown_seconds)

    async def add_account(self, scope: str, username: str, display_name: str | None = None) -> WatchedAccount:
        """Add or replace a watched account in a chat scope."""
        normalized = username.strip()
        if not self.USERNAME_PATTERN.fullmatch(normalized):
            raise InvalidAccountError("GitHub 用户名格式无效")
        data = await self._loader()
        accounts = [WatchedAccount.from_dict(item) for item in data.get(scope, [])]
        account = WatchedAccount(normalized, display_name.strip() if display_name else None)
        accounts = [item for item in accounts if item.username.lower() != normalized.lower()]
        accounts.append(account)
        data[scope] = [item.to_dict() for item in accounts]
        await self._saver(data)
        return account

    async def remove_account(self, scope: str, username: str) -> bool:
        """Remove a watched account and return whether it existed."""
        data = await self._loader()
        old = [WatchedAccount.from_dict(item) for item in data.get(scope, [])]
        new = [item for item in old if item.username.lower() != username.strip().lower()]
        if len(old) == len(new):
            return False
        data[scope] = [item.to_dict() for item in new]
        await self._saver(data)
        return True

    async def list_accounts(self, scope: str) -> list[WatchedAccount]:
        """List watched accounts in a chat scope."""
        data = await self._loader()
        return [WatchedAccount.from_dict(item) for item in data.get(scope, [])]

    async def check_account(self, scope: str, username: str) -> AccountCheckResult:
        """Check one configured account and persist its latest state."""
        accounts = await self.list_accounts(scope)
        account = next((item for item in accounts if item.username.lower() == username.lower()), None)
        if account is None:
            raise AccountNotFoundError(f"未配置 GitHub 账户：{username}")
        activities = await self._get_events(account.username)
        now = datetime.now(timezone.utc)
        summary = summarize_activities(activities, window_hours=self.config.window_hours, code_event_types=self.config.code_event_types, now=now)
        is_coding, status = classify_summary(summary)
        result = AccountCheckResult(account, now, self.config.window_hours, summary, is_coding, status)
        await self._save_result(scope, result)
        return result

    async def check_all(self, scope: str) -> list[AccountCheckResult]:
        """Check every configured account in a chat scope."""
        accounts = await self.list_accounts(scope)
        results: list[AccountCheckResult] = []
        for account in accounts:
            results.append(await self.check_account(scope, account.username))
        return results

    async def _get_events(self, username: str):
        """Read events from cache or fetch them after cooldown enforcement."""
        cached = self._cache.get(username.lower())
        if cached is not None:
            return cached
        remaining = self._cache.cooldown_remaining(username.lower())
        if remaining > 0:
            raise RuntimeError(f"请求过于频繁，请 {remaining:.0f} 秒后再试")
        self._cache.mark_requested(username.lower())
        events = await self._adapter.fetch_user_events(username)
        self._cache.set(username.lower(), events)
        return events

    async def _save_result(self, scope: str, result: AccountCheckResult) -> None:
        """Persist the latest result and its announcement fingerprint."""
        data = await self._loader()
        results = data.setdefault("_results", {})
        states = data.setdefault("_states", {})
        key = f"{scope}:{result.account.username.lower()}"
        results[key] = result.to_dict()
        fingerprint_source = f"{result.status}:{result.summary.total_count}:{result.summary.code_count}:{result.summary.latest_activity.event_id if result.summary.latest_activity else ''}"
        states[key] = WatchState(result.status, hashlib.sha256(fingerprint_source.encode()).hexdigest()).to_dict()
        await self._saver(data)

    def format_result(self, result: AccountCheckResult) -> str:
        """Format a check result as a concise Chinese chat message."""
        summary = result.summary
        label = result.account.label
        if result.status == "coding":
            conclusion = "不是摸鱼，正在写代码。"
        elif result.status == "active":
            conclusion = "有 GitHub 活动，但暂时不能确认在写代码。"
        else:
            conclusion = "最近没有检测到公开活动，疑似摸鱼。"
        lines = [f"{label} 最近 {result.window_hours} 小时 GitHub 状态：", f"- 活动总数：{summary.total_count}", f"- 代码相关活动：{summary.code_count}", f"- 普通活动：{summary.ordinary_count}", f"结论：{conclusion}"]
        if summary.latest_activity:
            latest = summary.latest_activity
            lines.insert(4, f"- 最近活动：{latest.event_type} / {latest.repository or '未知仓库'}")
        return "\n".join(lines)
