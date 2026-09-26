"""AstrBot entry point for the GitHub contribution watcher."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


def _import_bundled_modules() -> tuple[type, type, type, type]:
    """Import bundled modules in a way that survives AstrBot plugin reloads.

    AstrBot purges plugin modules from ``sys.modules`` on reload, but only for
    names under its ``data.plugins.<plugin>`` prefix. Importing the bundle as a
    relative subpackage keeps it inside that namespace, so reloads pick up new
    code instead of reusing classes cached by an earlier plugin version.
    """
    if __package__:
        try:
            from .github_daily.config import PluginConfig
            from .github_daily.errors import GitHubDailyError, PermissionDeniedError
            from .github_daily.service import ContributionService

            return PluginConfig, GitHubDailyError, PermissionDeniedError, ContributionService
        except ImportError:
            pass  # Parent package is unavailable; fall through to path import.

    plugin_root = Path(__file__).resolve().parent
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    for module_name in [
        name for name in list(sys.modules)
        if name == "github_daily" or name.startswith("github_daily.")
    ]:
        del sys.modules[module_name]

    from github_daily.config import PluginConfig
    from github_daily.errors import GitHubDailyError, PermissionDeniedError
    from github_daily.service import ContributionService

    return PluginConfig, GitHubDailyError, PermissionDeniedError, ContributionService


PluginConfig, GitHubDailyError, PermissionDeniedError, ContributionService = _import_bundled_modules()


class GithubDailyPlugin(Star):
    """Monitor configured GitHub accounts in AstrBot chat scopes."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        raw_config = dict(config or {})
        self._config = PluginConfig.from_mapping(raw_config)
        if not hasattr(self._config, "is_group_allowed"):
            raise RuntimeError(
                "github_daily 内部模块版本不一致，请删除插件目录后重新安装并重启 AstrBot。",
            )
        self._service = ContributionService(self._config, self._load_data, self._save_data)
        self._task: asyncio.Task[None] | None = None
        if self._config.auto_check_enabled:
            self._task = asyncio.create_task(self._monitor_loop())

    @filter.command("github_watch", alias={"ghw"})
    async def github_watch(self, event: AstrMessageEvent, action: str = "help", target: str = "", extra: str = ""):
        """管理 GitHub 监督：add/remove/list/check/repo/status/help。"""
        group_id = str(event.get_group_id() or "").strip()
        if not self._config.is_group_allowed(group_id):
            yield event.plain_result("当前群聊不在 GitHub 监督白名单内。")
            return
        scope = group_id
        action = action.lower().strip()
        action = {
            "a": "add",
            "rm": "remove",
            "ls": "list",
            "c": "check",
            "s": "status",
            "r": "repo",
            "h": "help",
        }.get(action, action)
        is_admin = event.is_admin()
        try:
            if not self._is_action_allowed(action, is_admin):
                yield event.plain_result(self._denied_text(action))
                return
            await self._service.remember_scope(scope, event.unified_msg_origin, group_id)
            if action == "add":
                if not target:
                    yield event.plain_result("用法：/github_watch add <GitHub用户名> [昵称]")
                    return
                account = await self._service.add_account(
                    scope,
                    target,
                    extra or None,
                    owner_id=event.get_sender_id(),
                    is_admin=is_admin,
                )
                yield event.plain_result(f"已开始监督 {account.label}（@{account.username}）。")
            elif action == "remove":
                if not target:
                    yield event.plain_result("用法：/github_watch remove <GitHub用户名>")
                    return
                removed = await self._service.remove_account(
                    scope,
                    target,
                    actor_id=event.get_sender_id(),
                    is_admin=is_admin,
                )
                yield event.plain_result("已移除监督账户。" if removed else "未找到该监督账户。")
            elif action == "list":
                accounts = await self._service.list_accounts(scope)
                if not accounts:
                    yield event.plain_result("当前群没有配置监督账户。")
                    return
                yield event.plain_result("当前监督账户：\n" + "\n".join(f"- {item.label} (@{item.username})" for item in accounts))
            elif action == "repo":
                if not target:
                    yield event.plain_result("用法：/github_watch repo <owner/repo>")
                    return
                if not await self._service.list_accounts(scope):
                    yield event.plain_result("当前群没有配置监督账户。")
                    return
                report = await self._service.check_repository(scope, target)
                yield event.plain_result(self._service.format_repo_report(report))
            elif action in {"check", "status"}:
                accounts = await self._service.list_accounts(scope)
                if not accounts:
                    yield event.plain_result("当前群没有配置监督账户。")
                    return
                targets = [target] if target else [item.username for item in accounts]
                results = [await self._service.check_account(scope, item) for item in targets]
                yield event.plain_result("\n\n".join(self._service.format_result(item) for item in results))
            else:
                yield event.plain_result(self._help_text())
        except PermissionDeniedError as exc:
            yield event.plain_result(str(exc))
        except (GitHubDailyError, RuntimeError, ValueError) as exc:
            yield event.plain_result(f"操作失败：{exc}")
        except Exception:
            logger.exception("github_daily command failed")
            yield event.plain_result("操作失败：插件遇到未预期错误，请查看日志。")

    async def terminate(self) -> None:
        """Stop the periodic monitor task when the plugin unloads."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        """Periodically check whitelisted scopes and announce meaningful changes."""
        while True:
            try:
                await asyncio.sleep(self._config.auto_check_interval_seconds)
                await self._run_auto_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("github_daily monitor loop failed")

    async def _run_auto_check(self) -> None:
        """Check every eligible scope once and push the results that matter."""
        targets = await self._service.auto_check_targets(self._config.is_group_allowed)
        for scope, umo in targets:
            results, failures = await self._service.check_all(scope)
            for failure in failures:
                logger.warning("automatic GitHub check failed for %s: %s", scope, failure)
            for result in results:
                try:
                    if not await self._service.should_announce(scope, result):
                        continue
                    chain = MessageChain().message(self._service.format_result(result))
                    await self.context.send_message(umo, chain)
                except Exception:
                    logger.exception(
                        "failed to announce GitHub status for %s in scope %s",
                        result.account.username,
                        scope,
                    )

    async def _load_data(self) -> dict[str, list[dict]]:
        """Load plugin data from AstrBot's asynchronous KV store."""
        raw = await self.get_kv_data("watch_data", {})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        return raw if isinstance(raw, dict) else {}

    async def _save_data(self, data: dict[str, list[dict]]) -> None:
        """Persist plugin data to AstrBot's asynchronous KV store."""
        await self.put_kv_data("watch_data", data)

    def _is_action_allowed(self, action: str, is_admin: bool) -> bool:
        """Return whether the sender may run an action under the current config."""
        if is_admin:
            return True
        if action in {"add", "remove"}:
            return self._config.allow_self_bind
        if action in {"check", "status", "list", "repo"}:
            return self._config.allow_public_query
        return True  # help and unknown actions only print usage.

    @staticmethod
    def _denied_text(action: str) -> str:
        """Explain why a non-admin action was refused."""
        if action in {"add", "remove"}:
            return "当前配置不允许自助绑定 GitHub 账户，请联系管理员操作。"
        if action in {"check", "status", "repo"}:
            return "当前配置仅允许管理员查询 GitHub 状态。"
        if action == "list":
            return "当前配置仅允许管理员查看监督列表。"
        return "当前配置不允许该操作。"

    @staticmethod
    def _help_text() -> str:
        """Return command help text."""
        return "\n".join([
            "GitHub 监督命令（/ghw 为 /github_watch 简写）：",
            "/github_watch add/a <用户名> [昵称] - 绑定自己的 GitHub 账户",
            "/github_watch remove/rm <用户名> - 解绑（本人或管理员）",
            "/github_watch list/ls - 查看监督账户",
            "/github_watch check/status/c/s [用户名] - 检查贡献状态",
            "/github_watch repo/r <owner/repo> - 查看绑定成员在该仓库的贡献",
            "/github_watch help/h - 查看帮助",
        ])
