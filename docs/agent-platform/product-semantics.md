# CODE-47 产品语义

## 资源边界

Agent 是具有稳定 `agent_id` 的配置资源；保存形成不可变 `AgentRevision`。Session 只归属一个 Agent 并保持 `OPEN` 或 `ARCHIVED`；Run 是一条输入产生的一次执行。审批或问题请求必须同时绑定 Agent、Session、Run 和 interaction ID。

内置 Agent 可运行但只读，可复制为自定义 Agent。revision 被 Run 或 Schedule 引用时保留，不物理删除。旧 Session 按现有 `agent_name` 显示；无法解析时以只读 legacy 方式保留，后续输入才生成正式 Run 元数据。

## 生命周期

Agent 依次使用 `STOPPED/STARTING/RUNNING/STOPPING/ERROR`；Session 使用 `OPEN/ARCHIVED`；Run 使用 `STARTING/RUNNING/WAITING_HUMAN/CANCELLING/COMPLETED/FAILED/CANCELLED`。

单一 Session 同时只能有一个 Run；同一 Agent 的不同 Session 可以并行。全局上限是 5 个活动 Run，超限立即返回可重试容量错误。关闭 Agent 会取消其全部活动 Run；取消单个 Run 不停止 Agent。服务重启恢复期望启动状态，但所有中断 Run 标记为取消，绝不重放副作用。

## workspace 与调度

同一 workspace 可并行聊天、LLM 和只读 Tool，但同一时刻只有一个 Run 能持有本地写入租约。写文件、编辑文件和 Bash 等本地变更工具在租约忙时返回 `WorkspaceWriteBusy`，不得隐式排队。subagent 继承父 Run 租约。

Scheduler 不依赖交互式 Agent 是否启动；触发时解析最新有效 revision，并把实际 revision ID 固化到 Schedule Run。已归档 Agent 或无效 revision 必须失败，不得回退。
