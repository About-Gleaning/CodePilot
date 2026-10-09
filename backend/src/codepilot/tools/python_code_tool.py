"""把用户 Python 代码适配到既有 BaseTool 调用链。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from codepilot.session.agent_config import AgentConfigError
from codepilot.tools.base import BaseTool, ToolSpec
from codepilot.tools.code_store import ToolStore, resolved_call_name, encoded

LIMIT = 64 * 1024

# 引导器不接收平台对象；业务输出嵌套保存，不能伪造平台控制结果。
_RUNNER = r'''
import contextlib, importlib.util, json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
args = json.load(sys.stdin)
with contextlib.redirect_stdout(sys.stderr):
    spec = importlib.util.spec_from_file_location("codepilot_user_tool", root / sys.argv[2])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.execute(args)
if not isinstance(result, dict):
    raise TypeError("工具必须返回字典")
json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False)
'''


class PythonCodeTool(BaseTool):
    def __init__(self, store: ToolStore, user: str, identity: str, version: str, credentials=None):
        self.store, self.user, self.code_tool_id, self.version = store, user, identity, version
        self.credentials = credentials
        bundle = store.resolve(user, identity, version)
        self.definition = bundle['definition']
        self.bundle = bundle
        self.spec = ToolSpec(name=resolved_call_name(identity, self.definition), description=self.definition['description'],
                             input_schema=self.definition['input_schema'], timeout_seconds=self.definition['timeout_seconds'],
                             side_effect='workspace_mutation', requires_approval=True, can_parallel=False)

    async def execute(self, args, context=None):
        process = None
        try:
            # 不放在 preflight：人工审批恢复会跳过 preflight，但不能跳过权限和撤权校验。
            if context is None or self.code_tool_id not in getattr(context.agent, 'tool_ids', []) or context.agent.readonly:
                raise AgentConfigError('当前 Agent 无权执行代码工具')
            if getattr(context.workspace, 'user_id', None) != self.user:
                raise AgentConfigError('工具执行身份不匹配')
            await asyncio.to_thread(self.store.resolve, self.user, self.code_tool_id, self.version)
            if len(encoded(args)) > LIMIT:
                raise AgentConfigError('工具输入超过限制')
            if next(Draft202012Validator(self.spec.input_schema).iter_errors(args), None):
                raise AgentConfigError('工具输入参数不符合 Schema')
            env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT'}}
            secrets = {}
            fields = self.definition['credential_fields']
            if fields:
                if self.credentials is None:
                    raise AgentConfigError('工具凭证待配置')
                record, secrets = await asyncio.to_thread(self.credentials)
                if record['target'] != 'tool:' + self.code_tool_id or any(field not in secrets for field in fields):
                    raise AgentConfigError('工具凭证无效')
                env.update({'CODEPILOT_TOOL_CREDENTIAL_' + name: secrets[name] for name in fields})
            # 独立临时副本避免 Python 写入历史版本目录；不传递 Runtime 或会话正文。
            with tempfile.TemporaryDirectory(prefix='codepilot-tool-') as directory:
                root = Path(directory)
                for name, content in self.bundle['files'].items():
                    path = self.store.files.resource_path(root, name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, '-I', '-B', '-c', _RUNNER, str(root), self.definition['entrypoint'],
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=str(context.workspace.workspace_path), env=env, start_new_session=True)

                async def read(stream):
                    data = bytearray()
                    while chunk := await stream.read(8192):
                        data.extend(chunk)
                        if len(data) > LIMIT:
                            raise AgentConfigError('工具输出超过限制')
                    return bytes(data)

                async def exchange():
                    process.stdin.write(encoded(args))
                    await process.stdin.drain()
                    process.stdin.close()
                    output, _ = await asyncio.gather(read(process.stdout), read(process.stderr))
                    if await process.wait() != 0:
                        raise AgentConfigError('工具代码执行失败')
                    result = json.loads(output)
                    if not isinstance(result, dict):
                        raise AgentConfigError('工具必须返回 JSON 对象')
                    return result

                try:
                    result = await asyncio.wait_for(exchange(), self.spec.timeout_seconds)
                    def redact(value):
                        if isinstance(value, str):
                            for secret in secrets.values():
                                value = value.replace(secret, "[已脱敏]")
                            return value
                        if isinstance(value, dict):
                            return {redact(key): redact(item) for key, item in value.items()}
                        if isinstance(value, list):
                            return [redact(item) for item in value]
                        return value
                    result = redact(result)
                    if len(encoded(result)) > LIMIT:
                        raise AgentConfigError("脱敏后的工具结果超过限制")
                    return {'status': 'ok', 'tool_name': self.spec.name, 'output': result}
                finally:
                    # 先回收进程再清理临时文件；包括已退出父进程留下的后台子进程。
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 不输出原始异常、stderr 或凭证；已启动的副作用不自动重放。
            return {'status': 'error', 'tool_name': self.spec.name,
                    'error_type': 'CodeToolOutcomeUncertain' if process else 'CodeToolRejected',
                    'error_message': '代码工具执行失败；已发出的操作需核对结果' if process else '代码工具授权、参数或凭证校验失败',
                    'recoverable': False}
