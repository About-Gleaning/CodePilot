# CODE-47 运行时契约

`AgentRuntimeBackend` 必须提供：启动/关闭 Agent、启动/取消 Run、回复人工交互、读取状态、订阅事件、恢复期望运行态和关闭后端。管理 API 只能依赖此契约，不能依赖同进程或 worker 的私有对象。

启动 Run 必须携带 `client_request_id`；同一请求重复提交返回原 Run，避免网络重试重复执行 Tool。关闭、取消和同一审批回复均幂等；错误或过期 interaction ID 返回冲突。

每个 RuntimeEvent 必须包含：`event_id`、`agent_id`、`session_id`、`run_id`、`run_seq`、`event_type`、`created_at`、`data`。只保证同一 Run 的事件顺序；控制面生成可重连 cursor，客户端按 event ID 去重。

Tool 需声明副作用范围：`read_only`、`workspace_mutation`、`runtime_mutation` 或 `external_mutation`。控制面为每个订阅者维护最多 1000 条事件的有界队列；溢出时标记重同步，不得阻塞 Run。

worker 使用受限双向消息协议：stdout 仅承载协议、日志走 stderr、命令行只传资源标识、不传 Prompt/附件/密钥。worker 事件由控制面单写入持久化；MCP 凭证留在控制面。

CODE-50 起，HTTP API 只依赖 `AgentRuntimeManager`。Manager 通过配置服务获取不可变 Profile 快照，并把 SessionRunner 私有 Task、Event 和人工交互 holder 封装在 `AgentRuntimeBackend` 句柄内。主进程最多保持 5 个已启动 Agent；CODE-51 起交互式活动 Run 上限为 5，同一 Session 仍只能有一个活动 Run。

Run 状态使用 compare-and-set 写入唯一终态；服务重启、取消、Agent 关闭和 watcher 不得重复发布终态或重复释放容量。最后一条不完整 JSONL 可归档证据后恢复完整前缀，中间损坏必须 fail closed。

workspace 变更 Tool 必须以 RunRef 获取非阻塞跨进程写入租约，并持有到执行终态；subagent 继承父 Run 租约。MCP 每服务最多 5 个并发调用、20 个 pending，请求发出后失败不得自动重放。

CODE-52 起，新页面只使用复数资源 API。Agent 列表可以返回默认 LLM，但不得返回 Prompt；Prompt 只由 Agent 详情接口按需读取。Session replay 必须同时返回 `latest_event_seq` 和安全运行态，客户端从该边界建立 Session SSE。

前端选择状态必须按 `agent_id + session_id` 隔离。异步 replay、Session 列表和 SSE 均需校验 generation 与资源归属；切换查看上下文不得解释为取消 Run。聚合控制流全局只保留一条，Session 高频流只为当前查看的 Session 建立一条。

CODE-53 的源码发布边界固定为本机单用户。HTTP 只接受回环 Client、Host 和 Origin；配置、replay 与附件响应禁止缓存。`live` 只反映进程装配，`ready` 必须同时满足运行时恢复完整、状态存储可写和至少一个 Provider 可用，MCP 降级不得泄露连接信息。

请求进入 Runtime 前必须完成正文、附件、文件名和人工交互字段的大小校验。LLM 请求日志只能记录安全摘要，所有日志与验证结果必须递归清除认证信息、图片 data URL 和绝对用户目录。源码发布门禁的确定性测试、真实 Provider、依赖审计和敏感信息扫描任一阻塞都不得标记为通过。
