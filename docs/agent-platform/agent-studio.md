# CODE-52 Agent Studio

## 目标与边界

Agent Studio 将 Agent 配置、启停、Session 历史、聊天和 Scheduler 组合到同一个响应式工作台。页面只使用资源化 API；旧 `/api/session/*` 仍由后端兼容，但不再是新页面的事实来源。

本阶段不改变 CODE-50/51 的 Manager/Backend 拓扑，也不新增并发策略。页面展示的已启动 Agent 和活动 Run 容量全部来自 `AgentRuntimeManager`，不得在前端硬编码。

## 页面结构

桌面以对话为中心：左侧 Agent 导航默认展开但可收起；会话和自动化检查器按需从右侧展开，避免长期占用主工作区宽度。Agent 配置作为主工作区的独立页面，与会话视图互斥显示。

- 左栏：Agent 搜索、活动/归档/异常筛选、运行状态、容量和新建入口。
- 主工作区：在会话视图中展示当前 Agent/Session、运行控制、消息流、人工交互、附件和输入区；在配置视图中展示 Agent 身份、模型、Prompt 和能力边界。
- 右栏：Session 历史和 Scheduler 自动化。

新建、复制和编辑 Agent 统一进入配置主页面。新建或复制保存成功后切换到新 Agent 的会话视图；编辑 revision 或归档状态后留在配置页面。进入配置页不会清空原会话选择、消息草稿或附件草稿，返回和切换 Agent 时统一执行未保存修改确认。

视觉层使用独立的 `agent-studio-refined.css`，只覆盖展示令牌和响应式布局，不改变 API、SSE、持久化或状态隔离。当前采用“信号台”设计语言：深色设备栏、黑白工作画布和高对比橙色运行信号；亮色主题保持相同信息层级，不复用旧版米白绿色卡片语言。空 Session 提供本地草稿建议，点击仅填充 Composer，不会自动发送或产生后端请求。

视觉重构必须保持业务 DOM 的可访问名称和交互入口稳定。桌面检查器仍按需展开，900px 以下左右栏转为抽屉；抽屉层级必须高于带 `backdrop-filter` 的遮罩，避免导航内容被整体模糊。动画统一支持 `prefers-reduced-motion`，不以动效传达唯一状态信息。

`/` 与 `/mobile` 复用同一组 hooks 和组件。窄屏仅把左右栏变为抽屉，不维护第二份 Agent、Session 或 Run 状态。

中央聊天区使用纵向 Flex 布局：命令栏、上下文和错误提示不伸缩，只有消息列表占用剩余高度并独立滚动，底部交互区始终保留在视口内。普通状态显示文本输入框；等待审批或 Question 时，对应人工交互面板替换文本输入框，但仍使用同一个固定底部区域。移动端优先使用动态视口高度，并保留传统视口高度回退。

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

Agent 列表只返回默认 Provider、Model 和思考参数，不返回 Prompt。只有进入配置主页面时才调用详情接口；新建空白配置只读取能力目录。活动 Run 期间允许保存新 revision，当前 Run 保持旧 revision，下一 Run 获取新 revision。

聚合流只携带 Agent、Run 和 interaction 状态，不携带 Prompt、token、附件、Tool 参数或人工交互正文。API client 只展示结构化 `code/message`，不直接渲染原始响应或堆栈。

附件 base64 和预览 URL 只存在于当前内存草稿；切换草稿不会写入浏览器存储。每个 Agent 独立保存内存草稿和结果不确定时的 `client_request_id`。

## 性能取舍

- Agent/runtime 合并使用按 ID 字典，查询为 `O(1)`，整体刷新为 `O(n)`。
- Session 历史默认只渲染 50 条，每次“加载更多”增加 50 条。
- 只有当前 Session 消费高频事件，避免后台 Agent token 占用渲染带宽。
- 运行态 mutation 使用 Agent ID 级同步 guard，重复点击不会发出第二个启停请求。

## C 端体验层验证

- `pnpm test --run`：覆盖 Agent 选择、会话与输入交互、主题以及 hooks 回归。
- `pnpm build`：通过 TypeScript 检查和 Vite 生产构建。
- 浏览器视觉检查：覆盖桌面亮色、桌面暗色、配置主页面、右侧检查器、390px 窄屏主界面与移动端 Agent 抽屉。
- 配置交互检查：覆盖新建、编辑、复制、保存去向、未保存离开确认，以及检查器只保留会话和自动化两个页签。
- 体验层仅增加本地 UI 状态（侧栏展开与收起、空态草稿填充）；不新增网络请求，空态建议不会自动发送任务。

## 错误恢复

- `runtime_recovery_incomplete`：全局阻塞新 Run，保留历史回放与关闭操作。
- `cancellation_uncertain`：提示外部副作用无法确认，不提供直接重试。
- `run_capacity_exceeded`：展示容量和 `Retry-After`，不自动排队。
- `resource_ownership_mismatch`：停止当前操作并刷新目标 Session。
- Session 或 Agent 的局部加载错误不会清空整个 Studio。

验证证据见 `agent-studio-validation-results.json`。
