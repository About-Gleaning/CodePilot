# CODE-49：单 Agent 生命周期与聊天闭环

CODE-49 使用同进程运行时，但从第一版起以 `agent_id + session_id + run_id` 作为全部控制、事件和交互操作的归属键。

当前容量策略只允许一个已启动 Agent 和一个活动 Run；该限制位于 `InProcessAgentRuntimeBackend`，不属于 SessionRunner 或 API schema。每个 Session 通过 `SessionRunnerFactory` 获得独立 Runner，CODE-50/51 放开并行时无需重构会话内部状态。

运行期期望状态保存到 `workspace/agent-runtimes.json`，采用原子替换与 0600 权限。服务重启只恢复已启动状态并取消旧 Run，绝不重放 LLM、Tool 或 MCP 副作用。

流事件在持久化成功后才进入 SSE 队列；每个订阅队列限制为 1000 条，慢消费者不会阻塞 Agent。
