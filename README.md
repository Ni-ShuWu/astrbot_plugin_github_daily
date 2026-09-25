# astrbot_plugin_github_daily

一个用于 AstrBot 群聊的 GitHub 代码活动监督插件：绑定群友的 GitHub 用户名后，查询其最近公开活动，并判断是“正在写代码”“有活动但无法确认”还是“疑似摸鱼”。

## 功能

- `/github_watch add <用户名> [昵称]` 绑定 GitHub 账户（无需管理员）
- `/github_watch remove <用户名>` 解绑自己的账户
- `/github_watch list` 查看当前群账户
- `/github_watch check [用户名]` 检查最近活动
- `/github_watch repo <owner/repo>` 查看绑定成员在该仓库的贡献
- `/github_watch help` 查看帮助
- 可选定时自动检查与主动播报（默认关闭）
- 内置 TTL 缓存、请求冷却、失败重试和 GitHub Token 配置
- 支持群聊白名单，仅白名单群可使用命令或接收自动播报

## 安装

将插件目录放入 AstrBot 的 `data/plugins`（或通过插件管理器安装），安装依赖：

```bash
pip install -r requirements.txt
```

在 AstrBot 插件配置中设置 `github_token`（可选）。建议使用只读的 GitHub Personal Access Token，以提高 API 限额。Token 不要提交到 Git。

在 `allowed_group_ids` 中填写允许使用插件的群聊 ID，例如 `['123456789', '987654321']`。

白名单只包含群聊，行为如下：

- 群聊 ID 在白名单内：可以使用命令，并会收到自动播报。
- 群聊 ID 不在白名单内：命令不响应，也不会收到任何播报。
- 私聊：不响应命令，也不会收到播报。私聊没有群聊 ID，因此无法加入白名单。
- 白名单留空：插件在任何会话中都不工作。

## 权限

每个绑定都属于执行绑定操作的群成员本人。管理员可以管理所有人的绑定。

| 操作 | 普通群成员 | 管理员 |
| --- | --- | --- |
| 绑定自己的账户 | 允许 | 允许 |
| 解绑自己的账户 | 允许 | 允许 |
| 重新绑定已有他人账户 | 拒绝 | 允许 |
| `list` / `check` / `status` / `repo` | 由 `allow_public_query` 控制 | 允许 |

两个开关：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `allow_self_bind` | `true` | 关闭后，绑定与解绑都只允许管理员操作 |
| `allow_public_query` | `true` | 关闭后，`list`、`check` 和 `repo` 只允许管理员使用 |

`check` 和 `repo` 会实际请求 GitHub API 并消耗限额，如果群内查询频繁，可以关闭 `allow_public_query`，只让管理员查询。

由旧版本创建的绑定没有归属者信息，这类账户只能由管理员解绑。

## 仓库贡献查询

`/github_watch repo <owner/repo>` 汇总**当前群全部绑定成员**在该仓库的贡献，时间窗口与 `check` 一致（`window_hours`，默认 24 小时）：

```text
仓库 Ni-ShuWu/astrbot_plugin_github_daily 最近 24 小时绑定成员贡献：
- 张三 (@zhangsan)：代码活动 3，普通活动 1，最近 PushEvent
- 无公开活动：李四
共 2 位绑定成员，1 位有贡献。
```

- 仓库参数可写成 `owner/repo`、`https://github.com/owner/repo`（带 `/tree/main` 后缀或 `user:token@` 前缀也可）、`git@github.com:owner/repo.git`，只支持 github.com。
- 只统计绑定成员的公开事件，匹配仓库名时不区分大小写；代码活动与普通活动的划分沿用 `code_event_types`。
- 结果按代码活动数排序，无贡献的成员单独列在“无公开活动”之后；某个成员拉取失败只显示为一行的“查询失败”，不影响其他成员。
- 查询复用与 `check` 相同的缓存和冷却，因此刚查过 `check` 时不会重复请求 GitHub API。

## 自动播报

自动播报默认关闭。开启方式：将 `auto_check_enabled` 设为 `true`。相关配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `auto_check_enabled` | `false` | 是否启用定时自动检查与播报 |
| `auto_check_interval_seconds` | `3600` | 检查间隔（秒），最小 60 |
| `announce_only_on_change` | `true` | 仅当状态或活动数量变化时播报 |
| `min_announce_interval_seconds` | `3600` | 同一账户两次播报的最短间隔（秒） |

自动播报需要一个已知的会话来源，因此插件会在该群至少成功执行过一次 `/github_watch` 命令后，才开始向该群推送。

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

`github_token` 仅用于请求 GitHub API，不会出现在任何对外输出中，插件导出的配置会把该字段替换为 `***`。

