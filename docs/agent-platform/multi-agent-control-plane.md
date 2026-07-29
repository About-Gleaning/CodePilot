# CODE-50：多 Agent 管理控制面与隔离路由

## 交付边界

CODE-50 将 HTTP API 的唯一运行时依赖收敛为 `AgentRuntimeManager`。Manager 管理 Agent、Session、Run、interaction、容量和幂等索引；`InProcessAgentRuntimeBackend` 只持有不透明的 SessionRunner 执行句柄。Scheduler worker 仅构建 Execution Bundle，不创建 Manager，也不读写 `agent-runtimes.json`。

本阶段允许最多 5 个 Agent 保持期望启动状态，但通过 `max_active_runs=1` 明确限制交互式活动 Run 为 1。CODE-51 只需替换容量策略并增加 workspace 写入租约，不需要修改资源化 API、RunRef 或 Runner 内部模型。

## 配置线性化与资源归属

Manager 通过 `AgentConfigService.get_active_profile_snapshot()` 原子捕获不可变 Profile。快照成功是 Run 配置的线性化点：随后发生的编辑或归档不影响该 Run；归档先完成时新 Run 被拒绝。

所有控制操作使用：

```text
agent_id + session_id + run_id
```

人工交互再增加 `interaction_id`。SessionRunnerFactory 为每个 Session 创建独立 Runner，停止事件、审批、Question、标题任务和当前执行任务不得跨 Session 共享。

## 容量、幂等与取消

- 已启动 Agent 上限由 `agent.max_started_agents` 控制，范围 1～5；统计包含期望运行的 `STARTING/RUNNING/ERROR`。
- CODE-50 不隐藏排队；第二个活动 Run 返回 `run_capacity_exceeded` 和 `Retry-After: 1`。
- `client_request_id` 在 workspace 内作为幂等键。指纹只包含客户端稳定意图，不包含稍后解析的 revision 或默认模型。
- Run 终态在 Manager 锁内比较并更新，watcher、cancel、stop 和 shutdown 只有第一个写入者释放容量并发布终态。
- 取消先协作等待 1 秒，再强制终止本 SessionRunner，单次总时限 10 秒。无法确认外部副作用停止时写入 `cancellation_uncertain`，Agent 进入 `ERROR`，不补偿或重放。

## 持久化与恢复

控制面文件位于 workspace 运行目录：

```text
agent-runtimes.json
agent-runs.jsonl
agent-runtime-events.jsonl
sessions/<date>-<session-id>.jsonl
sessions/<date>-<session-id>.events.jsonl
```

状态文件原子替换；JSONL 单条追加、flush、fsync；新文件权限为 `0600`。最后一条不完整记录会先归档到受限 `.corrupt` 目录，再恢复完整前缀；中间损坏则 fail closed，拒绝新 Run。服务恢复会把中断 Run 标记为 `CANCELLED/service_restarted`，不会调用 LLM、Tool 或 MCP。

Session 历史按文件名、大小和 mtime 建立增量摘要缓存，因此能发现 Scheduler 跨进程写入，同时只重新解析变化文件。外部 session ID 只按普通字符串比较，禁止拼入 glob。

## 事件与兼容接口

Session SSE 保留 token、Tool、审批和 subagent 等完整事件。`/api/agent-runtimes/stream` 只发布 Agent、Run 和 interaction 低频状态，使用 URL-safe 不透明 cursor。流事件必须先持久化再入队；每个订阅队列上限 1000，溢出后发送 `stream_reset_required` 并关闭连接。

旧 `/api/session/*` 已由 `LegacySessionAdapter` 转换为资源化 Manager 调用。兼容指针不进入 Manager；旧客户端未提供 `client_request_id` 时响应标记 `legacy_best_effort`。当前前端会为每次用户消息生成可复用的请求 ID。

## 性能与安全

确定性验证结果见 `multi-agent-control-plane-validation-results.json`。预热 3 轮、正式 20 轮后，Agent ready p95 为 0.594ms，控制事件路由 p95 为 0.006ms；5 个空闲 Agent 未推动进程峰值 RSS 上升，均低于门槛。这些数据不包含真实 LLM 网络延迟。

运行态文件、事件和验证结果不保存 Prompt、附件 base64、Authorization、密钥或完整 Tool 大结果。
