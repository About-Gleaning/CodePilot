# Agent 平台设计与验证

此目录保存 Agent 平台的可追溯设计和验证证据，不能只在聊天或 Plane 评论中保留结论。

- [publication-dependencies.md](publication-dependencies.md)：CODE-72 的结构化发布诊断、依赖统一预览、恢复旧公共工具、事务回执及部分成功重试；已实现，待用户验收。

- [python-tool-management.md](python-tool-management.md)：CODE-69 的 Python 代码工具、独立发布、调用者凭证、Agent 绑定与旧工具／执行包兼容，CODE-70 的 Agent 保存请求字段修复，以及 CODE-71 的独立可读调用名称和冲突保护；待用户验收。

- [unified-agent-delegation.md](unified-agent-delegation.md)：CODE-66 的统一 Agent 启动模式、委派运行角色、根 Run 人工交互通道与旧配置兼容，以及 CODE-67 的旧 revision 原文验签修复；已实现，待用户验收。

- [frontend-interaction-redesign.md](frontend-interaction-redesign.md)：CODE-64 的 Agent 模块总览、独立交互页和统一设计令牌，以及 CODE-68 的仅委派 Agent 配置入口；待用户验收。

- [scheduled-workbench.md](scheduled-workbench.md)：定时 worker 全自动、最新配置准入、独立容量、重叠跳过、Run 身份上报与停止恢复、当前会话概览/成果/子 Agent；待用户验收。

- [skill-live-and-hook-versions.md](skill-live-and-hook-versions.md)：Skill 读取当前内容、移除执行版本和个人历史、旧数据兼容及 Hook 可读版本号；待验收。

- [resource-management-redesign.md](resource-management-redesign.md)：技能与执行钩子的一级导航、Agent 六分区精密配置工作区、独立保存边界，以及桌面移动端验证；待用户验收。

- [skill-hook-management.md](skill-hook-management.md)：独立 Skill／Hook 页面、个人脚本版本、外部协议、原位审批恢复、身份绑定和受控测试；待用户验收，未执行完整发布门禁。

- [agent-assembly.md](agent-assembly.md)：组装能力的执行契约、资源管理、测试证据与运维前提；待用户验收，未通过发布门禁。

- [message-submissions.md](message-submissions.md)：CODE-61 本轮执行上限修复、复用发送接口追加、幂等持久化与决策边界；本轮验证不代表六阶段或发布门禁完成。

- [product-direction-handoff.md](product-direction-handoff.md)：CODE-60 产品方向交接记录；完整已确认目标、八项规则、开发差距和验收场景见根目录 [PRODUCT_DIRECTION.md](../../PRODUCT_DIRECTION.md)。该目标尚未全部实现，不能将历史契约或验证结果视为新目标已通过。

- `startup-readiness.md`：CODE-59 的桌面启动依赖、就绪检查和失败回收验证。

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
- `agent-studio-replay-status.md`：Agent Studio 的重复消息回放、审批恢复快照先于完成 SSE、Run 错误摘要与新会话模型选择契约。
- `release-readiness.md`：CODE-53 的本机安全边界、健康探针、源码发布门禁与回滚手册。
- `release-validation-results.json`：CODE-53 的确定性、真实 DeepSeek、依赖审计和脱敏扫描结果。
- `multi-user-isolation.md`：CODE-54/CODE-56/CODE-57 的认证、资源复合键、容量恢复、历史 revision、调度与离线迁移契约。
- `multi-user-isolation-validation-results.json`：CODE-54/CODE-57 的后端、前端与双用户隔离验证结果。

后续 Agent 平台工作进入待验收前，必须增加或更新对应设计、验证结果，并在本文件登记链接。
# 最新验证补充

- [Agent 配置与启动错误说明](agent-error-feedback.md)：结构化原因、脱敏、页面反馈和 life 修复验证（2026-09-22，待验收）。
# 公共发布最新记录

2026-09-22：[Agent 主动发布与公共使用](public-agent-publications.md)，包含公共资源归属、生效边界、个人使用设置及验证证据。公共复制与差异更新后置。
