# Repository Guidelines

## Python Tool 管理契约（2026-10-09）

CODE-71 调用名称修订：展示 name 与模型 call_name 分离。新建 HTTP／网页工具必须填写 1—64 位英文、数字或下划线调用名称，首位不能为数字，禁止 mcp__ 和 code_tool_ 保留前缀；旧版本缺少字段仍使用 ID 派生名，更新省略字段保留已有值，空字符串与 null 拒绝。名称固定在工具版本内，公共更新需重新发布，禁止改写历史或增加旧执行别名。代码工具与平台／MCP 名称冲突须双向拒绝注册，不能覆盖或静默跳过；授权与凭证继续按工具 ID 校验。

CODE-70 保存协议修订：HTTP AgentPayload 必须显式接受 launch_modes 与严格布尔 can_delegate；禁止将未知字段策略改为忽略以绕过校验。保存继续 exclude_unset，旧客户端省略字段保留原配置。前端表单新增字段必须补真实 HTTP 创建／更新回归，不能仅用 Mock 前端或直接配置服务测试替代。

代码工具与内置 BaseTool、MCP 独立管理，Agent 通过 `tool_ids` 绑定私人／公共工具；旧字段省略默认空数组，更新省略保留，显式空数组清空，null 拒绝。`PythonCodeTool` 实现原工具接口，继续经过原审批、Hook、事件和写租约；实际调用再次检查绑定、用户、readonly、凭证和管理员禁用。只支持同步 `execute(arguments: dict) -> dict` 与辅助 Python 文件，保存、目录和预览不得导入代码。独立子进程使用任务目录和最小环境，不提供文件或网络沙箱；取消与超时回收进程组，结果及诊断有界，业务返回值不得解释为控制协议。

工具内容不可变保存，私人编辑不公开，发布生成独立公共身份；Agent 发布预览将已发布私人依赖映射公共 ID，确认复核根和委派依赖。新 Run 读取最新版本，活动执行固定快照；私人子 Agent 派发时取快照，公共主子在根 Run 固定。凭证属于使用者，通过专用环境变量注入，字段变化必须重新配置。工具注册不得覆盖内置名称，子上下文不得继承父代码工具实例。执行包统一写格式 5、兼容读取 1—4；`resolved_tool_versions` 排除配置比较但独立校验完整性和授权。本条优先于下文旧执行包格式表述。设计与验证见 [Python Tool 管理](docs/agent-platform/python-tool-management.md)，CODE-69 待验收。

## 公共 Agent 发布契约（2026-09-22）

2026-10-09 CODE-72 修订：发布检查汇总资源、引用 Agent、原因及建议；`include_dependencies` 严格布尔默认 false，统一预览可一次确认发布本人未公开工具或恢复本人撤回的原公共版本，禁止自动公开私人更新或解除管理员禁用。正常公共工具不因私人源归档而受阻，作者个人凭证及目录不参与发布准入。调用名冲突按最终公共版本及各 Agent 独立上下文校验。候选摘要只覆盖固定内容，进度独立保存；请求与候选绑定，工具状态及步骤回执同事务提交，重试必须检查成功后发生的撤回、禁用与更新。最终公共发布库→工具库锁序保持到 Agent 提交，部分成功不回滚公共工具，重新发布失败保留原 Agent。旧预览需重新生成，重启不自动继续。设计和验证见 [发布依赖诊断与快捷发布](docs/agent-platform/publication-dependencies.md)。

公共 Agent 由作者主动发布，使用者可直接执行但不能修改定义；本轮不实现公共复制与差异更新。主子配置重新发布后由新 Run 采用。Skill、Hook 作为附属资源交付；私人源编辑不自动公开，再次发布不得覆盖公共资源编辑。公共 Skill 只维护当前文件，load_skill 读取最新内容，禁止恢复 Skill 运行时锁版；公共 Hook 在新 Run 固定主子快照。目录、连接、会话、记忆、审批及容量属于使用者。公共资源采用显式作用域，不借作者身份访问私人目录；私人 Agent 不能直接绑定公共附属资源。公共执行包格式 4，继续读取 1 至 3。设计验证见 [公共发布](docs/agent-platform/public-agent-publications.md)。

## 统一 Agent 与委派交互契约（2026-09-24）

Agent 配置不再永久区分主 Agent 与 subagent，统一使用 `launch_modes` 表达 `direct`、`delegated` 能力，使用 `can_delegate` 与 `delegate_agent_ids` 声明委派关系。旧 `kind`、`can_call_subagent`、`subagent_ids` 只在读取时兼容，保存 revision 必须写新格式。网页交互式 Run 的委派工具审批、Hook 确认和 question 复用根 Run 人工交互通道；根 Session 是等待状态与回复归属的唯一事实来源。拒绝只取消当前委派分支并向根 Agent 返回 `TaskDelegationRejected`，显式停止才取消整个调用栈。定时 worker 仍不等待人工输入。公共执行包格式为 4，继续读取 1 至 3。详细设计与验证见 [统一 Agent 与委派交互](docs/agent-platform/unified-agent-delegation.md)。

## 产品方向与后续开发入口

开始后续 Agent 平台设计或开发前，先阅读根目录 [PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md)。其中记录用户已确认的通用平台目标、八项产品规则、实现差距、验收场景与待确定细节；设计目录登记见 [product-direction-handoff.md](docs/agent-platform/product-direction-handoff.md)。

本文件下文描述当前实现契约。2026-09-17 用户最新确认：主 Agent 在 Run 内固定配置，子 Agent 派发时取自身快照，主子独立控制轮数；不实现配置热更新、共享预算或任务级只读。保留 Agent readonly 和既有 Bash 策略，不改内置 Prompt。子 Agent 可以拥有独立模型与专用工具，继承主任务目录，最多一层委派。此修订优先于旧文档中的实时刷新和共享预算目标。身份归属、凭证隔离和禁止不确定副作用盲目重放等要求继续适用。

组装能力实现与验证见 [agent-assembly.md](docs/agent-platform/agent-assembly.md)，当前待用户验收。配置更新必须区分字段省略与显式空数组；角色创建后不可修改。记忆编辑与运行时写入共用文件锁和原子替换，API 编辑必须校验 revision。插件 Hook 状态按运行上下文隔离，context_patch 进入插件命名空间，不得覆盖用户或授权；工具前 Hook 完成后审批恢复不得重复执行。后端回归、前端测试和构建不能替代 CODE-53 发布门禁。

组装后的运行契约优先于下文旧描述：task 模型描述按当前用户及委派清单生成，允许个人子 Agent；目录只使用管理员允许根内的已有目录，旧 Session 保留原目录，子 Agent 始终继承父目录。Skill 不再固定执行版本，load_skill 每次按授权身份读取最新正文及受控相对资源，不执行脚本；二进制通过受控文件下载提供。启动仅加载 Skill 简介，不扫描附件或生成共享快照。主进程和 worker 使用同一资源解析方法，execution_config 只记录 Skill 身份，Hook 和连接继续固定版本；执行包格式版本 2，继续读取旧格式 1。

2026-09-18 版本修订：个人 Skill 仅保留当前文件集，以短文件锁保护读取、原子指针发布和旧内容清理；revision 仅用于编辑冲突，不允许按历史 revision 执行。旧存储首次访问时校验并迁移当前内容后移除旧历史；废弃的共享快照不再读取或生成。Skill 的旧执行包字段只在读取兼容边界丢弃，不能重新加入 AgentProfile 或运行快照。Hook 内容摘要用于执行及完整性校验，version_number 用于展示保存序号；无内容变化不加版，恢复旧内容继续递增，归档不加版、复制从 1 开始。详情展示第 N 版及更新时间，摘要折叠为短标识并可复制。设计与验证见 docs/agent-platform/skill-live-and-hook-versions.md。

技能与 Hook 管理页均仅展示只读引用情况，不提供选择或配置 Agent 的入口；绑定与解绑必须从 Agent 配置发起，保留返回原配置的导航及草稿。Hook 保存仅提交当前执行方式的专属字段，类型切换草稿仍保留在当前页面；仅 Command 校验命令名称及脚本引用，旧客户端的 Prompt／HTTP 残留命令字段不能触发脚本检查，通用文件安全校验不变。

## Project Structure & Module Organization

2026-09-24 前端交互设计契约：Agent Studio 首页使用统一 Agent 模块网格，支持 `direct` 的 Agent 展示独立运行状态与最新 Session；仅支持 `delegated` 的 Agent 同样进入目录，但只展示委派配置摘要并进入配置页，不得提供直接启动、会话入口或伪造根运行态。内置与共享配置只读，个人配置编辑权限不因启动方式变化。不再使用状态泳道、卡片派发输入或会话工作抽屉；直接运行 Agent 点击模块进入可由 hash 路由恢复的交互页。左侧只保留一级导航、主题、容量和用户入口，会话历史与自动化作为交互页检查器。首页只消费目录、低频运行态和 `GET /api/agent-sessions/recent` 批量摘要，不得加载 Session 列表、replay、会话级 SSE 或增加逐 Agent 请求。视觉与响应式规则统一维护在 `frontend/src/features/agent-studio/agent-studio-refined.css`，桌面与移动端复用同一业务 DOM，不得为视觉改造改变 Run、权限、SSE 或资源保存语义。设计与验证见 [frontend-interaction-redesign.md](docs/agent-platform/frontend-interaction-redesign.md)。

2026-09-18 资源管理体验契约：一级导航为 Agent、技能、执行钩子，沿用 hash 地址并保留跨页草稿。SkillManagerPage 负责独立管理，SkillEditor 只负责 Agent 选择；技能元数据必须使用结构化 YAML 文档解析，保留未知字段，解析失败不得覆盖。技能页及解析依赖懒加载。Hook 新建使用三步向导，旧配置直接编辑并保留命令参数；测试只允许已保存版本，串行轮询且终态不可倒退。关联入口仅导航到 Agent 配置，不自动授予能力。详情、引用和搜索的异步结果需校验取消与当前资源，凭证不持久化到浏览器。验证记录见 docs/agent-platform/resource-management-redesign.md。

2026-09-22 Agent 配置体验契约：配置工作区固定为概览、指令、能力、运行、记忆、连接六个分区，桌面使用分区轨，900px 及以下使用吸顶分区选择器；分区切换不得丢失草稿。Agent revision 仍由统一保存按钮提交，记忆正文和个人凭证继续独立保存，凭证不得进入 Agent 表单状态或浏览器持久化。能力搜索、筛选和状态计数只处理已加载目录，复杂度为 O(n)，不得因此新增逐项详情请求。字段省略、`null` 继承与空数组显式禁用的协议语义保持不变。只读、共享、内置和无效 Agent 必须可完整查看，但禁止直接编辑与保存，并保留复制为个人 Agent 的入口。验证记录见 docs/agent-platform/resource-management-redesign.md。

2026-09-18 Skill／Hook 管理契约：Skill 和 Hook 在独立页面维护，Agent 配置页仅绑定资源。个人 Hook 使用 `personal:<UUID>`，定义与脚本完整原子发布，不可变版本在主 Run／子上下文开始时固定。个人 Hook 不得注册到平台全局表；外部协议与内部 HookResult 分离，禁止身份、授权、工具调用或任意事件注入。Hook 人工确认独立于工具自动审批开关，原调用栈等待并继续剩余 Hook，不重跑模型或工具；无交互子 Agent／worker 拒绝等待。最后纯文本轮也必须运行 loop.after，工具结果先于 tool.after 发布，已有失败和取消不能被收尾覆盖。已发出的外部操作结果不确定时失败且禁止自动重放。HTTP 个人凭证绑定目标和字段映射，Command 使用最小环境及现有写租约，不提供 OS 沙箱。受控测试保持用户隔离、容量和持久请求幂等，重启不执行。详细契约见 [skill-hook-management.md](docs/agent-platform/skill-hook-management.md)。

本仓库是前后端分离的 CodePilot 原型。后端位于 `backend/`，核心包在 `backend/src/codepilot/`：`api/` 提供 FastAPI 路由，`session/` 管理 Agent 会话流，`tools/` 放内置工具，`skills/` 管理按需加载的技能运行时，`scheduler/` 管理定时任务和独立 worker，`llm/` 封装 LiteLLM，`memory/` 处理 jsonl 存储。后端测试位于 `backend/tests/`。前端位于 `frontend/`，React 入口为 `frontend/src/main.tsx`，主界面在 `frontend/src/App.tsx`，样式在 `frontend/src/styles.css`。运行期文件写入 `storage.codepilot_home/workspace/<workspace_id>/`，不要提交日志、PID、密钥或本地缓存。

## Build, Test, and Development Commands

- `./dev.sh start`：同时启动后端 `127.0.0.1:8000` 和前端 `127.0.0.1:5173`。
- `./dev.sh stop` / `./dev.sh restart`：停止或重启开发服务。
- 后端热重载必须显式限定 `--reload-dir` 为 `backend/src`，禁止默认递归扫描含 `.venv` 的后端根目录，避免空闲时持续遍历依赖文件。
- 启动前必须完成冻结锁文件依赖整备；普通启动使用 `uv run --no-sync --offline`，禁止 Corepack 联网下载。前端固定 5173 并使用 `--strictPort`；前后端共享 20 秒就绪期限，HTTP 200 与监听进程归属均通过后才报告成功。失败仅回收本次创建且归属匹配的进程组，不影响已有组件或其他端口占用者。
- 启动脚本回归入口：`backend/.venv/bin/python -m pytest backend/tests/test_dev_script.py`，测试只使用临时项目和替身，不操作真实业务服务。
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

工具接入必须同时完成三步：在 `backend/src/codepilot/tools/__init__.py` 导出工具类；在运行时装配处注册到 `ToolRegistry`；在目标 Agent Markdown 文件的 `tools` 字段中加入工具名。注册到 `ToolRegistry` 只表示运行时知道该工具，不代表任何 Agent 可用；Agent Markdown 的 `tools` 才决定当前 Agent 调用 LLM 时能看到哪些工具 schema。

工具必须做双重约束：除 schema 的 `allowed_tools` 过滤外，执行时再次校验能力快照，避免模型历史或手工请求绕过授权。真正专属工具可以继续校验 Agent 身份；`write_plan` 和 `long_memory_write` 允许显式分配给个人 Agent，以支持内置配置复制，不再仅按展示名称授权。计划文件位于任务目录 `.codepilot/plans/<session_id>.md`，属于 workspace_mutation，使用现有写入租约并校验路径。

动态 MCP 工具使用服务级权限标记：`backend/config.yaml` 中的 `mcp.servers` 只控制连接，不授予 Agent 权限；Agent Markdown 必须在 `tools` 中显式声明 `mcp:<server_name>`，运行时才允许暴露该服务发现出的 `mcp__<server_name>__<tool_name>` schema。首版禁止 `mcp:*`。MCP 权限必须同时由 `ToolRegistry` 过滤和 `McpToolAdapter.execute()` 运行时校验，不能通过硬编码 Agent 名称或启动时篡改 `allowed_tools` 绕过文件式配置。

MCP 连接必须使用官方 Python SDK，主进程和 scheduler worker 都要纳入异步启动与关闭生命周期。团队密钥通过 `env_from_process` 或 `headers_from_env` 引用进程环境变量；个人凭证只能填写管理员声明的 credential_fields，Fernet 加密落盘，独立环境密钥 CODEPILOT_CONNECTION_KEY 不得写入仓库或传给 Bash、Command Hook、MCP 子进程。私人连接按上下文注册 schema，不得污染平台工具目录；轮换或撤销后调用校验必须失败，不能回退团队身份。同服务所有连接共用并发 5、总在途 25 的容量。默认要求人工审批，stdio 工作目录不得越出实际任务目录，工具输出必须截断，图片 base64 不得进入日志或会话 JSONL。

工具安全与性能默认从严：文件类工具必须复用 workspace 路径校验，默认禁止访问工作区外路径；`read_file` 读取工作区外文件和 `bash_tool` 使用工作区外 `cwd` 只能在人工审批通过后执行，或在 `human_in_the_loop.enabled=false` 的全自动模式下直接执行；该开关只控制工具审批，不限制网页交互式 Run 的 `question` 回答。scheduler worker 没有用户回答入口，必须禁止等待 `question`；网页 Run 的委派执行必须复用根 Run 人工交互通道。写入、删除、外部命令、网络调用等高风险工具默认应开启审批或使用白名单参数；只有只读、无副作用、互不影响的工具才允许 `can_parallel=True`；工具输出必须截断或分页，避免大结果撑爆 LLM 上下文。

## Agent & Subagent Runtime Guidelines

2026-09-22 错误反馈契约：Agent 配置解析保留安全的分类原因；列表和详情的 validation_issues、启动及新 Run 错误的 issues 包含 code、field、message、suggestion。不得输出原始解析异常、绝对路径、配置正文或校验 input。既有业务错误码与状态保持兼容；子 Agent 依赖标明委派身份。页面提供处理建议和配置入口，技术码折叠，迟到的启动错误必须校验当前 Agent 归属。见 docs/agent-platform/agent-error-feedback.md。

发送统一使用 `POST /api/agents/{agent_id}/runs`，经 Manager 的 `submit_message` 分流，底层 `start_run` 只启动。运行中追加返回原 Run，不能额外占容量或修改当前模型；`expected_run_id` 不匹配必须冲突。用户加请求 ID 幂等先于分流，指纹包含正文、图片摘要、模型、metadata 和预期 Run；旧追加重试不能开启新 Run。

主 Agent 的 SessionInbox 接收、决策选取、人工等待、停止和正常收尾共用短临界区，不能跨模型或工具调用持锁。每 Session 最多 20 条待纳入消息，先追加 JSONL 并 fsync 后接收；选取后到请求构造前保护新输入原文，构造后记录纳入 Run 和轮次，准备期间新消息留到下一轮。已纳入仅表示进入本地请求上下文。等待人工与正在停止拒绝普通发送；服务重启不执行，旧待处理输入先于下次人工新消息纳入。

轮数耗尽必须为 `CANCELLED` 且 `stop_reason=max_iterations`，最后允许的一轮正常完成仍为 `COMPLETED`；主 Agent、子 Agent、定时 worker 与前端回放均不能把超限当成功。停止后在原 Session 发送启动新 Run，不引入暂停状态或工具检查点。Session SSE 回执按消息 ID 去重、状态只前进；追加响应不能清空流式文本或重置 Run，正文与附件不得进入聚合事件。设计与验证见 `docs/agent-platform/message-submissions.md`。

运行时控制接口必须使用完整的 `user_id + agent_id + session_id + run_id` 归属键；不得新增全局 current user/agent/run 作为事实来源。每个 SessionRunner 只能管理一个 Session 的可变执行状态，跨 Session 并发由运行时协调层控制。用户运行期期望启动状态写入 `workspace/users/<user_id>/agent-runtimes.json` 时必须原子替换并使用 0600 权限。重启时所有用户的活动 Run 统一转为 `CANCELLED/service_restarted`，但只恢复启用用户的 Agent；禁用用户的期望状态必须持久化为 `STOPPED`，不能重放 Tool、MCP 或 LLM 副作用。

HTTP API 只能依赖 `AgentRuntimeManager`，不能直接持有 SessionRunner 的 Task、Event、审批或 Question holder。Manager 获取配置时必须使用 `AgentConfigService` 的不可变快照；Run 启动后固定 revision。Run 终态必须比较并更新，确保 cancel、Agent stop、watcher 和 shutdown 只释放一次容量。恢复期同样必须执行全服务与每用户 started Agent 上限；超限 Agent 不启动、不排队，必须持久化为带容量错误码的停止状态。`agent-runs.jsonl` 与控制事件必须追加、flush、fsync；末行截断可归档后恢复完整前缀，中间损坏必须拒绝新 Run。Session 历史索引需要识别 Scheduler 跨进程写入，外部 session ID 禁止拼入 glob。

交互式活动 Run 全服务上限为 5、每用户上限为 2，同一 Session 上限为 1，不得隐藏排队。并发相同 `client_request_id` 只在同一用户内共享请求预留并返回同一 Run；容量、Session 和 Agent 启停检查必须在同一 Manager 临界区完成。`workspace_mutation` Tool 必须持有 RunRef 级跨进程共享 workspace 写入租约至执行收尾，subagent 继承父 Run 租约。MCP 每服务最多 5 个并发调用、20 个 pending，已发出调用失败不得自动重放；Bash 取消必须回收整个进程组。

完整 Session SSE 与低频运行态 SSE 必须分离；聚合流不得传 token、Prompt、附件或 Tool 大结果。所有订阅队列固定上限 1000，溢出后要求客户端重连回放，不能阻塞 Agent。Scheduler worker 只构建 Execution Bundle，不得创建 Manager、执行 recover 或写入 `agent-runtimes.json`。

Agent Studio 只能调用资源化 `/api/agents/*` 与 `/api/agent-runtimes*` 接口，不得重新依赖 `/api/session/*` 全局兼容指针。前端选择状态使用 `agent_id + session_id`；fetch、replay 和 SSE 必须用 generation、AbortController 与事件归属校验隔离快速切换。聚合 SSE 全局最多一条，高频 SSE 只为当前查看的 Session 保留一条，事件去重、消息列表和历史 DOM 都必须有固定上限。

Agent Studio 的体验层统一放在 `frontend/src/features/agent-studio/agent-studio-refined.css`，视觉重构不得改动 API 请求与 SSE 协议。桌面与移动端必须复用同一业务 DOM；900px 以下的导航和检查器使用抽屉，抽屉层级必须高于带模糊效果的遮罩。交互元素需保留稳定的可访问名称，并支持 `prefers-reduced-motion`。

Agent 配置采用 Markdown 文件声明。内置 Agent 位于 `backend/src/codepilot/session/agent_profiles/`，必须固定包含 `build`、`plan`、`explore` 三个文件；共享 Agent 位于 `storage.codepilot_home/agents/shared/*.md`，用户私有 Agent 位于 `storage.codepilot_home/users/<user_id>/agents/*.md`。文件头使用 YAML frontmatter，至少声明 `name`、`launch_modes`、`description`、`tools`、`readonly`、`max_iterations`（可省略以使用全局默认）、`can_delegate`，委派清单使用 `delegate_agent_ids`；正文即该 Agent 的 system prompt。`launch_modes` 至少包含 `direct`、`delegated` 之一。私有与共享 Agent 允许同名，运行时必须使用 `agent_id` 解析，名称只作展示快照。历史 revision 必须按 `agent_id + revision_id` 的单个确定路径读取并校验摘要、身份、大小和符号链接；配置更新保留旧 revision 可执行性，归档立即禁止所有新 Run。

`session_id` 是持久化和前端回放边界，主 Agent 与 subagent 的消息可以写入同一个 session jsonl。`context_id` 是 LLM 上下文和压缩边界，主 Agent 与每次 `task` 派发的 subagent 必须使用不同上下文；构造 provider messages、上下文压缩和 replay 压缩替换时都必须按 `context_id` 过滤，不能直接把整场 `session.messages` 作为当前 Agent 的 LLM 输入。

委派执行只能通过 `task` 工具由 `invocation_role=root` 的执行同步发起，目标必须启用 `delegated`，调用方必须启用 `can_delegate`、拥有 `task` 并通过 `delegate_agent_ids` 校验；禁止自调用、超过一层委派和跨用户访问私人配置。运行事件必须带 `invocation_role`、`agent_id`、`context_id`、`parent_call_id`，并保留兼容 `agent_kind`。`SessionCompactedEvent` 若只压缩某个上下文，必须写入 `scope="context"` 和对应 `context_id`，回放时只替换该上下文消息，不能覆盖整个 session。

## Scheduler Runtime Guidelines

2026-09-23 无人值守与会话工作台契约：每次真正启动 worker 前解析最新 Agent revision，Run 内固定；旧任务模型/目录保持覆盖，新表单支持跟随 Agent。同任务最多一个待执行或未完成清理的 Run，重叠触发记录 SKIPPED；停用仅取消 pending，删除保留历史。保持独立容量及一小时超时，不合并网页容量或共享预算。普通工具自动审批但不绕过授权、readonly、路径、凭证或写租约；主子去除 question，Hook 人工确认明确失败，绝不转交网页等待。

主进程预分配 Session，worker 使用 Run 专属令牌及完整归属上报递增序号；终态优先且不可倒退。worker 退出后按已落盘会话事实一次对账，缺少 HTTP 终态不能重跑任务。停止确认进程树清理、存储收尾后才解除 Session 互斥；运行期间只读可停止，之后人工消息启动新网页 Run。重启中断旧活动及排队，不重放工具，周期只计算未来触发。worker 调度工具经专属认证转交主进程，避免两个进程写调度配置；不在 worker 创建 Manager。

当前会话工作台通过有界字节游标读取定时事件，不逐 Token 直播；状态查询不能创建可写 Runner。活动执行按身份增量索引，调度循环不遍历完整运行历史。成果仅接受成功文件工具的结构化输出、受控目录与归属校验，下载再次验证路径。详情与轮询按 Agent/Session/请求代次隔离。设计及验证见 docs/agent-platform/scheduled-workbench.md；此修订优先于旧文中固定创建时 revision、人工转交与实时推送的表述，继续跳过 FlowDeck，待用户验收。

定时任务由主进程调度、独立 worker 子进程执行。每个用户的任务配置写入 `workspace/users/<user_id>/schedules.json`，必须使用临时文件原子替换；运行状态写入同目录的 `schedule-runs.jsonl`，必须追加完整状态快照，不要覆盖历史。动态用户创建、启用或更新启用任务时必须先确保该用户的 Schedule runner 已启动；查询、禁用和删除不得为此创建后台循环。worker 的 session 写入该用户的 `sessions/` 目录，并且创建会话时必须在服务端 metadata/session_meta 中保留 `source="schedule"`、`schedule_task_id`、`schedule_run_id` 和 `schedule_task_name`。Schedule 与 worker 执行束必须固化 `user_id + agent_id + revision_id`，名称只作展示快照；不得按名称降级执行。缺失、损坏或归档 revision 只允许把本次 Run 标记为失败，不得终止调度循环；无法稳定映射的迁移任务必须禁用并在已有 metadata 中记录原因。

worker 执行目录只作为项目工作目录，不能向用户项目目录写入 CodePilot 运行态文件。主进程只接受本机 worker 上报，并必须校验 `schedule_worker_token`；删除任务时只取消未启动的 pending run，不强杀已经运行的 worker。第一版只支持 `once`、`interval`、`daily`、`weekly` 四类触发，不引入 cron 或实时 SSE 推送。

## Skill Development Guidelines

运行期 skills 默认从 `storage.codepilot_home/skills` 加载一级目录的简介，不能递归扫描附件生成执行快照。system prompt 只注册已选择 skill 的名称和描述；完整规范通过 `load_skill` 按需读取最新内容，默认 SKILL.md，也支持受控相对资源路径，不执行附带脚本。修改不会自动替换已进入模型上下文的旧内容，再次加载才读到最新正文。私人归档、归属和路径限制在读取时再次校验。

## Long Memory Guidelines

长期记忆按用户写入 `storage.codepilot_home/users/<user_id>/memory/`，全局记忆为 `_global.md`，Agent 专属记忆为 `<agent_id>.md`；构造 Prompt 时先合并全局记忆，再合并 Agent 专属记忆。文件必须包含 YAML frontmatter，`long_memory_write` 必须按实际能力快照校验，不能只按 Agent 名称授权。读取时必须剥离 frontmatter，只注入正文记忆。

## Attachment Runtime Guidelines

用户上传附件属于 CodePilot 运行态数据，必须保存到 `workspace_dir/users/<user_id>/attachments/<session_id>/<message_id>/`，会话 JSONL 只保存 `FilePart` 元数据、受控预览 URL 和本地文件路径，不得持久化 base64 原文。首期附件仅支持 `image/png`、`image/jpeg`、`image/webp` 和 `image/gif`，单图默认不超过 5MB，单条用户消息最多 4 张；前端可做提前拦截，但后端必须基于文件头再次校验 MIME、大小、路径与记录 owner。

附件预览接口只能读取当前 workspace 的 attachments 目录，必须清理文件名并校验解析后的路径仍位于目标消息目录内。`read_file` 读取图片时可返回图片附件元数据；LLM 请求构造阶段再按需把图片编码为 data URL，日志与持久化记录不得写入图片 base64。

## Testing Guidelines

后端使用 `pytest` 与 `pytest-asyncio`，测试文件命名为 `test_*.py`。新增修复应先覆盖可复现行为，再实现代码；涉及异步会话、工具调用、上下文压缩或配置加载时，应补充对应单元测试。前端使用 Vitest、jsdom、React Testing Library 和受控 MockEventSource；涉及 hooks、请求竞态或 SSE 生命周期的改动必须执行 `pnpm test --run`，并继续执行 `pnpm build` 验证类型与生产打包。

## Commit & Pull Request Guidelines

## Agent 平台设计文档

涉及 Agent 配置、运行时拓扑、会话/Run 生命周期、并发、Tool/MCP 权限或验证结论的改动，设计与验证证据必须写入 `docs/agent-platform/`，并更新该目录的 `README.md`。不得只在聊天、Plane 评论或日志中保存架构结论；结果文件不得包含绝对用户目录、密钥、Prompt 正文或附件 base64。

共享 Agent 配置的活动 Markdown 位于 `{codepilot_home}/agents/shared/`，用户私有配置位于 `{codepilot_home}/users/<user_id>/agents/`；各自的归档文件位于 `.archived/`，revision 快照位于 `.revisions/`。配置写入必须使用受控文件名、临时文件和原子替换，不能物理删除 revision。能力目录不得返回 MCP 命令、URL、工作目录、环境变量、Header、密钥或原始 Tool schema；新增 Tool 必须声明副作用类别和是否可分配给私有 Agent。

当前历史只有初始提交，后续请使用中文、祈使式提交信息，例如 `修复会话恢复的事件重放顺序`。PR 应说明变更目的、主要实现、验证命令和潜在风险；涉及 UI 时附截图或录屏；涉及配置时说明新增环境变量或迁移步骤。不要在 PR 中混入无关格式化或顺手重构。

## Security & Configuration Tips

`backend/config.yaml` 是可提交的项目配置，用于维护服务端口、模型清单和工具策略；真实 `backend/.env`、API Key、会话 jsonl、日志和本地 workspace 数据不得提交。处理文件工具、workspace 路径和 LLM 输入时，注意路径越权、敏感信息泄露和非预期写入风险。

源码发布支持可信局域网内的应用级多用户隔离。后端和开发脚本不得放宽到非回环监听；局域网入口必须由同机反向代理在每个精确配置的 HTTPS Origin 下提供前端与 `/api`，不得信任任意来源的 Forwarded Header。认证不构成 OS、项目文件或 Bash 沙箱，只适用于共享 workspace 的互信用户。配置、Session replay 和附件响应必须使用 `no-store`；健康探针只能返回稳定组件状态和计数，不得返回用户信息、路径、Agent 配置、MCP 地址或底层异常。

用户正文、附件编码、文件名、审批备注、Question 回答和 metadata 必须在进入 Runtime 前执行有限长度校验。LLM 请求日志默认关闭；开启后也只能记录 Provider、Model、数量和耗时摘要。日志与发布结果必须递归脱敏认证信息、图片 data URL 和绝对用户目录。

发布前必须执行 CODE-53 一键源码门禁。确定性并发、真实 Provider、本地 MCP、依赖漏洞审计和敏感信息扫描任一失败或外部审计不可用，都不得声明发布通过；结果文件不得保存 Prompt、模型输出、请求 ID 或真实用户数据。
