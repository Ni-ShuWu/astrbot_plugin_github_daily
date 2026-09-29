# astrbot_plugin_github_daily

一个用于 AstrBot 群聊的 GitHub 代码活动监督插件：绑定群友的 GitHub 用户名后，查询其最近公开活动，并判断是“正在写代码”“有活动但无法确认”还是“疑似摸鱼”。命令支持 `/github_watch` 和简写 `/ghw`，两者完全等价；子命令也支持简写：`add/a`、`remove/rm`、`list/ls`、`check/c`、`detail/d`、`repo/r`、`help/h`。

## 功能

- `/github_watch add <用户名> [昵称]` 绑定 GitHub 账户（无需管理员）
- `/github_watch remove <用户名>` 解绑自己的账户
- `/github_watch list` 查看当前群账户
- `/github_watch check [用户名]` 检查最近活动
- `/github_watch detail <用户名> [条数]` 查看指定用户最新活动的详情（默认合并转发）
- `/github_watch repo <owner/repo>` 查看绑定成员在该仓库的贡献
- `/github_watch help` 查看帮助
- 可选定时自动检查与主动播报（默认关闭）
- 内置 TTL 缓存、请求冷却、失败重试和 GitHub Token 配置
- 支持群聊白名单，仅白名单群可使用命令或接收自动播报
- 在插件设置中分别维护三类结论文案；可选启用语句轮换（`random_conclusion`，默认关闭）

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
| `list` / `check` / `status` / `detail` / `repo` | 由 `allow_public_query` 控制 | 允许 |

两个开关：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `allow_self_bind` | `true` | 关闭后，绑定与解绑都只允许管理员操作 |
| `allow_public_query` | `true` | 关闭后，`list`、`check`、`detail` 和 `repo` 只允许管理员使用 |
| `max_accounts_per_scope` | `20` | 每个群允许绑定的 GitHub 账户上限，达到上限后需先解绑账户 |

`check`、`detail` 和 `repo` 会实际请求 GitHub API 并消耗限额，如果群内查询频繁，可以关闭 `allow_public_query`，只让管理员查询。每群绑定上限用于限制自助绑定规模，减少查询带来的 API 请求量。

昵称会剥离控制字符并截断至 64 个字符。

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

## 活动详情查询

`/github_watch detail <GitHub用户名> [条数]` 展示指定用户**最新的公开活动详情**。不写条数时只看最新 1 条，写了条数就按条数展示最近若干条：

```text
/github_watch detail Ni-ShuWu      # 最新 1 条
/ghw d Ni-ShuWu 5                  # 最新 5 条
/ghw detail 5                      # 引用一条播报消息时，看该账户最新 5 条
```

- 结果默认以**合并转发**消息发送，一条活动一个节点，避免刷屏。只有 OneBot 系适配器（`aiocqhttp`、`satori`）能渲染合并转发，其他平台会自动退回普通文本；把 `detail_use_forward` 设为 `false` 可以强制使用普通文本。
- 每条活动会尽量还原该事件的上下文：事件类型（中文说明）、仓库、时间（本地时区与相对时间）、分支/标签、提交数与提交信息、Issue/PR 标题与编号、跳转链接。
- 用户名既可以是本群已绑定的账户，也可以是任意 GitHub 用户名；命中已绑定账户时显示其昵称，也可以直接用昵称代替用户名。
- 也可以**引用一条定时播报（或 `/github_watch check`）的消息**，再发送 `/github_watch detail [条数]`，插件会从被引用的消息里解析出 `(@用户名)` 并展示该账户的详情。
- `detail` 不受 `window_hours` 限制：它展示的是最新公开事件，即使该事件早于检查窗口（GitHub 公开 Events API 最多可回溯约 90 天）。
- 权限由 `allow_public_query` 控制，缓存与请求冷却和 `check` 共用，因此刚查过 `check` 时不会重复请求 GitHub API。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `detail_default_entries` | `1` | `detail` 不写条数时默认展示的活动条数 |
| `detail_max_entries` | `20` | `detail` 单次最多允许展示的活动条数 |
| `detail_use_forward` | `true` | `detail` 结果使用合并转发消息发送 |

## API 额度

GitHub 未认证请求的限额是 **60 次/小时**（按出口 IP 计），填写 `github_token` 后提升到 **5000 次/小时**。插件按下面的规则把请求数压到最低：

- **一个账户一次抓取只花 1 次请求。** Events API 一次最多返回 100 条公开活动，所以 `detail` 的条数（`detail 1` 与 `detail 20` 请求数相同）和 `check` 的时间窗口都**不会**增加请求数。
- **缓存优先。** 同一账户在 `cache_ttl_seconds`（默认 300 秒）内重复查询直接命中缓存；`check`、`detail`、`repo` 共用同一份缓存，所以刚查过 `check` 再 `detail` 不会产生新请求。
- **并发合并。** 同一账户的多个并发查询只发出一次请求。
- **额度预检。** 插件记录响应头中的 `X-RateLimit-Remaining` / `X-RateLimit-Reset`；额度耗尽时**不再发出注定失败的请求**，改用手边最近的缓存数据（并在输出里注明“数据来源：本地缓存”），同时告诉你要等多久、以及配置 `github_token` 可以提升限额。
- **失败降级。** GitHub 报错或额度耗尽时，过期未超过 30 分钟的旧缓存会继续作答，而不是直接报错。
- **定时播报整轮跳过。** 额度耗尽时跳过整轮检查并写日志，而不是逐个账户刷失败。
- **内存有上限。** 缓存只保留最近 200 个账户（LRU），避免 `detail` 查询任意用户名把内存撑大。

请求数估算：一轮定时检查约等于**去重后的绑定账户数**；`repo` 是每个绑定成员 1 次。`detail` 的头部会直接显示当前剩余额度，方便群里自己观察消耗。

> 说明：GitHub 对带 `If-None-Match` 的条件请求返回的 `304 Not Modified` **同样计入限额**（本项目实测确认：连续请求的 `x-ratelimit-remaining` 每次都 −1），所以插件没有靠 ETag “省额度”，而是从源头减少请求。

## 自动播报

自动播报默认关闭。开启方式：将 `auto_check_enabled` 设为 `true`。相关配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `auto_check_enabled` | `false` | 是否启用定时自动检查与播报 |
| `auto_check_interval_seconds` | `3600` | 检查间隔（秒），最小 60 |
| `announce_only_on_change` | `true` | 仅当状态或活动数量变化时播报 |
| `min_announce_interval_seconds` | `3600` | 同一账户两次播报的最短间隔（秒） |

自动播报需要一个已知的会话来源，因此插件会在该群至少成功执行过一次 `/github_watch` 命令后，才开始向该群推送。

一轮自动检查会给每个（去重后的）绑定账户发 1 次请求。如果白名单群很多、绑定账户很多，又没配置 `github_token`，60 次/小时很容易用尽；此时插件会跳过该轮检查并写日志，等额度恢复后自动继续。多群共用同一进程时，建议配置 `github_token`。

## 判定规则

默认将 `PushEvent`、`PullRequestEvent` 和 `PullRequestReviewEvent` 判定为代码相关活动。其他公开事件可能会被记录为普通活动，但不会直接判定为正在写代码。GitHub Events API 只反映近期公开活动，不能代表完整贡献图；私有仓库活动也可能无法获取。

## 结论文案自定义

在 AstrBot 插件管理页中，可以分别编辑三种状态的结论文案：

| 配置项 | 状态 |
| --- | --- |
| `conclusion_coding_sentences` | 正在写代码 |
| `conclusion_active_sentences` | 有 GitHub 活动，但无法确认正在写代码 |
| `conclusion_idle_sentences` | 最近没有检测到公开活动 |

每项使用 AstrBot 的字典编辑器。每一行的键是唯一条目标识，值是要发送的语句；可在“修改”对话框中逐条新增、修改和删除，值应为字符串。配置不限制条目数量。键名 `default` 对应固定文案：关闭 `random_conclusion` 时使用它；若删掉该键，则改用该状态第一条有效语句。将该状态的字典清空时，会安全回退到插件内置固定文案。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `random_conclusion` | `false` | 打开后在当前状态的全部有效文案中使用洗牌袋轮换；关闭时使用该状态的 `default` 文案 |

- 三个状态的语句池完全独立，不会把“正在写代码”的状态配上“疑似摸鱼”文案。
- 每种状态默认 12 条（含原固定文案），覆盖中性、编译器、编程语言、开源、雌小鬼与轻嘲讽六种语气。
- 轮换时一轮之内不重复，也不会与上一条重复；保存配置并重载插件后生效。
- 仅自定义结论措辞，不改变判定依据、数据展示或查询行为；默认配置下，关闭轮换时的消息输出与旧版本保持一致。

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

