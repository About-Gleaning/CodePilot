# CODE-54 多用户 Agent 隔离

## 安全边界

CodePilot 后端仍只监听回环地址。局域网生产部署必须由同机反向代理把前端与 `/api` 暴露在一个唯一 HTTPS Origin 下；浏览器不能直接访问 Uvicorn。认证提供应用级资源隔离，不提供 OS、项目文件或命令执行沙箱：所有用户仍共享 workspace、Bash 可见范围与 workspace 写入租约，因此只适用于互信用户。

管理员只能通过离线 CLI 管理账号与共享 Agent。管理员角色不授予读取其他用户 Session、附件、长期记忆、Run 或 Schedule 的产品 API 权限；跨用户资源统一返回 404。

## 身份与认证

可信身份为 `UserPrincipal(user_id, username, role)`，其中 `user_id` 是服务端 UUID。SQLite 保存用户、认证会话、审计事件和 schema version；密码长度为 4 到 1024 个字符，使用版本化 scrypt，最多并发校验 2 次。高熵 Session Token 只保存 SHA-256 摘要，认证请求不执行慢哈希。会话具有 12 小时闲置期限和 7 天绝对期限。

生产 Cookie 使用 `Secure; HttpOnly; SameSite=Strict; Path=/`，本机开发使用独立的非 Secure Cookie 名称。CSRF Token 与认证会话绑定，所有写请求同时校验可读 Cookie、`X-CodePilot-CSRF` 与精确 Origin。Scheduler report 只接受回环请求和独立 Worker Token，不接受用户 Cookie 替代。

## 资源与运行时键

运行时事实键固定为：

```text
UserAgentKey = (user_id, agent_id)
SessionKey   = (user_id, agent_id, session_id)
RunKey       = (user_id, agent_id, session_id, run_id)
```

`client_request_id`、锁、恢复状态、交互索引、Session 句柄和控制流 cursor 均包含用户维度。Run 固化 revision 与能力快照；执行 MCP 时还与当前服务策略求交集，因此策略收紧即时生效，策略放宽不会扩张旧 Run 权限。

Agent 分为 `builtin`、`shared`、`private`。内置与共享 Agent 对启用用户只读，私有 Agent 仅所有者可维护；私有与共享 Agent 可以同名，选择、Task、Schedule、revision 与持久化只使用 `agent_id`，名称仅作展示快照。`task` 工具描述只列出活动的内置与共享 subagent，执行时再用当前 `user_id + agent_id` 解析用户可见快照，静态目录不能泄露其他用户的私有配置。

全服务活动 Run 上限为 5、每用户为 2；全服务 started Agent 上限为 5、每用户为 3。启动时只读取一次用户启用状态：所有用户的历史活动 Run 均转为 `CANCELLED/service_restarted`，仅启用用户恢复期望运行的 Agent。恢复过程执行相同的全服务和每用户容量限制，超限项不排队，转为带 `service_started_agent_capacity_exceeded` 或 `user_started_agent_capacity_exceeded` 的停止状态。禁用用户不会启动 Agent 或 Scheduler，其 Agent 期望状态与已有周期任务分别持久化为停止和禁用。运行期间禁用用户还会撤销认证会话、关闭 SSE、取消可取消 Run、作废待处理交互并停止 Scheduler worker。

Schedule 执行只接受固化的 `user_id + agent_id + revision_id`，自定义 Agent 的当前与历史 revision 都按单个确定路径读取不可变快照；只有未声明 revision 的旧配置可在首次加载时补建快照。Agent 更新后仍可执行旧 revision；Agent 归档、revision 缺失、摘要损坏、符号链接或身份不匹配只使本次 Run 失败，不创建 worker，也不影响后续调度。动态用户创建或重新启用任务时必须启动其 Schedule runner，禁用、删除与只读查询不启动后台循环。worker 对执行包 profile 再做一次无人值守净化，模型不可见且不可调用 `question`。旧任务迁移无法唯一映射 Agent 时必须设置 `enabled=false`、清空 `next_run_at`，并在已有 `metadata.migration_status` 写入 `disabled_agent_unresolved`，禁止按名称降级执行。

## 存储与事件

认证库位于 `{codepilot_home}/auth.sqlite3`。共享 Agent 位于 `{codepilot_home}/agents/shared/`，私有 Agent 与记忆位于 `{codepilot_home}/users/<user_id>/`。Session、附件、运行时日志、Schedule 与临时文件位于 `workspace/<workspace_id>/users/<user_id>/`；workspace 源码和写入锁保持共享。

所有新 JSON/JSONL 记录包含 schema version 与可信 `user_id`。路径分区之外还校验 UUID、解析后父目录和记录 owner。用户事件由 `RunEventScope` 补齐完整归属；EventBus 在入队前按用户和可选 Session 过滤，控制日志与 cursor replay 按用户分区。

长期记忆先合并 `memory/_global.md`，再合并 `memory/<agent_id>.md`。附件只保存到当前用户目录，并同时验证 Session、消息和文件记录归属。

## 迁移与运维

检测到旧单用户格式时服务进入 `migration_required`：健康接口可用，Scheduler 不启动，认证之外的副作用请求被拒绝。迁移只能通过离线 CLI 先预览、后显式 `--apply`；执行前备份旧数据并写 journal，逐文件原子写入。旧 Agent、revision、归档、记忆、Session、附件、Schedule 和运行态归初始管理员私有，不自动发布共享 Agent；无法固化 Agent 身份与 revision 的旧 Schedule 保留记录但禁用。

常用命令：

```bash
cd backend
uv run python -m codepilot.admin init-admin --username admin
uv run python -m codepilot.admin migrate-multi-user --admin admin
uv run python -m codepilot.admin migrate-multi-user --admin admin --apply
```

共享 Agent 只能由管理员 CLI 从 Markdown 发布或归档，操作写入不含正文和凭证的认证审计事件。
