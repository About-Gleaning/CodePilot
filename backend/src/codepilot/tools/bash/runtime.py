from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from codepilot.config.settings import BashToolSettings
from codepilot.tools.bash.models import BashRequest, BashResult


class BashRuntimeError(Exception):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type


def resolve_cwd(cwd: str, workspace_root: Path, *, allow_outside_workspace: bool = False) -> Path:
    raw_path = Path(cwd or ".").expanduser()
    if raw_path.is_absolute():
        target = raw_path.resolve(strict=False)
    else:
        target = (workspace_root / raw_path).resolve(strict=False)
    if not allow_outside_workspace and not target.is_relative_to(workspace_root):
        raise BashRuntimeError(f"cwd 超出工作区范围：{cwd}", error_type="BashCwdForbidden")
    if not target.exists():
        raise BashRuntimeError(f"cwd 不存在：{target}", error_type="BashCwdNotFound")
    if not target.is_dir():
        raise BashRuntimeError(f"cwd 不是目录：{target}", error_type="BashCwdNotDirectory")
    return target


async def run_bash_command(
    request: BashRequest,
    *,
    tool_name: str,
    workspace_root: Path,
    settings: BashToolSettings,
    default_timeout_seconds: int,
    allow_outside_workspace_cwd: bool = False,
) -> dict[str, object]:
    started = time.monotonic()
    try:
        cwd = resolve_cwd(request.cwd, workspace_root, allow_outside_workspace=allow_outside_workspace_cwd)
    except BashRuntimeError as exc:
        return BashResult(
            status="error",
            tool_name=tool_name,
            command=request.command,
            cwd=request.cwd,
            error_type=exc.error_type,
            error_message=exc.message,
        ).to_tool_result()

    timeout = request.timeout_seconds or default_timeout_seconds
    process = await asyncio.create_subprocess_exec(
        "/bin/zsh",
        "-lc",
        request.command,
        cwd=str(cwd),
        env=dict(os.environ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await _terminate_process_group(process)
        stdout_bytes, stderr_bytes = await process.communicate()
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        await process.communicate()
        raise

    stdout, stdout_truncated = _decode_and_truncate(stdout_bytes, settings.max_output_chars)
    stderr, stderr_truncated = _decode_and_truncate(stderr_bytes, settings.max_output_chars)
    duration_ms = int((time.monotonic() - started) * 1000)
    return BashResult(
        status="error" if timed_out else "ok",
        tool_name=tool_name,
        command=request.command,
        cwd=str(cwd),
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
        error_type="BashCommandTimedOut" if timed_out else None,
        error_message=f"命令执行超时：{timeout} 秒" if timed_out else None,
    ).to_tool_result()


def _decode_and_truncate(data: bytes, max_chars: int) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()
