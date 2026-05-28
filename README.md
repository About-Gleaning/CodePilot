# CodePilot

CodePilot 是一个以 Web 为入口、Gateway 为统一协议层、Agent Runtime 为核心的后台型 Coding Agent 框架。一期目标不是复刻完整 Claude Code / Codex CLI，而是先搭起一个边界清晰、可运行、可扩展的最小骨架。

## 一期能力范围

- 后端：`FastAPI + LiteLLM + SSE + jsonl`
- 前端：`React + Vite + TypeScript`
- 单实例只绑定一个 workspace
- 单实例只允许一个 active session
- 支持 Web 页面发起任务
- 支持 LiteLLM 真实流式输出
- 支持一个无副作用 demo tool：`echo_tool`
- 支持 Hook 基础设施与 `PromptPluginHook`
- 支持 human-in-the-loop 状态机
- 支持 `session.jsonl` 会话恢复与 `events.jsonl` SSE 重放

## 一期明确不做

- 不接数据库
- 不使用 WebSocket
- 不引入 LangGraph
- 不实现 LSP
- 不实现复杂 dashboard
- 不实现复杂权限系统
- 不实现复杂真实工具集
- 不实现真实 MCP 连接

后续接入 MCP 时，必须使用官方 MCP Python SDK，而不是自己实现协议细节。

## 项目结构

```text
codepilot/
  backend/
    .env.example
    config.example.yaml
    config.yaml
    pyproject.toml
    src/codepilot/
  frontend/
    package.json
    src/
  README.md
```

## 后端安装与启动

推荐使用你已经准备好的 Python 3.12 虚拟环境。

1. 安装 `uv`
2. 进入 `backend/`
3. 安装依赖：

```bash
uv sync
```

4. 复制后端配置模板与环境变量模板：

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

5. 在 `backend/.env` 中填写需要激活的 LLM 厂商密钥。配置了哪家厂商所需的完整环境变量，就代表激活了哪家厂商；可以同时激活多家。

6. 启动后端：

```bash
cd backend
uv run uvicorn codepilot.main:app --app-dir src --reload --host 127.0.0.1 --port 8000
```

## 前端安装与启动

```bash
cd frontend
pnpm install
pnpm dev
```

前端开发服务器默认通过 Vite 代理把 `/api` 转发到 `http://127.0.0.1:8000`。

## 统一启动脚本

仓库根目录提供了一个开发态启动脚本：

```bash
./dev.sh start
./dev.sh stop
./dev.sh restart
```

默认行为：

- `start` 同时启动后端 FastAPI 和前端 Vite
- `stop` 同时停止前后端
- `restart` 先停止再启动

运行时文件会写到：

```text
.run/
  backend.pid
  frontend.pid
  backend.log
  frontend.log
```

如果脚本提示缺少 `uv` 或 `pnpm`，请先完成对应工具安装与依赖初始化。后端启动时会自动加载 `backend/.env`，因此不需要额外执行 `export`。

## LiteLLM 配置

在 `backend/.env` 中配置对应 Provider 所需的密钥。当前内置支持 `openai` 与 `qwen`：

```dotenv
OPENAI_API_KEY="your-key"
QWEN_API_KEY="your-qwen-key"
QWEN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

然后在 `backend/config.yaml` 中维护厂商与模型清单：

```yaml
llm:
  providers:
    openai:
      label: "OpenAI"
      models:
        - "gpt-5.3-codex"
        - "gpt-4.1"
      default_model: "gpt-5.3-codex"
      litellm_model_prefix: ""
    qwen:
      label: "Qwen"
      models:
        - "qwen-plus"
        - "qwen-max"
      default_model: "qwen-plus"
      litellm_model_prefix: "openai/"
```

规则说明：

- `backend/.env` 中只有环境变量完整的厂商才会被激活
- 前端的 provider 与 model 下拉框全部来自 `backend/config.yaml`
- 若同时激活多个厂商，发起任务时必须显式选择 provider

一期直接通过 LiteLLM 发起真实流式调用，不做 mock。

## 存储目录

默认 `codepilot_home` 为：

```text
~/codepilot
```

实际目录结构：

```text
{codepilot_home}/
  workspace/
    {workspace_id}/
      workspace.json
      sessions/
        yyyy-MM-dd-sessionId.jsonl
        yyyy-MM-dd-sessionId.events.jsonl
      logs/
        yyyy-MM-dd.log
```

其中：

- `session.jsonl` 保存会话生命周期、消息、审批记录等可恢复状态
- `events.jsonl` 保存 SSE 事件，用于断线重放

## API 概览

- `POST /api/session/input`
  统一处理 `user_message / human_reply / stop`
- `GET /api/session/stream?after_seq=0`
  SSE 事件流，支持 replay + 实时订阅
- `GET /api/session/status`
  当前 workspace 与 session 快照
- `GET /api/session/replay`
  从 `session.jsonl` 恢复消息与会话记录
- `GET /api/config`
  返回前端初始化所需配置

## 使用说明

- 默认 agent 提供：`build`、`plan`、`subagent`
- `build / plan` 共用同一个 `AgentLoop`
- 若要体验审批演示，可以在任务文本中加入 `[[approve]]`
- `echo_tool` 为无副作用示例工具，便于验证 LLM tool call 链路
- `context.compression_enabled` 可开启上下文压缩，支持按模型配置 token 阈值、保留最新轮次、LLM 摘要压缩和旧 Tool Result 占位清理

## 当前约束

- 一个后端实例只负责一个 workspace
- `workspace_path` 固定为后端启动时所在目录
- 当前只支持一个 active session
- Web 连接断开不会中断后台 AgentLoop

## 后续扩展方向

- MCP 适配器接入
- 更多真实工具
- 更完整的 subagent 编排
- 持久化存储升级
