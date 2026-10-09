# 统一 Agent 模型与委派人工交互

- 日期：2026-09-24
- 工作项：CODE-66
- 状态：已实现，待用户验收

## 目标与范围

Agent 配置不再以永久 `agent`／`subagent` 类型限制用途。同一份配置可以允许用户直接启动，也可以允许其他 Agent 在当前 Run 中委派调用。运行时仍区分根执行与委派执行，保持独立上下文、父调用关系、一层委派限制和权限收窄。

网页交互式 Run 中，委派执行产生的工具审批、Hook 人工确认和 question 统一转交当前用户。根 Session 是人工等待状态与回复路由的唯一事实来源；子上下文保存消息、工具状态和恢复点。定时 worker 继续无人值守，不等待网页输入，也不跨重启恢复调用栈。

本轮不实现多层委派、跨重启继续人工等待、多个并行待处理交互或自动重放结果不确定的外部操作。

## 配置与兼容

新配置使用以下字段：

- `launch_modes`：包含 `direct`、`delegated` 中至少一项。
- `can_delegate`：当前 Agent 是否允许调用 `task` 委派其他 Agent。
- `delegate_agent_ids`：允许委派的目标；省略或 `null` 沿用目录默认，空数组显式禁用。

旧配置继续读取，并按以下规则归一化：

- `kind: agent` 转换为 `launch_modes: [direct]`。
- `kind: subagent` 转换为 `launch_modes: [delegated]`。
- `can_call_subagent` 转换为 `can_delegate`。
- `subagent_ids` 转换为 `delegate_agent_ids`。
- 更早期的 `allowed_subagent_ids` 同样转换为 `delegate_agent_ids`。

带 `revision_id` 的旧 Markdown 先按原始文件内容验证摘要，再在内存中归一化字段；加载不得因新旧序列化格式不同误报篡改，也不得自动改写活动文件或历史 revision。只有用户正常保存配置时才生成新格式 revision。真实内容与声明摘要不一致时仍拒绝加载。

保存新 revision 时只写新字段。内存模型保留只读兼容属性，避免旧调用方在迁移期间误判。公共执行包格式升级为 4，读取格式 1 至 3 时应用同一转换规则。

直接启动要求 `direct`；委派目标要求 `delegated`。调用方还必须启用 `can_delegate`、拥有 `task` 工具，并通过目标清单、用户可见性、归属和依赖校验。禁止自调用、循环调用和超过一层委派。

## 运行与交互

每次执行记录 `invocation_role=root|delegate`、`context_id`、`parent_call_id` 和 `depth`。委派上下文继承父 RunRef、停止信号、工作目录、写入租约以及根 Run 的人工交互通道，但使用目标 Agent 自己的模型、Prompt、工具、Skill、Hook 和记忆配置快照。

根交互通道同时持有根 Session、审批与 question 的事件和结果容器。同一 Run 同时只允许一个待处理交互。人工请求领域事件和 SSE 增加来源 Agent、执行角色、上下文及父调用信息；旧 `agent_kind` 字段继续派生为 `agent`／`subagent` 供兼容客户端读取。

审批通过或问题回答后，从原工具、Hook 或 question 暂停点继续，不重新调用模型、不重跑已完成 Hook 或工具。拒绝审批或拒答只取消当前委派上下文，`task` 返回可恢复的 `TaskDelegationRejected`，根 Agent 可以选择替代方案。用户显式停止根 Run 时，停止信号唤醒全部等待并取消完整父子调用栈。

## 安全与性能

委派后的有效能力取目标配置、调用方委派清单、当前用户授权和父任务约束的交集。人工批准仅放行对应调用一次，不放宽 readonly、工作区路径、MCP、连接或凭证校验。已发出但结果不确定的外部操作继续失败关闭且禁止重放。

交互注册和回复按 interaction ID 在 Run 内 O(1) 查找。目录筛选保持 O(n)，不新增逐 Agent 请求、轮询或额外 SSE。

## 实施进度

- [x] 产品行为与兼容策略确认
- [x] FlowDeck 工作项 CODE-66 已启动
- [x] Agent 配置、API 与公共执行包迁移
- [x] 根 Run 委派交互通道与恢复语义
- [x] Agent Studio 配置、目录与交互来源展示
- [x] 后端与前端自动化回归
- [x] AGENTS.md 与产品契约同步
- [x] 浏览器自动化交互验收
- [x] CODE-67 修复旧 revision 原始内容验签及 `allowed_subagent_ids` 兼容

## 验证记录

- 后端：`CODEPILOT_HOME=/private/tmp/codepilot-code66-final backend/.venv/bin/python -m pytest backend/tests -q`，472 通过、1 跳过。
- 前端：`pnpm test --run`，75 项通过。
- 构建：`pnpm build` 通过。
- 浏览器：`pnpm exec playwright test e2e/agent-config-redesign.spec.ts e2e/agent-studio-interaction.spec.ts`，2 项通过。
- 定向覆盖：旧字段读取与新格式保存、双启动模式、仅委派 Agent 禁止直接启动、公共包格式 4、无交互委派失败关闭、委派审批拒绝与 question 拒答仅取消分支、交互来源字段。
- CODE-67 定向覆盖：带摘要的格式 2 配置不改写加载、两种旧委派清单迁移、保存后升级新字段、内容篡改继续拒绝。

## 验收

- 同一 Agent 可分别直接启动和被委派，配置一致且上下文隔离。
- 旧 Markdown、历史 revision 和旧公共执行包可读；新保存内容仅使用新字段。
- 不允许直接启动、不可委派、自调用、越权目标和超过一层委派均被拒绝。
- 委派审批、Hook 确认和 question 可实时显示来源，刷新后仍可回复并原位恢复。
- 拒绝仅结束委派分支，显式停止取消完整调用栈。
- 定时 worker 不进入网页人工等待，服务重启不恢复旧调用栈。
