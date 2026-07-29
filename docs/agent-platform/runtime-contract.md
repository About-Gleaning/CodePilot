# CODE-47 运行时契约

`AgentRuntimeBackend` 必须提供：启动/关闭 Agent、启动/取消 Run、回复人工交互、读取状态、订阅事件、恢复期望运行态和关闭后端。管理 API 只能依赖此契约，不能依赖同进程或 worker 的私有对象。

启动 Run 必须携带 `client_request_id`；同一请求重复提交返回原 Run，避免网络重试重复执行 Tool。关闭、取消和同一审批回复均幂等；错误或过期 interaction ID 返回冲突。

每个 RuntimeEvent 必须包含：`event_id`、`agent_id`、`session_id`、`run_id`、`run_seq`、`event_type`、`created_at`、`data`。只保证同一 Run 的事件顺序；控制面生成可重连 cursor，客户端按 event ID 去重。

Tool 需声明副作用范围：`read_only`、`workspace_mutation`、`runtime_mutation` 或 `external_mutation`。控制面为每个订阅者维护最多 1000 条事件的有界队列；溢出时标记重同步，不得阻塞 Run。

worker 使用受限双向消息协议：stdout 仅承载协议、日志走 stderr、命令行只传资源标识、不传 Prompt/附件/密钥。worker 事件由控制面单写入持久化；MCP 凭证留在控制面。
