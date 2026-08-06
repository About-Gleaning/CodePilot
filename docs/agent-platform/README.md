# Agent 平台设计与验证

此目录保存 Agent 平台的可追溯设计和验证证据，不能只在聊天或 Plane 评论中保留结论。

- `product-semantics.md`：CODE-47 已确认的产品语义。
- `runtime-contract.md`：控制面与执行后端的拓扑无关契约。
- `validation-plan.md`：可执行验证场景和门槛。
- `validation-results.json`：由基准脚本生成的脱敏原始结果。
- `adr-runtime-topology.md`：拓扑决策记录；当前状态为 Accepted。
- `agent-config-center.md`：CODE-48 的 Agent 配置、revision、归档和能力目录契约。
- `agent-config-validation-results.json`：CODE-48 的脱敏验证结果。
- `single-agent-runtime.md`：CODE-49 的资源化运行时与兼容边界。
- `single-agent-runtime-validation-results.json`：CODE-49 的脱敏验证结果。
- `multi-agent-control-plane.md`：CODE-50 的 Manager/Backend、隔离路由、恢复与兼容契约。
- `multi-agent-control-plane-validation-results.json`：CODE-50 的脱敏容量与性能结果。
- `parallel-agent-runtime.md`：CODE-51 的 5 Run 并发、写入租约、背压和取消治理。
- `parallel-agent-runtime-validation-results.json`：CODE-51 的脱敏并发与性能结果。
- `agent-studio.md`：CODE-52 的多 Agent 工作台、独立配置主页面、响应式状态、C 端体验层与 replay/SSE 一致性设计。
- `agent-studio-validation-results.json`：CODE-52 的后端、前端与浏览器脱敏验证结果。
- `agent-studio-replay-status.md`：Agent Studio 的重复消息回放、Run 错误摘要与新会话模型选择契约。
- `release-readiness.md`：CODE-53 的本机安全边界、健康探针、源码发布门禁与回滚手册。
- `release-validation-results.json`：CODE-53 的确定性、真实 DeepSeek、依赖审计和脱敏扫描结果。

后续 CODE-48 至 CODE-53 进入待验收前，必须增加或更新对应设计、验证结果，并在本文件登记链接。
