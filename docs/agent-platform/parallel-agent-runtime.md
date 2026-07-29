# CODE-51：多 Agent 并行执行后端

## 交付边界

CODE-51 将交互式活动 Run 上限从 1 提升为 5，不增加隐藏队列。容量按
`STARTING/RUNNING/WAITING_HUMAN/CANCELLING` 统一统计；第 6 个 Run 返回
`run_capacity_exceeded`。Scheduler Run 不进入该计数。

本阶段不增加页面。现有资源化 API 和完整 `agent_id/session_id/run_id` 路由保持不变，
CODE-52 将基于这些接口完成 Agent Studio。

## 并发线性化

- `client_request_id` 首次进入时创建 workspace 级 Future 预留。并发相同请求等待首个
  请求并返回同一 Run；指纹冲突立即拒绝。
- `(agent_id, session_id)` 在 Runner 加载前预留活动 Run，因此并发请求不能绕过
  Session 单 Run 约束。
- Agent 状态复核、全局容量和 Session 预留位于同一 Manager 临界区。Run 先预留时
  stop 必须取消它；stop 先进入 `STOPPING` 时新 Run 被拒绝。
- Run 终态使用独立状态锁和完成事件。watcher、cancel、stop 与 shutdown 只会释放一次
  容量和 Session 预留。
- `agent-runtimes.json` 通过 generation 与单独持久化锁避免旧快照覆盖新快照。

Manager 不再读取 SessionRunner 的 Task、Event、审批或 Question holder。Backend 只返回
`RunExecutionResult`、`CancellationResult` 和脱敏 Session 快照，为未来 worker Backend
保留替换边界。

## 写入租约

所有 `workspace_mutation` Tool 在审批和预检通过后、执行前获取
`workspace-write.lock` 的非阻塞 `flock`。租约归属 RunRef，同一 Run（包括 subagent）
重复获取幂等；其他 Run立即得到 `WorkspaceWriteBusy`，必须在租约释放后重新读取文件。

锁文件权限为 `0600`，只记录 RunRef 与 PID。租约从第一次本地变更持有至 SessionRunner
执行收尾；Scheduler worker 使用同一文件锁。进程崩溃时内核自动释放锁，不删除锁文件。

## 外部调用与取消

每个 MCP server 共享一个 ClientSession，最多同时处理 5 个调用，pending 队列上限 20。
队列满返回 `McpCapacityExceeded`。请求发出后的失败返回 `McpOutcomeUncertain`，不自动
重放远端 Tool，也不回显 URL、Header、环境变量或底层异常全文。

Bash 使用独立进程组。取消时先发 SIGTERM，短暂等待后发送 SIGKILL，并执行 wait 回收。
LLM 流在结束或取消时显式调用 `aclose()`。标题、subagent 与 Tool 仍继承 SessionRunner
的 Run 级取消信号。

## 事件与资源

每个 RunEventScope 使用独立异步锁，使 `run_seq` 分配、持久化和 SSE 投递严格有序；
不同 Run 不共享此锁。SSE 队列保持 1000 条上限。

纯历史 replay/stream 只读取 Session 元数据验证归属，不创建 SessionRunner。空闲执行
句柄最多保留 20 个，活动或等待 interaction 的句柄不驱逐，超限后按最近访问时间关闭。

## 验证

专项测试覆盖并发幂等、Session 预留、写租约和 100 事件严格顺序。完整后端回归和前端
构建结果与确定性性能数据记录在 `parallel-agent-runtime-validation-results.json`。
验证脚本只使用临时目录，结果不包含 Prompt、附件、密钥或绝对用户目录。
