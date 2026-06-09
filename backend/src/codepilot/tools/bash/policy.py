from __future__ import annotations

import shlex
from pathlib import Path

from codepilot.config.settings import BashToolSettings
from codepilot.tools.bash.models import ApprovalDecision, BashRequest


READONLY_SEPARATORS = {"|"}
BLOCKED_READONLY_SEPARATORS = {";", "&&", "||", "&", "&&&"}
REDIRECT_TOKENS = {">", ">>", "2>", "2>>", "1>", "1>>", "&>", "&>>"}
BUILD_SEPARATORS = {"|", ";", "&&", "||", "&"}


def tokenize_command(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def token_prefix_matches(tokens: list[str], rules: list[list[str]]) -> bool:
    for rule in rules:
        if rule and len(tokens) >= len(rule) and tokens[: len(rule)] == rule:
            return True
    return False


def decide_build_policy(request: BashRequest, settings: BashToolSettings) -> ApprovalDecision:
    tokens = tokenize_command(request.command)
    segments = split_command_segments(tokens, BUILD_SEPARATORS)
    if any(token_prefix_matches(segment, settings.blacklist) for segment in segments):
        return ApprovalDecision(status="blocked", reason="命令命中 blacklist，已拒绝执行。")
    if settings.approval_mode == "all":
        return ApprovalDecision(status="requires_approval", reason="当前 Bash 审批模式为 all，执行前需要人工确认。")
    if settings.approval_mode == "allowlist":
        if segments and all(token_prefix_matches(segment, settings.allowlist) for segment in segments):
            return ApprovalDecision(status="allow", reason="命令命中 allowlist。")
        return ApprovalDecision(status="requires_approval", reason="命令未命中 allowlist，执行前需要人工确认。")
    return ApprovalDecision(status="allow", reason="当前 Bash 审批模式为 none。")


def split_command_segments(tokens: list[str], separators: set[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in separators:
            segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment]


def decide_readonly_policy(
    request: BashRequest,
    settings: BashToolSettings,
    *,
    workspace_root: Path,
    scratch_dir: Path,
) -> ApprovalDecision:
    tokens = tokenize_command(request.command)
    if token_prefix_matches(tokens, settings.blacklist):
        return ApprovalDecision(status="blocked", reason="命令命中 blacklist，已拒绝执行。")
    if not tokens:
        return ApprovalDecision(status="blocked", reason="命令不能为空。")
    if any(token in BLOCKED_READONLY_SEPARATORS for token in tokens):
        return ApprovalDecision(status="blocked", reason="只读 agent 不允许使用会改变控制流或后台执行的 shell 组合符。")

    command_parts: list[list[str]] = [[]]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in READONLY_SEPARATORS:
            command_parts.append([])
            index += 1
            continue
        if token in REDIRECT_TOKENS:
            if index + 1 >= len(tokens):
                return ApprovalDecision(status="blocked", reason="重定向缺少目标路径。")
            target = _resolve_redirect_target(tokens[index + 1], workspace_root)
            if not target.is_relative_to(scratch_dir):
                return ApprovalDecision(status="blocked", reason="只读 agent 的重定向只能写入受控 scratch 目录。")
            index += 2
            continue
        command_parts[-1].append(token)
        index += 1

    for part in command_parts:
        if not part:
            return ApprovalDecision(status="blocked", reason="管道中存在空命令。")
        if not token_prefix_matches(part, settings.readonly_allowlist):
            return ApprovalDecision(status="blocked", reason=f"只读 agent 不允许执行该命令：{part[0]}")
    return ApprovalDecision(status="allow", reason="命令通过只读 allowlist 校验。")


def _resolve_redirect_target(raw_target: str, workspace_root: Path) -> Path:
    raw_path = Path(raw_target).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve(strict=False)
    return (workspace_root / raw_path).resolve(strict=False)
