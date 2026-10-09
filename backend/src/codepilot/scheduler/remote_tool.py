from __future__ import annotations

"""worker 的调度工具只转交主进程，保证调度文件仍由单个协调器写入。"""

from pathlib import Path

import httpx

from codepilot.tools.schedule_manage_tool import ScheduleManageTool
from codepilot.tools.file_tool_common import build_tool_failure, FileToolError


class WorkerScheduleTool(ScheduleManageTool):
    def __init__(self, args, settings):
        super().__init__(store=None, runner=None, settings=settings, agent_profiles={}, timeout_seconds=15)
        self._args = args

    async def execute(self, args, context=None):
        try:
            self._ensure_allowed(context)
            payload = {"user_id": self._args.user_id, "agent_id": self._args.agent_id,
                       "revision_id": self._args.revision_id, "session_id": self._args.session_id,
                       "sender_agent_id": context.agent.agent_id, "sender_revision_id": context.agent.revision_id, "arguments": args}
            token = Path(self._args.report_token_file).read_text().strip()
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.post(self._args.report_url.removesuffix("/report") + "/tool",
                    json=payload, headers={"x-codepilot-schedule-token": token})
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, OSError):
            # 请求可能已经执行，返回不确定结果，不在网络层自动重发。
            return build_tool_failure(self.spec.name, FileToolError("调度请求未确认结果，请先查询现状，不要直接重复操作。", error_type="ScheduleResultUncertain"))
        except FileToolError as exc:
            return build_tool_failure(self.spec.name, exc)
