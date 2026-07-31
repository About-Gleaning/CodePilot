# CODE-52 Agent Studio

## 目标与边界

Agent Studio 将 Agent 配置、启停、Session 历史、聊天和 Scheduler 组合到同一个响应式工作台。页面只使用资源化 API；旧 `/api/session/*` 仍由后端兼容，但不再是新页面的事实来源。

本阶段不改变 CODE-50/51 的 Manager/Backend 拓扑，也不新增并发策略。页面展示的已启动 Agent 和活动 Run 容量全部来自 `AgentRuntimeManager`，不得在前端硬编码。

## 页面结构

桌面使用三栏布局：

- 左栏：Agent 搜索、活动/归档/异常筛选、运行状态、容量和新建入口。
- 中栏：当前 Agent/Session、启动、取消当前 Run、关闭 Agent、消息流、人工交互、附件和输入区。
- 右栏：Session 历史、Agent 配置和 Scheduler 自动化。

`/` 与 `/mobile` 复用同一组 hooks 和组件。窄屏仅把左右栏变为抽屉，不维护第二份 Agent、Session 或 Run 状态。

## 状态隔离

页面选择键为 `agent_id + session_id`。切换 Agent 或 Session 只改变查看上下文，不触发取消或关闭。

- `useAgentCatalog` 维护 Agent 目录、运行态容量和唯一一条低频控制 SSE。
- `useAgentSession` 只为当前查看的 Session 建立高频 SSE。
- `useAgentConfig` 按需加载 Prompt 和能力目录。
- `useScheduleManagement` 只在“自动化”页签可见时加载和轮询。

每次 Session 切换都递增 generation 并取消旧请求。回调通过 ref 保持稳定，避免父组件重渲染重复请求 replay。Session 列表还记录其所属 Agent ID，异步切换期间不会把前一个 Agent 的 Session 误配给新 Agent。

## Replay 与 SSE 一致性

Session 恢复顺序为：

1. 后端读取当前 `latest_event_seq`。
2. 回放 Domain records 和消息。
3. 复制安全的 Session runtime。
4. 客户端从该边界建立 SSE。

客户端按 `event_id` 和消息 ID 去重。事件 ID 缓存最多保留 1200 条；UI 事件列表最多保留 240 条。token 与 reasoning delta 使用 `requestAnimationFrame` 合并刷新，切走 Session 后不再消费后台 token，切回时从持久化 replay 恢复。

聚合运行态先捕获不透明 cursor，再复制运行态快照。cursor 之后的控制事件由 SSE 重放；`stream_reset_required` 或连接错误会重新获取快照和 cursor。

## 配置与安全

Agent 列表只返回默认 Provider、Model 和思考参数，不返回 Prompt。只有进入“配置”页签时才调用详情接口。活动 Run 期间允许保存新 revision，当前 Run 保持旧 revision，下一 Run 获取新 revision。

聚合流只携带 Agent、Run 和 interaction 状态，不携带 Prompt、token、附件、Tool 参数或人工交互正文。API client 只展示结构化 `code/message`，不直接渲染原始响应或堆栈。

附件 base64 和预览 URL 只存在于当前内存草稿；切换草稿不会写入浏览器存储。每个 Agent 独立保存内存草稿和结果不确定时的 `client_request_id`。

## 性能取舍

- Agent/runtime 合并使用按 ID 字典，查询为 `O(1)`，整体刷新为 `O(n)`。
- Session 历史默认只渲染 50 条，每次“加载更多”增加 50 条。
- 只有当前 Session 消费高频事件，避免后台 Agent token 占用渲染带宽。
- 运行态 mutation 使用 Agent ID 级同步 guard，重复点击不会发出第二个启停请求。

## 错误恢复

- `runtime_recovery_incomplete`：全局阻塞新 Run，保留历史回放与关闭操作。
- `cancellation_uncertain`：提示外部副作用无法确认，不提供直接重试。
- `run_capacity_exceeded`：展示容量和 `Retry-After`，不自动排队。
- `resource_ownership_mismatch`：停止当前操作并刷新目标 Session。
- Session 或 Agent 的局部加载错误不会清空整个 Studio。

验证证据见 `agent-studio-validation-results.json`。
