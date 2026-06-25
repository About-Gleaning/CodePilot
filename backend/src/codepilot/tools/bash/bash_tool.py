from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.config.settings import BashToolSettings
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolPreflightResult, ToolSpec
from codepilot.tools.bash.models import BashRequest
from codepilot.tools.bash.policy import decide_build_policy, decide_readonly_policy
from codepilot.tools.bash.runtime import BashRuntimeError, resolve_cwd, run_bash_command


BUILD_DESCRIPTION = """在本机 login shell 中执行命令行命令。
- 默认使用 /bin/zsh -lc "<command>"，cwd 默认是当前 workspace 根目录。
- cwd 必须存在且必须是目录；跳出 workspace 时会按人机交互配置请求审批或在全自动模式下直接执行。
- 命令执行会继承当前进程环境变量。
- 根据 Bash 审批配置，命令可能在执行前请求人工确认。
- exit code 非 0 会作为结构化结果返回，不会中断 agent loop。"""

READONLY_DESCRIPTION = """在本机 login shell 中执行只读探查命令。
- 仅允许 readonly allowlist 中的命令，例如 rg、find、cat、sed、head、tail、wc、git status/diff/log/show。
- 允许只读管道组合，例如 rg "foo" backend | head -50。
- cwd 默认位于 workspace 内；跳出 workspace 时会按人机交互配置请求审批或在全自动模式下直接执行。
- 不允许修改源码、依赖、Git 状态或 workspace 业务文件。
- 重定向只允许写入受控 scratch 目录。"""


class BashTool(BaseTool):
    def __init__(self, *, settings: BashToolSettings, timeout_seconds: int) -> None:
        self._settings = settings
        self.spec = ToolSpec(
            name="bash_tool",
            description=BUILD_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令。"},
                    "cwd": {"type": "string", "description": "执行命令的工作目录。工作区外目录需要审批或全自动模式。", "default": "."},
                    "timeout_seconds": {"type": "integer", "description": "本次命令的超时时间，单位秒。"},
                    "description": {"type": "string", "description": "简短说明本次命令的目的。"},
                },
                "required": ["command"],
            },
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
        )

    def get_llm_description(self, *, agent_name: str | None = None, agent_readonly: bool | None = None) -> str:
        if agent_readonly or agent_name in {"plan", "explore"}:
            return READONLY_DESCRIPTION
        return BUILD_DESCRIPTION

    async def preflight(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolPreflightResult:
        request = BashRequest.model_validate(args)
        try:
            resolve_cwd(request.cwd, Path(context.workspace.workspace_path).resolve())
        except BashRuntimeError as exc:
            if exc.error_type == "BashCwdForbidden":
                if self._is_non_interactive_mode(context):
                    return self._preflight_command_policy(request, context)
                return ToolPreflightResult(status="requires_approval", reason=exc.message)
            return ToolPreflightResult(
                status="blocked",
                reason=exc.message,
                result={
                    "status": "error",
                    "tool_name": self.spec.name,
                    "command": request.command,
                    "cwd": request.cwd,
                    "error_type": exc.error_type,
                    "error_message": exc.message,
                    "recoverable": True,
                },
            )
        return self._preflight_command_policy(request, context)

    def _preflight_command_policy(self, request: BashRequest, context: ToolExecutionContext) -> ToolPreflightResult:
        try:
            decision = self._decide(request, context, skip_approval=self._is_non_interactive_mode(context))
        except ValueError as exc:
            return ToolPreflightResult(
                status="blocked",
                reason=str(exc),
                result=self._parse_error_result(request, exc),
            )
        if decision.status == "allow":
            return ToolPreflightResult(status="allow")
        if decision.status == "requires_approval":
            return ToolPreflightResult(status="requires_approval", reason=decision.reason)
        return ToolPreflightResult(
            status="blocked",
            reason=decision.reason,
            result={
                "status": "blocked",
                "tool_name": self.spec.name,
                "command": request.command,
                "cwd": request.cwd,
                "error_type": "BashCommandBlocked",
                "error_message": decision.reason,
                "recoverable": True,
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        if context is None:
            return {
                "status": "error",
                "tool_name": self.spec.name,
                "error_type": "ToolContextMissing",
                "error_message": "bash_tool 缺少运行上下文。",
                "recoverable": True,
            }
        try:
            request = BashRequest.model_validate(args)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "tool_name": self.spec.name,
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
                "recoverable": True,
            }
        try:
            decision = self._decide(request, context, skip_approval=True)
        except ValueError as exc:
            return self._parse_error_result(request, exc)
        if decision.status == "blocked":
            return {
                "status": "blocked",
                "tool_name": self.spec.name,
                "command": request.command,
                "cwd": request.cwd,
                "error_type": "BashCommandBlocked",
                "error_message": decision.reason,
                "recoverable": True,
            }
        if self._is_readonly_agent(context):
            self._scratch_dir(context).mkdir(parents=True, exist_ok=True)
        return await run_bash_command(
            request,
            tool_name=self.spec.name,
            workspace_root=Path(context.workspace.workspace_path).resolve(),
            settings=self._settings,
            default_timeout_seconds=self.spec.timeout_seconds,
            allow_outside_workspace_cwd=context.skip_approval or self._is_non_interactive_mode(context),
        )

    def _decide(self, request: BashRequest, context: ToolExecutionContext, *, skip_approval: bool = False) -> Any:
        if self._is_readonly_agent(context):
            return decide_readonly_policy(
                request,
                self._settings,
                workspace_root=Path(context.workspace.workspace_path).resolve(),
                scratch_dir=self._scratch_dir(context),
            )
        decision = decide_build_policy(request, self._settings)
        if skip_approval and decision.status == "requires_approval":
            return decision.__class__(status="allow", reason="审批已通过。")
        return decision

    def _parse_error_result(self, request: BashRequest, exc: ValueError) -> dict[str, Any]:
        return {
            "status": "error",
            "tool_name": self.spec.name,
            "command": request.command,
            "cwd": request.cwd,
            "error_type": "BashCommandParseError",
            "error_message": str(exc),
            "recoverable": True,
        }

    def _is_readonly_agent(self, context: ToolExecutionContext) -> bool:
        return bool(getattr(context.agent, "readonly", getattr(context.agent, "name", None) in {"plan", "explore"}))

    def _scratch_dir(self, context: ToolExecutionContext) -> Path:
        session_id = str(context.session.session_id)
        workspace_dir = Path(context.workspace.workspace_dir).resolve()
        # scratch 目录位于运行目录中，避免只读 agent 写入仓库业务文件。
        return (workspace_dir / "bash" / session_id).resolve(strict=False)

    def _is_non_interactive_mode(self, context: ToolExecutionContext) -> bool:
        config = getattr(context, "config", None)
        hitl = getattr(config, "human_in_the_loop", None)
        return hitl is not None and not bool(getattr(hitl, "enabled", True))
