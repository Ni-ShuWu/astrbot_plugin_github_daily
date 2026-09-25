"""Application service for account management and contribution checks."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .cache import ActivityCache
from .classifier import classify_summary, filter_by_repository, summarize_activities
from .config import PluginConfig
from .errors import AccountNotFoundError, InvalidAccountError, PermissionDeniedError
from .github_adapter import GitHubAdapter
from .models import AccountCheckResult, RepoContributionReport, RepositoryRef, WatchedAccount, WatchState

PluginData = dict[str, Any]
PersistLoader = Callable[[], Awaitable[PluginData]]
PersistSaver = Callable[[PluginData], Awaitable[None]]


def _rebind_denied_text(account: WatchedAccount) -> str:
    """Explain why a non-admin cannot rebind an existing account."""
    if account.owner_id:
        return f"{account.label} 已由其他群成员绑定，请联系管理员处理。"
    return f"{account.label} 是旧版本创建的绑定，没有归属者，请联系管理员处理。"


def _unbind_denied_text(account: WatchedAccount) -> str:
    """Explain why a non-admin cannot unbind an existing account."""
    if account.owner_id:
        return f"{account.label} 由其他群成员绑定，请本人或管理员来解绑。"
    return f"{account.label} 是旧版本创建的绑定，没有归属者，只能由管理员解绑。"


class ContributionService:
    """Coordinate persistence, GitHub access, caching and classification."""

    USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")

    def __init__(self, config: PluginConfig, loader: PersistLoader, saver: PersistSaver) -> None:
        self.config = config
        self._loader = loader
        self._saver = saver
        self._adapter = GitHubAdapter(config.github_token, config.request_timeout_seconds, config.max_retries)
        self._cache: ActivityCache = ActivityCache(config.cache_ttl_seconds, config.request_cooldown_seconds)

    async def add_account(
        self,
        scope: str,
        username: str,
        display_name: str | None = None,
        *,
        owner_id: str | None = None,
        is_admin: bool = False,
    ) -> WatchedAccount:
        """Bind an account to the chat user who requested it.

        A binding belongs to whoever created it. Non-admins can only rebind an
        account they already own; admins may manage any binding.
        """
        normalized = username.strip()
        if not self.USERNAME_PATTERN.fullmatch(normalized):
            raise InvalidAccountError("GitHub 用户名格式无效")
        data = await self._loader()
        accounts = [WatchedAccount.from_dict(item) for item in data.get(scope, [])]
        existing = next(
            (item for item in accounts if item.username.lower() == normalized.lower()),
            None,
        )
        if existing is not None and not existing.is_owned_by(owner_id) and not is_admin:
            raise PermissionDeniedError(_rebind_denied_text(existing))
        account = WatchedAccount(
            username=normalized,
            display_name=display_name.strip() if display_name else None,
            owner_id=owner_id,
        )
        accounts = [item for item in accounts if item.username.lower() != normalized.lower()]
        accounts.append(account)
        data[scope] = [item.to_dict() for item in accounts]
        await self._saver(data)
        return account

    async def remove_account(
        self,
        scope: str,
        username: str,
        *,
        actor_id: str | None = None,
        is_admin: bool = False,
    ) -> bool:
        """Remove an account the actor owns, or any account for an admin.

        Returns ``False`` when the account is not configured for this scope.
        """
        data = await self._loader()
        old = [WatchedAccount.from_dict(item) for item in data.get(scope, [])]
        target = next(
            (item for item in old if item.username.lower() == username.strip().lower()),
            None,
        )
        if target is None:
            return False
        if not target.is_owned_by(actor_id) and not is_admin:
            raise PermissionDeniedError(_unbind_denied_text(target))
        new = [item for item in old if item.username.lower() != target.username.lower()]
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

    async def check_all(self, scope: str) -> tuple[list[AccountCheckResult], list[str]]:
        """Check every configured account, returning results and failure notes.

        One failing account must not stop the remaining accounts from being
        checked, so failures are collected instead of raised.
        """
        accounts = await self.list_accounts(scope)
        results: list[AccountCheckResult] = []
        failures: list[str] = []
        for account in accounts:
            try:
                results.append(await self.check_account(scope, account.username))
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                failures.append(f"{account.label}: {exc}")
        return results, failures

    async def check_repository(self, scope: str, repository: str) -> RepoContributionReport:
        """Summarize bound members' contribution inside one repository.

        Every account bound in ``scope`` is checked against the configured
        window. Members without public activity in that repository are reported
        separately, and a member whose events cannot be read is recorded as a
        failure instead of aborting the whole query.
        """
        ref = RepositoryRef.parse(repository)
        accounts = await self.list_accounts(scope)
        if not accounts:
            raise AccountNotFoundError("当前群没有配置监督账户。")
        now = datetime.now(timezone.utc)
        contributions: list[AccountCheckResult] = []
        silent_members: list[str] = []
        failures: list[str] = []
        for account in accounts:
            try:
                activities = await self._get_events(account.username)
            except Exception as exc:  # noqa: BLE001 - reported per member
                failures.append(f"{account.label}: {exc}")
                continue
            summary = summarize_activities(
                filter_by_repository(activities, ref.slug),
                window_hours=self.config.window_hours,
                code_event_types=self.config.code_event_types,
                now=now,
            )
            if summary.total_count == 0:
                silent_members.append(account.label)
                continue
            is_coding, status = classify_summary(summary)
            contributions.append(
                AccountCheckResult(account, now, self.config.window_hours, summary, is_coding, status),
            )
        contributions.sort(key=lambda item: (item.summary.code_count, item.summary.total_count), reverse=True)
        return RepoContributionReport(
            repository=ref,
            checked_at=now,
            window_hours=self.config.window_hours,
            contributions=tuple(contributions),
            silent_members=tuple(silent_members),
            failures=tuple(failures),
        )

    async def remember_scope(self, scope: str, umo: str, group_id: str) -> None:
        """Store the session origin so scheduled checks can push messages."""
        data = await self._loader()
        scopes = data.setdefault("_scopes", {})
        scopes[scope] = {"umo": umo, "group_id": group_id}
        await self._saver(data)

    async def auto_check_targets(self, is_group_allowed: Callable[[str], bool]) -> list[tuple[str, str]]:
        """Return ``(scope, umo)`` pairs eligible for scheduled announcements."""
        data = await self._loader()
        scopes = data.get("_scopes", {})
        targets: list[tuple[str, str]] = []
        for scope in data:
            if scope.startswith("_"):
                continue
            entry = scopes.get(scope) if isinstance(scopes, dict) else None
            if not isinstance(entry, dict):
                continue
            umo = str(entry.get("umo") or "")
            group_id = str(entry.get("group_id") or "")
            if umo and is_group_allowed(group_id):
                targets.append((scope, umo))
        return targets

    async def should_announce(self, scope: str, result: AccountCheckResult) -> bool:
        """Decide whether a result should be pushed, honoring the config flags.

        Records the announcement time when it returns ``True`` so the minimum
        announcement interval is enforced on later checks.
        """
        data = await self._loader()
        states = data.setdefault("_states", {})
        key = self._state_key(scope, result)
        raw_previous = states.get(key)
        previous = WatchState.from_dict(raw_previous) if isinstance(raw_previous, dict) else None
        fingerprint = self._fingerprint(result)
        changed = previous is None or previous.fingerprint != fingerprint
        within_interval = previous is not None and previous.announced_at is not None and (
            (result.checked_at - previous.announced_at).total_seconds()
            < self.config.min_announce_interval_seconds
        )
        if previous is None:
            announce = True
        elif self.config.announce_only_on_change and not changed:
            announce = False
        elif within_interval:
            announce = False
        else:
            announce = True

        if announce:
            # Deliver the current state and start a new interval.
            pending = fingerprint
            announced_at = result.checked_at
        else:
            # Keep the previous fingerprint so a rate-limited change stays
            # pending and is announced once the interval has elapsed.
            pending = fingerprint if not changed else previous.fingerprint
            announced_at = previous.announced_at if previous else None
        states[key] = WatchState(result.status, pending, announced_at).to_dict()
        await self._saver(data)
        return announce

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

    @staticmethod
    def _state_key(scope: str, result: AccountCheckResult) -> str:
        """Return the persistence key for one account in one chat scope."""
        return f"{scope}:{result.account.username.lower()}"

    @staticmethod
    def _fingerprint(result: AccountCheckResult) -> str:
        """Hash the parts of a result that make an announcement worthwhile."""
        latest_id = result.summary.latest_activity.event_id if result.summary.latest_activity else ""
        source = f"{result.status}:{result.summary.total_count}:{result.summary.code_count}:{latest_id}"
        return hashlib.sha256(source.encode()).hexdigest()

    async def _save_result(self, scope: str, result: AccountCheckResult) -> None:
        """Persist the latest result without touching announcement state."""
        data = await self._loader()
        results = data.setdefault("_results", {})
        results[self._state_key(scope, result)] = result.to_dict()
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

    def format_repo_report(self, report: RepoContributionReport) -> str:
        """Format a repository contribution report as a Chinese chat message."""
        lines = [f"仓库 {report.repository.slug} 最近 {report.window_hours} 小时绑定成员贡献："]
        if report.contributions:
            for item in report.contributions:
                summary = item.summary
                detail = f"代码活动 {summary.code_count}，普通活动 {summary.ordinary_count}"
                if summary.latest_activity is not None:
                    detail += f"，最近 {summary.latest_activity.event_type}"
                lines.append(f"- {item.account.label} (@{item.account.username})：{detail}")
        else:
            lines.append("- 没有成员在该仓库产生公开活动")
        if report.silent_members:
            lines.append(f"- 无公开活动：{'、'.join(report.silent_members)}")
        if report.failures:
            lines.append(f"- 查询失败：{'；'.join(report.failures)}")
        lines.append(f"共 {report.member_count} 位绑定成员，{len(report.contributions)} 位有贡献。")
        return "\n".join(lines)
