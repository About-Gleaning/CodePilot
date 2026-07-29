# CODE-47 验证计划

基线、同进程多 Runner 和独立 worker 使用同一套合成 LLM、Tool、审批和事件场景；不接入真实密钥或网络。

必须验证：双 Agent 事件隔离、同 Agent 双 Session、写入租约、取消与关闭隔离、错误/过期审批、请求幂等、subagent 归属与取消、权限双重校验、慢消费者背压、旧 Session/附件/Scheduler 兼容、worker 崩溃和孤儿清理。

性能使用 3 次预热和至少 20 次测量，记录 p50/p95 的 ready、首事件、路由、审批、取消、租约和 RSS 指标。功能硬门槛包括零事件串线、零重复副作用、零存储损坏和可隔离取消。当前合成原型只为架构筛选提供证据，真实 LLM/MCP/RSS 验证仍属于正式运行时阶段。

生成结果的入口是 `backend/scripts/run_agent_runtime_experiment.py`；它只输出脱敏 JSON，提交前须核对输出与 `validation-results.json` 一致。
