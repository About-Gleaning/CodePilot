# CODE-49：单 Agent 生命周期与聊天闭环

CODE-49 使用同进程运行时，但从第一版起以 `agent_id + session_id + run_id` 作为全部控制、事件和交互操作的归属键。

CODE-49 当时只允许一个已启动 Agent 和一个活动 Run。CODE-50 已将启动容量提升为 5，并把控制索引迁移到 `AgentRuntimeManager`；活动 Run 仍为 1。每个 Session 继续通过 `SessionRunnerFactory` 获得独立 Runner，CODE-51 放开真实并行时无需重构会话内部状态。

运行期期望状态保存到 `workspace/agent-runtimes.json`，采用原子替换与 0600 权限。服务重启只恢复已启动状态并取消旧 Run，绝不重放 LLM、Tool 或 MCP 副作用。

流事件在持久化成功后才进入 SSE 队列；每个订阅队列限制为 1000 条，慢消费者不会阻塞 Agent。
