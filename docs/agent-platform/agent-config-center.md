# CODE-48 Agent 配置中心

自定义 Agent 继续以 Markdown 作为配置来源。活动配置位于 `{codepilot_home}/agents/<name>.md`，归档配置位于 `.archived/`，不可变快照位于 `.revisions/<agent_id>/<revision_id>.md`。

`agent_id` 对新配置使用 UUID；内置和旧配置使用确定性 ID。`revision_id` 是规范化配置（不含 revision 字段自身）的 SHA-256 摘要，因此相同保存和网络重试不会制造重复版本。

配置中心只接受主 Agent，名称创建后不可改。新增和编辑必须校验已激活的 Provider/Model、思考参数、本地 Tool 与 MCP 服务。MCP 服务目录只返回名称、可用状态和审批属性，绝不返回连接参数、环境变量或密钥。

归档不会取消已开始的 Run，也不会删除历史 Session 或 Schedule；后续运行入口会拒绝归档 Agent。默认 LLM 在本节点只保存，自动用于会话属于 CODE-49。

配置写入使用同目录临时文件、fsync 与原子替换。单次写入最多 256 KiB，读取旧文件最多 1 MiB；异常文件作为脱敏无效记录展示，不会阻塞其他自定义 Agent 或内置 Agent 加载。
