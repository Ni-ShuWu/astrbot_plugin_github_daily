# astrbot_plugin_github_daily

一个用于 AstrBot 群聊的 GitHub 代码活动监督插件：绑定群友的 GitHub 用户名后，查询其最近公开活动，并判断是“正在写代码”“有活动但无法确认”还是“疑似摸鱼”。

## 功能

- `/github_watch add <用户名> [昵称]` 添加监督账户
- `/github_watch remove <用户名>` 移除账户
- `/github_watch list` 查看当前群账户
- `/github_watch check [用户名]` 检查最近活动
- `/github_watch help` 查看帮助
- 可选定时自动检查
- 内置 TTL 缓存、请求冷却、失败重试和 GitHub Token 配置
- 支持群聊白名单，仅白名单群可使用命令或接收自动检查

## 安装

将插件目录放入 AstrBot 的 `data/plugins`（或通过插件管理器安装），安装依赖：

```bash
pip install -r requirements.txt
```

在 AstrBot 插件配置中设置 `github_token`（可选）。建议使用只读的 GitHub Personal Access Token，以提高 API 限额。Token 不要提交到 Git。

在 `allowed_group_ids` 中填写允许使用插件的群聊 ID，例如 `['123456789', '987654321']`。留空时不会在任何群聊中响应命令，也不会执行自动推送；私聊始终不受白名单允许。

## 判定规则

默认将 `PushEvent`、`PullRequestEvent` 和 `PullRequestReviewEvent` 判定为代码相关活动。其他公开事件可能会被记录为普通活动，但不会直接判定为正在写代码。GitHub Events API 只反映近期公开活动，不能代表完整贡献图；私有仓库活动也可能无法获取。

## 更新插件后

更新到新版本后请在 AstrBot 中重新加载或重启插件。插件内部模块使用相对导入，重载时会加载新代码；如果日志出现：

```text
'PluginConfig' object has no attribute ...
```

说明磁盘上的内部模块版本与 `main.py` 不一致，请删除插件目录后重新安装，再重启 AstrBot。

## 开发检查

```bash
py -3 -m compileall -q .
```

## 隐私与限制

插件只查询 GitHub API 返回的公开事件，不绕过 GitHub 权限。群聊中请尊重成员隐私，不要将监督结果作为考勤或惩罚的唯一依据。
