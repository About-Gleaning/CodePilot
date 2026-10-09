# Agent 组装能力实施记录

日期：2026-09-17。状态：实现及受控回归完成，待用户验收；未作发布结论。

## 已确认边界

- 保留 readonly 与 Bash 策略，不改内置 Agent Prompt。
- 主 Run 固定快照，子 Agent 派发时读取自身配置，分别限制轮数。
- 子 Agent 独立模型和工具，继承父目录，一层委派。
- 不实现配置热更新、共享预算、任务级只读或独立任务模型。
- 用户明确授权跳过 FlowDeck；未作提交或发布。

## 当前实现

- Agent 创建支持角色和轮数；角色不可变；新写配置标记格式版本 2。更新按实际提交字段合并，省略保留，空数组清空；历史 revision 不改写。
- API 与配置页支持 Hook 选择、委派清单、记忆开关。子 Agent 导航进入配置页，不进入直接执行看板或定时任务选择。
- 子 Agent 有模型配置时使用自己的 Provider、Model 和思考配置；旧配置无模型时继承父模型。task 描述按用户和允许清单生成，执行入口再次校验清单和归属。
- 关闭记忆同时禁止 Prompt 注入和记忆工具写入。个人全局、Agent 记忆分别查看、编辑及清空；编辑使用摘要 revision，运行时工具与 API 共用文件锁，原子替换文件。
- Command Hook 使用 argv、受限输入输出、超时及进程组回收；HTTP Hook 固定管理员目标，不跟随重定向，返回值按 HookResult 校验。Agent 插件明确报不支持。
- 插件实例按 RuntimeHandles 和 context_id 隔离；上下文补丁放在插件命名空间。工具前阻断不执行工具，工具后结果保留真实工具输出，审批恢复复用已执行的工具前 Hook 结果。
- 模型、工具、Skill、连接、工作目录、Hook 和显式子 Agent 引用缺失可保存为待配置，活动快照阻止新执行。Agent 启动、新 Run、子 Agent 派发及 scheduler 均使用配置服务检查；历史仍可查看和停止。
- 个人 Skill 支持创建、完整文件集原子更新、归档和下载，仅保留当前内容；共享 Skill 只读引用，不生成执行快照。load_skill 按已授权身份读取当前文件；主、子 Agent 和 worker 均不锁定 Skill 版本。配置中的显式空列表仍表示不启用技能。
- 工作目录选择管理员允许根内的已有目录，禁止解析后越界。SessionRunner 只替换 workspace_path，附件、日志、会话及租约保留平台路径。目录贯通 Prompt、文件工具、浏览与自动补全、Bash、Hook 和 MCP；计划写入实际任务目录 `.codepilot/plans/<session_id>.md`，使用现有保守写租约。Prompt 的 plan_path 占位在装配时替换，不修改内置 Prompt 文件。
- 个人连接使用 Fernet 加密文件，API 仅返回身份、目标、版本和撤销状态。服务地址、命令和凭证字段由管理员设置。团队与个人身份显式选择；更换或撤销使旧版本后续调用失败，不能回退团队身份。MCP 按执行上下文发现和路由，同服务所有身份共享并发 5、总在途 25 的上限。
- Hook 参数按管理员声明验证，Prompt 与 argv 参数支持受控替换，不替换命令名；HTTP 使用固定目标和声明 Header 映射。停止信号取消外部 Hook，Command 在退出或取消时回收进程组。Tool 前审批恢复不重跑已完成 Hook，失败后不自动重试外部调用。
- 主进程及 worker 共用 resolve_execution_profile；执行包格式 2 保留配置和资源版本，继续读取旧格式 1。worker 校验用户、归属、revision、依赖及凭证当前版本。会话 execution_config 记录 context、Agent revision、模型和资源版本，不记录秘密。
- 前端资源列表分页，缺失引用保留并提供明确移除入口。复制保留工具清单，去除个人凭证连接，记忆内容与历史不复制。write_plan 与 long_memory_write 以实际能力授权支持个人副本。

## 运维前提与边界

- 个人连接需要管理员设置 CODEPILOT_CONNECTION_KEY（Fernet 密钥），密钥与数据文件分别保管；不自动生成、不回显、不写入执行包。密钥缺失明确待配置。密钥轮换迁移不在本轮管理界面范围内。
- agent.allowed_working_roots 声明额外目录根；默认 workspace 仍可使用。已有 Session 不切换目录；子 Agent 配置中的目录不替代父任务目录。
- MCP 管理配置通过 credential_fields 声明个人可填字段，env_from_process／headers_from_env 声明映射。HTTP Hook 使用 credential_fields 和 config.headers_from_env。验证只做 MCP 初始化及工具发现，或固定 HTTP 目标 HEAD。
- Hook parameters 声明 string／integer／boolean、必填和枚举；参数通过双花括号占位使用。Command 配置 argv 数组，HTTP 配置 url；管理员实现类型 agent 明确显示尚不支持。
- 个人 Skill 文件上限 100 个、单文件 1 MiB、当前文件集 4 MiB；共享 Skill 按请求文件校验，管理页才枚举附件并排除常见开发产物。禁止请求路径穿越和符号链接；不执行 Skill 脚本。归档后运行时禁止读取，管理页可查看当前内容。
- 旧字段省略保持原能力，显式空列表清空。旧子 Agent 无模型继续继承，新建子 Agent 要求模型。历史配置与 Session 文件不批量改写。
- 连接生命周期为执行上下文内复用、结束关闭；不新增跨 Run 常驻个人连接池。连接替换后已发出的远端请求不回滚，后续调用被拦截。

## 验证

- 后端全量：404 passed，1 skipped。命令：`CODEPILOT_HOME=<临时测试目录> backend/.venv/bin/python -m pytest backend/tests -q`。真实 Provider 用例未启用，外部 Hook 和 MCP 行为使用受控测试服务或替身。
- 覆盖旧表单字段保留、不可变角色、独立子模型与父目录、Skill 原子发布／版本／归属／归档、目录越界、记忆 CAS、连接加密／撤销／身份及 schema 隔离、共享容量、Hook 超时／停止／阻断／工具事实保留／审批去重，以及历史 revision 和子依赖检查。
- 追加消息 13 项、主子轮数和停止相关回归包含在全量测试中。
- 前端 `pnpm test --run`：30 passed；`pnpm build` 通过。新增资源编辑测试覆盖记忆冲突保留草稿、Skill 发布失败保留正文、个人凭证提交及清空。
- 浏览器：1440×1000 桌面与 390×844 移动端，受控 API fixture；检查配置页、资源编辑入口、Skill 保存及连接凭证保存。图片位于忽略目录 frontend/output/playwright/assembly-desktop.png、assembly-mobile.png 和 assembly-mobile-connection.png。未修改真实用户记录；这些浏览器结果不代表真实外部服务部署验收。
- 未执行 CODE-53 完整发布门禁，未声明可发布。

## 性能与安全边界

记忆原子写入为 O(n)，正文上限约 1 MiB；相较原追加写增加复制成本，以换取跨进程编辑不丢更新。Hook 输入和每路输出限制为 64 KiB。Skill 保存按有界当前文件集 O(n) 处理，共享简介加载不扫描附件；load_skill 每次读取当前请求文件，不扫描历史。MCP schema 只保留在各上下文注册表，增加身份不扩大调用容量。Hook 输入排除运行时配置和凭证对象，普通日志不记录插件异常原文；Bash、Command Hook 和 MCP 子进程均剥离连接加密主密钥。用户级隔离仍建立在可信 OS／共享 workspace 前提上，不声明 Bash 沙箱或抵御本机恶意文件系统操作。
