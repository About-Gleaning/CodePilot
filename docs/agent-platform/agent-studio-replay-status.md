# Agent Studio 回放、状态与模型选择

## 会话回放

同一 assistant 消息可能在人工审批或 Question 恢复后再次持久化。回放以消息 ID 为键保留最后一份快照，确保已完成的工具结果覆盖早先的 `pending` 状态，避免历史工具调用阻止后续 LLM 请求。审批通过后的工具执行完成时，运行时必须先追加相同消息 ID 的 Domain 快照，再发送 assistant 完成 SSE；快照写入失败不得发送该完成 SSE。

## 运行状态

Run 持久化 `error_code` 和安全的 `error_summary`。会话运行态同时返回当前活动 Run 与最近 Run；页面优先显示 SSE 的会话错误详情，重载后回退到最近 Run 的安全摘要。原始异常、路径和凭证不写入该摘要。

## 新会话模型

内置 build、plan、explore Profile 默认使用 `deepseek / deepseek-v4-pro`。新会话不携带覆盖值时由 Agent 默认值解析；用户勾选覆盖后，前端只可从 `/api/config` 返回的已激活 Provider、Model 与 Thinking 选项中选择，并以该选择优先。

## 验证

- `cd backend && uv run pytest`
- `cd frontend && pnpm test --run`
- `cd frontend && pnpm build`
