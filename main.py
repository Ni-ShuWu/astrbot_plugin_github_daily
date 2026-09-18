"""AstrBot entry point for the GitHub contribution watcher."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# AstrBot 4.27.x loads main.py directly without adding the plugin directory
# to sys.path, so make sibling packages importable before importing them.
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from github_daily.config import PluginConfig
from github_daily.errors import GitHubApiError, GitHubDailyError
from github_daily.service import ContributionService


class GithubDailyPlugin(Star):
    """Monitor configured GitHub accounts in AstrBot chat scopes."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        raw_config = dict(config or {})
        self._config = PluginConfig.from_mapping(raw_config)
        self._service = ContributionService(self._config, self._load_data, self._save_data)
        self._task: asyncio.Task[None] | None = None
        if self._config.auto_check_enabled:
            self._task = asyncio.create_task(self._monitor_loop())

    @filter.command("github_watch")
    async def github_watch(self, event: AstrMessageEvent, action: str = "help", username: str = "", display_name: str = ""):
        """管理 GitHub 监督：add/remove/list/check/status/help。"""
        if self._config.admin_only and not event.is_admin():
            yield event.plain_result("只有管理员可以管理 GitHub 监督。")
            return
        scope = str(event.get_group_id() or event.get_session_id())
        action = action.lower().strip()
        try:
            if action == "add":
                if not username:
                    yield event.plain_result("用法：/github_watch add <GitHub用户名> [昵称]")
                    return
                account = await self._service.add_account(scope, username, display_name or None)
                yield event.plain_result(f"已开始监督 {account.label}（@{account.username}）。")
            elif action == "remove":
                if not username:
                    yield event.plain_result("用法：/github_watch remove <GitHub用户名>")
                    return
                removed = await self._service.remove_account(scope, username)
                yield event.plain_result("已移除监督账户。" if removed else "未找到该监督账户。")
            elif action == "list":
                accounts = await self._service.list_accounts(scope)
                if not accounts:
                    yield event.plain_result("当前群没有配置监督账户。")
                    return
                yield event.plain_result("当前监督账户：\n" + "\n".join(f"- {item.label} (@{item.username})" for item in accounts))
            elif action in {"check", "status"}:
                accounts = await self._service.list_accounts(scope)
                if not accounts:
                    yield event.plain_result("当前群没有配置监督账户。")
                    return
                targets = [username] if username else [item.username for item in accounts]
                results = [await self._service.check_account(scope, item) for item in targets]
                yield event.plain_result("\n\n".join(self._service.format_result(item) for item in results))
            else:
                yield event.plain_result(self._help_text())
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
        """Periodically check scopes and announce only meaningful changes."""
        while True:
            try:
                await asyncio.sleep(self._config.auto_check_interval_seconds)
                data = await self._load_data()
                for scope in [key for key in data if not key.startswith("_")]:
                    try:
                        await self._service.check_all(scope)
                    except Exception:
                        logger.exception("automatic GitHub check failed for scope %s", scope)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("github_daily monitor loop failed")

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

    @staticmethod
    def _help_text() -> str:
        """Return command help text."""
        return "\n".join([
            "GitHub 监督命令：",
            "/github_watch add <用户名> [昵称] - 添加监督账户",
            "/github_watch remove <用户名> - 移除监督账户",
            "/github_watch list - 查看监督账户",
            "/github_watch check [用户名] - 检查贡献状态",
            "/github_watch help - 查看帮助",
        ])
