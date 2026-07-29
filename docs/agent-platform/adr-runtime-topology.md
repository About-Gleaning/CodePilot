# ADR：多 Agent 执行拓扑

状态：Accepted

候选方案为同进程多 Runner与每 Agent 一个持久 worker。两者都必须经过同一事件归属、写入租约、审批、取消和持久化验证。

当前实验已证明同进程控制面可隔离并行 Run，并能在不阻塞执行的情况下标记慢消费者；worker 原型已证明最小子进程协议、启动和崩溃退出边界。它尚未承载真实 SessionRunner，因此不能据此宣称 worker 已通过生产可用性验证。

20 次合成测量中，同进程 Run 完成 p50/p95 为 6.403/9.561ms，worker ready p50/p95 为 76.460/114.029ms；完整数据和适用限制见 `validation-results.json`。这些指标不包含真实 LLM、MCP、网络或 RSS，因此只用于比较控制面附加成本。

负责人已通过 CODE-47 架构结论，CODE-50 正式采用“主进程控制面 + 同进程多 SessionRunner”：控制面和持久化保持主进程单写，每个 Session 使用独立 Runner，并通过 `AgentRuntimeBackend` 保留 worker 替换边界。

当出现不可信工具、阻塞型原生代码、硬资源配额、强进程隔离或规模超过当前 5 个活动 Agent 目标时，再启动 worker 拓扑评估；切换不得改变 Manager、RunRef、资源化 API 或事件归属契约。
