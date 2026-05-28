# Repository Guidelines

## Project Structure & Module Organization

本仓库是前后端分离的 CodePilot 原型。后端位于 `backend/`，核心包在 `backend/src/codepilot/`：`api/` 提供 FastAPI 路由，`session/` 管理 Agent 会话流，`tools/` 放内置工具，`llm/` 封装 LiteLLM，`memory/` 处理 jsonl 存储。后端测试位于 `backend/tests/`。前端位于 `frontend/`，React 入口为 `frontend/src/main.tsx`，主界面在 `frontend/src/App.tsx`，样式在 `frontend/src/styles.css`。运行期文件写入根目录 `.run/`，不要提交日志、PID、密钥或本地缓存。

## Build, Test, and Development Commands

- `./dev.sh start`：同时启动后端 `127.0.0.1:8000` 和前端 `127.0.0.1:5173`。
- `./dev.sh stop` / `./dev.sh restart`：停止或重启开发服务。
- `cd backend && uv sync --extra dev`：安装后端依赖与测试依赖。
- `cd backend && uv run pytest`：运行后端测试。
- `cd backend && uv run uvicorn codepilot.main:app --app-dir src --reload --host 127.0.0.1 --port 8000`：单独启动后端。
- `cd frontend && pnpm install`：安装前端依赖。
- `cd frontend && pnpm dev`：启动 Vite 开发服务。
- `cd frontend && pnpm build`：执行 TypeScript 构建和 Vite 打包。

## Coding Style & Naming Conventions

Python 使用 4 空格缩进、类型标注和清晰的模块边界；文件、函数、变量使用 `snake_case`，类使用 `PascalCase`。TypeScript/React 组件使用 `PascalCase`，普通变量和函数使用 `camelCase`。优先沿用现有直接实现风格，避免为一次性逻辑增加抽象。关键分支、边界处理和不直观实现需要中文注释；不要添加复述代码的空洞注释。

## Tool Development Guidelines

新增内置工具时，工具实现统一放在 `backend/src/codepilot/tools/`，继承 `BaseTool`，在 `ToolSpec` 中声明全局唯一 `name`、面向 LLM 的 `description`、严格的 `input_schema`、`can_parallel`、`requires_approval` 和 `timeout_seconds`，并实现异步 `execute()`。较长工具说明优先放在 `backend/src/codepilot/tools/descriptions/`，避免把复杂提示词硬编码在工具类中。

工具接入必须同时完成三步：在 `backend/src/codepilot/tools/__init__.py` 导出工具类；在 `backend/src/codepilot/main.py` 注册到 `ToolRegistry`；在 `backend/src/codepilot/session/agents.py` 的目标 `AgentProfile.allowed_tools` 中加入工具名。注册到 `ToolRegistry` 只表示运行时知道该工具，不代表任何 Agent 可用；`allowed_tools` 才决定当前 Agent 调用 LLM 时能看到哪些工具 schema。

Agent 专属工具必须做双重约束：除 `allowed_tools` 限制外，还要在 `execute()` 内通过 `context.agent.name` 做运行时校验，避免模型历史、手工构造请求或后续编排绕过 Agent 权限。可参考 `write_plan` 的 plan agent 限制方式。

工具安全与性能默认从严：文件类工具必须复用 workspace 路径校验，禁止访问工作区外路径；写入、删除、外部命令、网络调用等高风险工具默认应开启审批或使用白名单参数；只有只读、无副作用、互不影响的工具才允许 `can_parallel=True`；工具输出必须截断或分页，避免大结果撑爆 LLM 上下文。

## Testing Guidelines

后端使用 `pytest` 与 `pytest-asyncio`，测试文件命名为 `test_*.py`。新增修复应先覆盖可复现行为，再实现代码；涉及异步会话、工具调用、上下文压缩或配置加载时，应补充对应单元测试。前端当前未配置测试框架，至少执行 `pnpm build` 验证类型与打包。

## Commit & Pull Request Guidelines

当前历史只有初始提交，后续请使用中文、祈使式提交信息，例如 `修复会话恢复的事件重放顺序`。PR 应说明变更目的、主要实现、验证命令和潜在风险；涉及 UI 时附截图或录屏；涉及配置时说明新增环境变量或迁移步骤。不要在 PR 中混入无关格式化或顺手重构。

## Security & Configuration Tips

以 `backend/config.example.yaml` 和 `backend/.env.example` 为模板创建本地配置。真实 `backend/.env`、API Key、会话 jsonl、日志和本地 workspace 数据不得提交。处理文件工具、workspace 路径和 LLM 输入时，注意路径越权、敏感信息泄露和非预期写入风险。
