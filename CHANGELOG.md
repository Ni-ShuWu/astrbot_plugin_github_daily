# Changelog

## 1.4.0 - 2026-09-25

- 新增 `/github_watch repo <owner/repo>`：汇总群内全部绑定成员在该仓库最近 `window_hours` 小时的公开贡献，按代码活动排序，并单独列出无活动成员。
- 仓库参数支持 `owner/repo`、github.com 链接（可带 `tree/...` 后缀或 `user:token@` 前缀）和 `git@github.com:` 远程地址，非 github.com 主机与非法格式会给出明确提示。
- 单个成员拉取失败只记录为该成员的“查询失败”，不影响其他成员的结果。
- `repo` 复用 `check` 的活动缓存，遵循同一账户的请求冷却；权限由 `allow_public_query` 控制。
- 命令参数由 `username` / `display_name` 改名为 `target` / `extra`，以覆盖账户名与仓库名两种取值（位置不变，不影响用法）。
- 修正配置 schema 中 `allow_public_query` 的描述，明确涵盖 `repo` 查询。
- 绑定昵称会剥离控制字符并限制为最多 64 个字符；新增 `max_accounts_per_scope` 配置，每群默认最多绑定 20 个账户。
- 拒绝 owner 或仓库名为 `.`、`..` 的仓库参数。

## 1.3.0 - 2026-09-18

- 非管理员现在可以自助绑定和解绑自己的 GitHub 账户，不再要求 op 权限。
- 绑定记录归属者，非管理员无法解绑或覆盖他人的绑定，管理员可管理全部绑定。
- 旧的 `admin_only` 配置拆分为 `allow_self_bind` 与 `allow_public_query`，权限语义更明确。
- 越权操作返回明确提示，而不是笼统的“只有管理员可以管理”。

## 1.2.0 - 2026-09-18

- 实现定时自动播报：定时检查后会主动向白名单群推送结果，此前只有配置项没有实际推送。
- `announce_only_on_change` 与 `min_announce_interval_seconds` 现在真正生效。
- 单个账户检查失败不再中断同群其他账户的检查。
- 配置解析改为严格类型转换，字符串 `"false"` 不再被当成 `true`，非法数字回退到默认值。
- `github_token` 不再出现在 `repr` 和配置导出中，导出时替换为 `***`。
- 修正 README 中私聊与白名单的说明，并补充自动播报配置说明。
- 新增插件市场图标 `logo.png`。

## 1.1.1 - 2026-09-18

- 修复插件更新后仍复用旧内部模块、导致 `'PluginConfig' object has no attribute 'is_group_allowed'` 的问题。
- 内部模块改为相对导入，使 AstrBot 重载时能正确清理并加载新代码。
- 内部模块版本不一致时在加载阶段给出明确提示，替代难以定位的运行时属性错误。

## 1.1.0 - 2026-09-18

- 新增 `allowed_group_ids` 群聊白名单配置。
- 非白名单群聊和私聊不响应监督命令。
- 自动检查仅处理白名单群聊，避免向其他群推送。

## 1.0.0 - 2026-09-17

- 首次发布 GitHub 公开活动监督插件。
- 支持群聊账户绑定、移除、查询和状态检查。
- 支持 GitHub Token、缓存、限流、重试和可选自动检查。
