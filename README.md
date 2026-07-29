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
- 支持通过官方 MCP Python SDK 接入 stdio 与 Streamable HTTP 工具服务

## 一期明确不做

- 不接数据库
- 不使用 WebSocket
- 不引入 LangGraph
- 不实现 LSP
- 不实现复杂 dashboard
- 不实现复杂权限系统
- 不实现复杂真实工具集

## 项目结构

```text
codepilot/
  backend/
    .env.example
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

4. 复制环境变量模板：

```bash
cp .env.example .env
```

5. 如需调整服务端口、模型清单或工具策略，直接修改仓库中的 `backend/config.yaml`。该文件不包含密钥，可以提交到 GitHub。

6. 在 `backend/.env` 中填写需要激活的 LLM 厂商密钥。配置了哪家厂商所需的完整环境变量，就代表激活了哪家厂商；可以同时激活多家。

7. 启动后端：

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

在 `backend/.env` 中配置对应 Provider 所需的密钥。当前内置支持 `openai`、`qwen` 与 `deepseek`：

```dotenv
OPENAI_API_KEY="your-key"
QWEN_API_KEY="your-qwen-key"
QWEN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_API_KEY="your-deepseek-key"
# 可选：默认使用 https://api.deepseek.com。
DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

然后在 `backend/config.yaml` 中维护厂商与模型清单：

```yaml
llm:
  providers:
    openai:
      label: "OpenAI"
      models:
        - id: "gpt-5.3-codex"
          thinking:
            kind: "reasoning_effort"
            allowed_values: ["low", "medium", "high"]
            default_value: "medium"
        - "gpt-4.1"
      litellm_model_prefix: ""
    qwen:
      label: "Qwen"
      models:
        - "qwen3-max"
        - "qwen3-coder-next"
        - id: "qwen3.5-flash"
          thinking:
            kind: "extra_body_boolean"
            extra_body_key: "enable_thinking"
            allowed_values: ["on", "off"]
            default_value: "on"
        - "kimi-k2.5"
        - "kimi/kimi-k2.5"
        - "ZHIPU/GLM-5"
      litellm_model_prefix: "openai/"
    deepseek:
      label: "DeepSeek"
      models:
        - "deepseek-v4-flash"
        - "deepseek-v4-pro"
      litellm_model_prefix: "openai/"
```

规则说明：

- `backend/.env` 中只有环境变量完整的厂商才会被激活
- 前端的 provider 与 model 下拉框全部来自 `backend/config.yaml`
- 若同时激活多个厂商，发起任务时必须显式选择 provider
- DeepSeek 使用 OpenAI 兼容接口接入；`deepseek-chat` 与 `deepseek-reasoner` 将在 2026-07-24 15:59 UTC 弃用，因此默认配置只保留当前 V4 模型

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

- 内置主 agent 提供：`build`、`plan`
- 默认 subagent 提供：`explore`
- 自定义 agent 从 `{codepilot_home}/agents/*.md` 加载，例如本机默认目录为 `~/codepilot/agents/`
- `build / plan` 共用同一个 `AgentLoop`
- 若要触发人工审批，可以在任务文本中加入 `[[approve]]`
- `echo_tool` 为无副作用示例工具，便于验证 LLM tool call 链路
- `context.compression_enabled` 可开启上下文压缩，支持按模型配置 token 阈值、保留最新轮次、LLM 摘要压缩和旧 Tool Result 占位清理

## 新增 Agent

页面中的“管理 Agent”可以创建、编辑、归档和恢复自定义主 Agent。配置保存为 Markdown，并保留 revision 快照；默认 Provider/Model 在当前阶段仅作为 Agent 默认配置保存，现有会话仍显式选择模型。

运行时由 `AgentRuntimeManager` 统一管理，最多可保持 5 个 Agent 启动；每个 Session 使用独立 SessionRunner，资源 API 按 `agent_id/session_id/run_id` 精确路由。CODE-50 仍限制全局一个活动 Run，CODE-51 将负责开放 5 Run 并发和 workspace 写入租约。

Agent 分为两类：

- 主 agent：`kind="agent"`，会通过 `/api/config` 返回给前端，可在前端下拉框直接选择。
- subagent：`kind="subagent"`，不会出现在前端下拉框，只能由主 agent 通过 `task` 工具同步分派。

Agent 配置采用 Markdown 文件声明，文件头是 YAML frontmatter，正文就是该 Agent 的 system prompt。

内置 agent 位于 `backend/src/codepilot/session/agent_profiles/`，当前固定包含 `build`、`plan`、`explore`。普通自定义 agent 不要改内置目录，放到运行态目录：

```text
{codepilot_home}/agents/
```

默认 `codepilot_home` 是 `~/codepilot`，因此本机自定义 agent 目录通常是：

```text
~/codepilot/agents/
```

新增主 agent 的步骤：

1. 创建自定义 agent 目录：

```bash
mkdir -p ~/codepilot/agents
```

2. 新建一个 `.md` 文件，例如 `~/codepilot/agents/review.md`：

```markdown
---
name: review
kind: agent
description: 执行代码审查，优先发现缺陷、风险和缺失测试。
tools:
  - bash_tool
  - read_file
  - load_skill
  - webfetch
  - todo_write
  - todo_read
  - question
readonly: true
can_call_subagent: false
---
你是一个代码审查 Agent。你的首要目标是发现真实缺陷、行为回归、风险和缺失测试。

输出时优先列出问题，按严重程度排序，并给出文件和行号。
```

3. 重启后端。`kind: agent` 的自定义 agent 会通过 `/api/config` 返回给前端，可在前端下拉框选择。

4. 如需设为默认 agent，修改 `backend/config.yaml`：

```yaml
agent:
  default_agent_name: "review"
```

新增 subagent 的步骤：

1. 同样在 `{codepilot_home}/agents/` 下创建 `.md` 文件，例如 `~/codepilot/agents/search.md`：

```markdown
---
name: search
kind: subagent
description: 只读搜索和定位代码上下文。
tools:
  - bash_tool
  - read_file
  - load_skill
  - webfetch
readonly: true
can_call_subagent: false
---
你是一个只读搜索 subagent。你只负责定位文件、阅读上下文、总结发现，不修改任何文件。
```

2. 重启后端。不需要修改前端。`TaskTool` 会自动扫描所有 `kind="subagent"` 的 profile，并把它们写入 `task` 工具描述。

字段说明：

- `name`：agent 名称，必须全局唯一；自定义 agent 不能覆盖内置 `build`、`plan`、`explore`。
- `kind`：`agent` 或 `subagent`。
- `description`：简短用途说明，会展示给前端或写入 `task` 工具描述。
- `tools`：该 agent 可见的工具名列表。工具必须已在后端 `ToolRegistry` 注册。
- `readonly`：是否只读；只读 agent 不应配置写入类工具。
- `max_iterations`：可选；不填时主 agent 使用 `agent.max_loop_iterations`，subagent 使用 `agent.subagent_max_loop_iterations`。
- `can_call_subagent`：是否允许调用 subagent。设为 `true` 时，`tools` 必须包含 `task`；subagent 不允许再调用 subagent。

安全与测试要求：

- 只读 agent 不要配置 `write_file`、`edit_file` 等写入工具。
- subagent 不要配置 `task`，避免递归分派。
- Agent 专属工具必须做双重约束：除 Markdown `tools` 字段外，还要在工具 `execute()` 内通过 `context.agent.name` 做运行时校验。
- 如果新增工具，不只要写到 agent 的 `tools` 字段，还必须先在后端运行时注册到 `ToolRegistry`。
- 调整内置 agent 或 loader 行为时，更新 `backend/tests/test_agents.py` 和相关权限断言；新增 subagent 时，必要时同步更新 `backend/tests/test_task_tool.py` 中的 `task` 工具描述断言。
- 验证命令：`cd backend && uv run pytest`；若影响前端 agent 下拉或配置返回，再执行 `cd frontend && pnpm build`。

## MCP 工具配置

MCP Server 的连接配置与 Agent 权限相互独立：`backend/config.yaml` 只决定服务是否启用，Agent Markdown 的 `tools` 字段决定该 Agent 是否能看到服务发现出的工具。

```yaml
mcp:
  servers:
    filesystem:
      enabled: true
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      cwd: "."
      env_from_process: {}
      requires_approval: true
      timeout_seconds: 120
      max_output_chars: 50000

    remote:
      enabled: true
      transport: streamable_http
      url: "https://example.com/mcp"
      headers_from_env:
        Authorization: MCP_AUTHORIZATION
      requires_approval: true
      timeout_seconds: 120
      max_output_chars: 50000
```

`env_from_process` 和 `headers_from_env` 的值是后端进程环境变量名，真实密钥不要写入 `config.yaml`。远程服务默认要求 HTTPS，只有回环地址允许 HTTP；stdio 的 `cwd` 必须位于当前 workspace 内。

Agent 按服务授权，例如：

```yaml
tools:
  - read_file
  - mcp:filesystem
  - mcp:remote
```

`mcp:filesystem` 会暴露该服务启动时发现的全部工具，实际传给 LLM 的函数名为 `mcp__filesystem__<tool>`，并保留远端参数 schema。首版不支持 `mcp:*`，也不会因为配置了 Server 就自动授权任何 Agent。MCP 调用默认需要人工审批；仅应对可信服务显式设置 `requires_approval: false`，无人值守定时任务无法执行需要审批的 MCP 工具。

## 当前约束

- 一个后端实例只负责一个 workspace
- `workspace_path` 固定为后端启动时所在目录
- 当前只支持一个 active session
- Web 连接断开不会中断后台 AgentLoop

## 后续扩展方向

- 更多真实工具
- 更完整的 subagent 编排
- 持久化存储升级
