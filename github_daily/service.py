"""Application service for account management and contribution checks."""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Sequence

from astrbot.api import logger

from .cache import ActivityCache
from .classifier import classify_summary, filter_by_repository, summarize_activities
from .conclusions import DEFAULT_CONCLUSIONS, ConclusionPicker
from .config import PluginConfig
from .errors import AccountNotFoundError, GitHubApiError, InvalidAccountError, PermissionDeniedError
from .github_adapter import GitHubAdapter
from .models import (
    AccountCheckResult,
    GitHubActivity,
    RepoContributionReport,
    RepositoryRef,
    WatchedAccount,
    WatchState,
)
from .rate_limit import RateLimitState

PluginData = dict[str, Any]
PersistLoader = Callable[[], Awaitable[PluginData]]
PersistSaver = Callable[[PluginData], Awaitable[None]]

#: Stop requesting when GitHub reports this few units left, so a burst can still
#: report the real reason instead of tripping over an empty budget.
RATE_LIMIT_RESERVE = 0

#: Human-readable names for the event types rendered by ``detail``.
DETAIL_EVENT_LABELS = {
    "PushEvent": "推送提交",
    "CreateEvent": "创建分支/标签",
    "DeleteEvent": "删除分支/标签",
    "PullRequestEvent": "Pull Request",
    "PullRequestReviewEvent": "PR 代码审查",
    "PullRequestReviewCommentEvent": "PR 审查评论",
    "PullRequestReviewThreadEvent": "PR 审查线程",
    "PullRequestReviewThread": "PR 审查线程",
    "IssuesEvent": "Issue",
    "IssueCommentEvent": "Issue 评论",
    "CommitCommentEvent": "提交评论",
    "ReleaseEvent": "发布 Release",
    "ForkEvent": "Fork 仓库",
    "WatchEvent": "Star 仓库",
    "PublicEvent": "仓库转为公开",
    "MemberEvent": "成员变动",
    "GollumEvent": "编辑 Wiki",
    "SponsorshipEvent": "赞助",
}

#: Chinese verbs for the ``payload.action`` values GitHub reports.
DETAIL_ACTION_LABELS = {
    "opened": "打开",
    "closed": "关闭",
    "reopened": "重新打开",
    "created": "创建",
    "deleted": "删除",
    "edited": "编辑",
    "published": "发布",
    "updated": "更新",
    "started": "Star",
    "added": "添加",
    "removed": "移除",
    "merged": "合并",
    "submitted": "提交",
    "dismissed": "驳回",
    "assigned": "指派",
    "unassigned": "取消指派",
    "labeled": "加标签",
    "unlabeled": "去标签",
    "pinned": "置顶",
    "unpinned": "取消置顶",
    "locked": "锁定",
    "unlocked": "解锁",
    "transferred": "转移",
    "milestoned": "关联里程碑",
    "demilestoned": "取消里程碑",
    "review_requested": "请求审查",
    "review_request_removed": "撤回审查请求",
    "ready_for_review": "标记可审查",
    "converted_to_draft": "转为草稿",
    "synchronize": "同步提交",
}

DETAIL_REF_TYPE_LABELS = {"branch": "分支", "tag": "标签", "repository": "仓库"}

#: ``(@name)`` is emitted by ``format_result`` and ``format_repo_report``, so a
#: quoted broadcast tells us which account the reply is about.
QUOTED_USERNAME_PATTERN = re.compile(r"\(@([A-Za-z0-9-]{1,39})\)")
MENTION_PATTERN = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9-]{0,38})")


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
        """Initialize service dependencies and per-user request coordination."""
        self.config = config
        self._loader = loader
        self._saver = saver
        self._adapter = GitHubAdapter(config.github_token, config.request_timeout_seconds, config.max_retries)
        self._cache: ActivityCache = ActivityCache(config.cache_ttl_seconds, config.request_cooldown_seconds)
        # The status-specific pools come from plugin settings. Each plugin reload
        # starts a fresh shuffle bag for the newly saved configuration.
        self._conclusions = ConclusionPicker(
            {status: config.conclusion_pool(status) for status in DEFAULT_CONCLUSIONS}
        )
        # One lock per username, so several simultaneous queries about the same
        # account still cost a single GitHub request.
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        # Remember which exhausted window was already logged, to avoid spam.
        self._budget_warning_reset: datetime | None = None

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
        if existing is None and len(accounts) >= self.config.max_accounts_per_scope:
            raise InvalidAccountError("该群绑定账户已达上限")
        cleaned_name = "".join(
            char for char in (display_name or "").strip()
            if not unicodedata.category(char).startswith("C")
        )[:64]
        account = WatchedAccount(
            username=normalized,
            display_name=cleaned_name or None,
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

    async def find_account(self, scope: str, username: str) -> WatchedAccount | None:
        """Return the watched account matching a username, ignoring case."""
        target = str(username or "").strip().lower()
        if not target:
            return None
        for account in await self.list_accounts(scope):
            if account.username.lower() == target:
                return account
        return None

    async def fetch_activities(
        self,
        username: str,
        detail_limit: int = 1,
    ) -> tuple[list[GitHubActivity], bool]:
        """Fetch a user's public events, newest first, without a time window.

        ``detail`` deliberately ignores ``window_hours``: it promises the newest
        public activity, which must still be shown when it is older than the
        configured check window. GitHub itself keeps only about 90 days of
        public events.

        The boolean is ``True`` when the events came from the stale cache
        because GitHub could not be queried, so callers can say so.
        """
        normalized = str(username or "").strip()
        if not self.USERNAME_PATTERN.fullmatch(normalized):
            raise InvalidAccountError("GitHub 用户名格式无效")
        activities, stale = await self._get_events(normalized)
        activities = sorted(activities, key=lambda item: item.created_at, reverse=True)
        if not stale:
            limit = max(1, detail_limit)
            activities = await self._resolve_push_commit_counts(activities[:limit]) + activities[limit:]
            self._cache.update(normalized.lower(), activities)
        return activities, stale

    async def _resolve_push_commit_counts(
        self,
        activities: Sequence[GitHubActivity],
    ) -> list[GitHubActivity]:
        """Fill missing push commit counts using the GitHub compare endpoint."""
        resolved: list[GitHubActivity] = []
        for activity in activities:
            if activity.event_type != "PushEvent" or activity.commit_count is not None:
                resolved.append(activity)
                continue
            count = await self._adapter.fetch_push_commit_count(
                activity.repository or "",
                activity.before or "",
                activity.head or "",
            )
            resolved.append(replace(activity, commit_count=count))
        return resolved

    async def check_account(self, scope: str, username: str) -> AccountCheckResult:
        """Check one configured account and persist its latest state."""
        account = await self.find_account(scope, username)
        if account is None:
            raise AccountNotFoundError(f"未配置 GitHub 账户：{username}")
        activities, stale = await self._get_events(account.username)
        now = datetime.now(timezone.utc)
        summary = summarize_activities(activities, window_hours=self.config.window_hours, code_event_types=self.config.code_event_types, now=now)
        is_coding, status = classify_summary(summary)
        result = AccountCheckResult(account, now, self.config.window_hours, summary, is_coding, status, stale=stale)
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
                activities, stale = await self._get_events(account.username)
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
                AccountCheckResult(account, now, self.config.window_hours, summary, is_coding, status, stale=stale),
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

    async def _get_events(self, username: str) -> tuple[list[GitHubActivity], bool]:
        """Return a user's events plus whether a stale cache had to answer.

        Three things keep the GitHub budget small:

        1. a fresh cache entry is returned without touching the network;
        2. concurrent queries for the same user share one request through a
           per-username lock;
        3. a request that GitHub would certainly reject - because the advertised
           budget is spent - is never sent, and the last cached answer is reused
           instead so a rate-limited API degrades to older data, not an error.
        """
        key = username.lower()
        cached = self._cache.get(key)
        if cached is not None:
            return cached, False

        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                # A concurrent caller may have filled the cache while we waited.
                cached = self._cache.get(key)
                if cached is not None:
                    return cached, False
                # Report a spent budget before the per-user cooldown: it is the
                # reason the user can actually act on.
                if self._budget_is_spent():
                    return await self._stale_or_raise(key)
                waited = self._cache.cooldown_remaining(key)
                if waited > 0:
                    # An expired-but-recent answer beats an error, and it also
                    # keeps impatient users from retrying and costing more.
                    fallback = self._cache.get_stale(key)
                    if fallback is not None:
                        return fallback, True
                    raise RuntimeError(f"请求过于频繁，请 {waited:.0f} 秒后再试")
                self._cache.mark_requested(key)
                try:
                    events = await self._adapter.fetch_user_events(username)
                except GitHubApiError as exc:
                    # A failing GitHub must not cost the user the answer we
                    # already have; report it as stale instead.
                    fallback = self._cache.get_stale(key)
                    if fallback is None:
                        raise
                    logger.warning(
                        "github_daily: reusing cached events for %s after GitHub error: %s",
                        username,
                        exc,
                    )
                    return fallback, True
                self._cache.set(key, events)
                return events, False
        finally:
            users = self._lock_users[key] - 1
            if users == 0:
                self._lock_users.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._lock_users[key] = users

    def _budget_is_spent(self) -> bool:
        """Return whether GitHub's advertised budget forbids another request."""
        if self.rate_limit_wait_seconds() <= 0:
            return False
        # Log at most once per rate-limit window instead of once per account.
        reset = self._adapter.rate_limit.state.reset_at
        if reset != self._budget_warning_reset:
            self._budget_warning_reset = reset
            logger.warning(
                "github_daily: GitHub request budget exhausted until %s",
                reset.isoformat() if reset else "the next window",
            )
        return True

    async def _stale_or_raise(self, key: str) -> tuple[list[GitHubActivity], bool]:
        """Serve the last cached answer, or explain when the budget returns."""
        fallback = self._cache.get_stale(key)
        if fallback is not None:
            return fallback, True
        wait = self.rate_limit_wait_seconds()
        reset = self._adapter.rate_limit.state.reset_at
        when = f"，预计 {self.format_wait(wait)}后恢复" if wait > 0 else ""
        if reset is not None:
            when += f"（重置时间 {reset.astimezone().strftime('%H:%M:%S')}）"
        raise GitHubApiError(
            f"GitHub API 请求额度已用尽{when}。"
            "可在插件配置里填写 github_token，把限额从 60 次/小时提升到 5000 次/小时。",
        )

    def rate_limit_wait_seconds(self) -> float:
        """Return how long GitHub asks us to wait before the next request."""
        return self._adapter.rate_limit.wait_seconds(RATE_LIMIT_RESERVE)

    def rate_limit_snapshot(self) -> RateLimitState:
        """Return the most recent ``X-RateLimit-*`` snapshot."""
        return self._adapter.rate_limit.state

    def format_quota(self) -> str:
        """Describe the remaining GitHub budget, or ``""`` while still unknown.

        Surfacing this is the simplest way for a group to see how much of the
        shared hourly allowance its queries are consuming.
        """
        state = self.rate_limit_snapshot()
        if not state.known or state.limit <= 0:
            return ""
        quota = f"API 额度：剩余 {state.remaining}/{state.limit}"
        if state.reset_at is not None:
            quota += f"，{state.reset_at.astimezone().strftime('%H:%M:%S')} 重置"
        return quota

    @staticmethod
    def format_wait(seconds: float) -> str:
        """Describe a wait duration in Chinese, for chat output."""
        if seconds <= 0:
            return "稍等片刻"
        if seconds < 60:
            return f"{seconds:.0f} 秒"
        minutes = int(seconds // 60) + (1 if seconds % 60 else 0)
        if minutes < 60:
            return f"{minutes} 分钟"
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"

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
        """Format a check result as a concise Chinese chat message.

        The fixed sentence is configured per status; when ``random_conclusion``
        is on, a sentence is drawn from that status's configured pool. The
        ``(@username)`` marker is what lets ``detail`` resolve a quoted
        announcement back to the account it talks about.
        """
        summary = result.summary
        label = result.account.label
        username = result.account.username
        if self.config.random_conclusion:
            conclusion = self._conclusions.pick(result.status)
        else:
            conclusion = self.config.fixed_conclusion(result.status)
        lines = [
            f"{label} (@{username}) 最近 {result.window_hours} 小时 GitHub 状态：",
            f"- 活动总数：{summary.total_count}",
            f"- 代码相关活动：{summary.code_count}",
            f"- 普通活动：{summary.ordinary_count}",
            f"结论：{conclusion}",
        ]
        if summary.latest_activity:
            latest = summary.latest_activity
            lines.insert(4, f"- 最近活动：{latest.event_type} / {latest.repository or '未知仓库'}")
        if result.stale:
            lines.append("- 数据来源：本地缓存（GitHub 暂时不可用或额度已用尽，可能不是最新）。")
        lines.append(f"引用本条消息并发送 /github_watch detail 可查看 @{username} 的活动详情。")
        return "\n".join(lines)

    def build_detail(
        self,
        activities: Sequence[GitHubActivity],
        *,
        username: str,
        label: str,
        limit: int,
        stale: bool = False,
    ) -> list[str]:
        """Render the newest ``limit`` activities as forward-message blocks.

        The first block describes the query and every following block describes
        one activity. Staying platform independent here lets ``main.py`` turn
        the blocks into a merged forward message, or fall back to plain text.
        """
        selected = list(activities)[: max(1, limit)]
        now = datetime.now(timezone.utc)
        header = [
            f"GitHub 活动详情 · {label} (@{username})",
            f"显示最近 {len(selected)} 条，共 {len(activities)} 条可查",
            f"生成时间：{self._format_local(now)}",
            "数据来源：GitHub 公开 Events API（仅公开事件，最多可回溯约 90 天）",
        ]
        if stale:
            header.append("注意：本次使用本地缓存（GitHub 暂时不可用或额度已用尽），内容可能不是最新。")
        quota = self.format_quota()
        if quota:
            header.append(quota)
        blocks = ["\n".join(header)]
        blocks.extend(
            self.describe_activity(index, activity, now)
            for index, activity in enumerate(selected, start=1)
        )
        return blocks

    def describe_activity(
        self,
        index: int,
        activity: GitHubActivity,
        now: datetime | None = None,
    ) -> str:
        """Render one GitHub activity as a multi-line detail block."""
        current = now or datetime.now(timezone.utc)
        headline: list[str] = []
        if activity.action:
            headline.append(DETAIL_ACTION_LABELS.get(activity.action, activity.action))
        headline.append(DETAIL_EVENT_LABELS.get(activity.event_type, activity.event_type))
        if activity.number:
            headline.append(f"#{activity.number}")
        lines = [
            f"#{index} {' '.join(headline)}",
            f"仓库：{activity.repository or '未知仓库'}",
            f"时间：{self._format_local(activity.created_at)}（{self._format_relative(activity.created_at, current)}）",
        ]
        if activity.title:
            lines.append(f"标题：{activity.title}")
        if activity.ref:
            ref_type = str(activity.ref_type or "")
            if not ref_type and activity.event_type == "PushEvent":
                ref_type = "branch"  # Results persisted by older versions.
            lines.append(f"{DETAIL_REF_TYPE_LABELS.get(ref_type, '引用')}：{self._short_ref(activity.ref)}")
        if activity.event_type == "PushEvent":
            count = str(activity.commit_count) if activity.commit_count is not None else "未知"
            lines.append(f"提交数：{count}")
        if activity.commits:
            lines.append("提交信息：")
            lines.extend(f"  · {message}" for message in activity.commits)
        elif activity.message:
            lines.append(f"提交信息：{activity.message}")
        if activity.url:
            lines.append(f"链接：{activity.url}")
        return "\n".join(lines)

    @staticmethod
    def username_from_text(text: str, accounts: Iterable[WatchedAccount] = ()) -> str:
        """Guess which GitHub account a quoted chat message talks about.

        ``(@name)`` wins because the plugin writes it into every broadcast, then
        a bound account's display name, and finally a bare ``@name`` mention —
        but only in text that looks like this plugin's own output.
        """
        blob = str(text or "").strip()
        if not blob:
            return ""
        match = QUOTED_USERNAME_PATTERN.search(blob)
        if match:
            return match.group(1)
        for account in sorted(accounts, key=lambda item: len(item.label), reverse=True):
            if account.label and account.label in blob:
                return account.username
        if "github" in blob.lower():
            match = MENTION_PATTERN.search(blob)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _short_ref(value: str) -> str:
        """Strip the ``refs/heads/`` / ``refs/tags/`` prefix from a git ref."""
        text = str(value or "").strip()
        for prefix in ("refs/heads/", "refs/tags/"):
            if text.startswith(prefix):
                return text[len(prefix):]
        return text

    @staticmethod
    def _format_local(value: datetime) -> str:
        """Render a UTC timestamp in the host's local timezone."""
        local = value.astimezone()
        stamp = local.strftime("%Y-%m-%d %H:%M:%S")
        offset = local.utcoffset()
        if offset is None:
            return stamp
        seconds = int(offset.total_seconds())
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        return f"{stamp} (UTC{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d})"

    @staticmethod
    def _format_relative(value: datetime, now: datetime) -> str:
        """Describe how long ago an activity happened, in Chinese."""
        seconds = max(0, int((now - value).total_seconds()))
        if seconds < 60:
            return "刚刚"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} 小时前"
        days = hours // 24
        if days < 30:
            return f"{days} 天前"
        return f"{days // 30} 个月前"

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
        if any(item.stale for item in report.contributions):
            lines.append("- 注意：部分数据来自本地缓存（GitHub 暂时不可用或额度已用尽），可能不是最新。")
        lines.append(f"共 {report.member_count} 位绑定成员，{len(report.contributions)} 位有贡献。")
        return "\n".join(lines)
