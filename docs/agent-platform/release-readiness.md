# CODE-53 Agent 平台发布硬化

## 发布边界

当前发布形态是本机单用户源码运行。HTTP 服务只接受回环客户端、回环 Host 和回环 HTTP Origin；不提供公网访问、登录、反向代理、容器或安装包支持。`dev.sh` 同样拒绝非回环监听地址。

配置、Session replay 和附件响应使用 `Cache-Control: no-store`。安全中间件统一增加 `nosniff`、禁止 Referrer 和禁止嵌入响应头。`/api/config` 不再返回本地目录，资源化 replay 不返回原始记录或绝对路径。

## 输入、日志与工具安全

- 用户正文最多 100000 字符，`client_request_id` 最多 128 字符。
- 单条消息最多 4 张图片，单张解码后最多 5 MiB，同时限制 base64 编码长度。
- 文件名、审批备注、Question 回答和 metadata 均有明确上限。
- LLM 请求日志默认关闭；开启时也只记录模型、数量和耗时等摘要。
- 日志递归脱敏 Bearer/API Key、认证 URL、图片 data URL 和用户目录。
- 发布配置中 Bash 调用全部要求人工审批。

## 健康探针

- `GET /api/health/live`：仅证明 FastAPI 已完成装配，不探测外部 Provider。
- `GET /api/health/ready`：运行时恢复完整、状态目录可写且至少存在一个已激活 Provider 时返回 200。
- MCP 未配置或个别服务不可用只产生 `degraded`，不会阻断不依赖该 MCP 的聊天。
- 恢复不完整、存储不可写或没有 Provider 时返回 503，响应仅包含稳定组件状态和计数。

## 源码发布门禁

在已配置 DeepSeek 密钥的本机执行：

```bash
cd backend
uv run python scripts/validate_agent_platform_release.py \
  --mode all \
  --live-provider deepseek \
  --live-model deepseek-v4-flash \
  --output ../docs/agent-platform/release-validation-results.json
```

门禁依次执行锁文件检查、后端测试、前端测试与构建、5 Run 确定性压力测试、本地 MCP 协议测试、真实 DeepSeek 链路、生产依赖审计和敏感信息扫描。任一硬门槛失败或审计服务不可用都会返回非零状态。

真实模型结果不保存请求正文、回复正文、API 地址或请求 ID。外部 MCP 未配置时明确记录 `not_configured`，不会伪装为已验收。

## 发布与回滚

1. 备份当前 `CODEPILOT_HOME`。
2. 使用锁文件安装依赖：`uv sync --frozen --extra dev` 与 `pnpm install --frozen-lockfile`。
3. 运行完整源码发布门禁。
4. 仅使用回环地址启动服务，检查 live 与 ready。
5. 如需回滚，停止服务、切回上一 Git 提交并恢复备份。

当前没有数据库迁移；旧 Session 和 Agent Markdown 保持兼容。重启只取消中断 Run，不重放 LLM、Tool 或 MCP 副作用。
