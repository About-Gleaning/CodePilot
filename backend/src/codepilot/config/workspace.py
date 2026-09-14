from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


def _slugify(text: str) -> str:
    lowered = text.strip().lower()
    replaced = re.sub(r"[^a-z0-9]+", "-", lowered)
    return replaced.strip("-") or "workspace"


def build_workspace_id(workspace_path: Path) -> str:
    resolved = workspace_path.resolve()
    slug = _slugify(resolved.name)
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


@dataclass(slots=True)
class WorkspaceState:
    workspace_id: str
    workspace_path: Path
    codepilot_home: Path
    workspace_dir: Path
    sessions_dir: Path
    logs_dir: Path
    workspace_meta_file: Path

    def for_user(self, user_id: str, principal: Any | None = None) -> "UserWorkspaceView":
        """创建只包含服务端 UUID 的用户运行目录视图。"""
        try:
            normalized = str(UUID(user_id))
        except ValueError as exc:
            raise ValueError("user_id 必须是服务端 UUID") from exc
        user_home_dir = (self.codepilot_home / "users" / normalized).resolve()
        user_runtime_dir = (self.workspace_dir / "users" / normalized).resolve()
        if not user_home_dir.is_relative_to(self.codepilot_home.resolve()):
            raise ValueError("用户主目录越界")
        if not user_runtime_dir.is_relative_to(self.workspace_dir.resolve()):
            raise ValueError("用户运行目录越界")
        for path in (user_home_dir, user_runtime_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        sessions_dir = user_runtime_dir / "sessions"
        logs_dir = user_runtime_dir / "logs"
        sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return UserWorkspaceView(
            user_id=normalized,
            principal=principal,
            workspace_id=self.workspace_id,
            workspace_path=self.workspace_path,
            codepilot_home=self.codepilot_home,
            user_home_dir=user_home_dir,
            shared_runtime_dir=self.workspace_dir,
            user_runtime_dir=user_runtime_dir,
            workspace_dir=user_runtime_dir,
            sessions_dir=sessions_dir,
            logs_dir=logs_dir,
            workspace_meta_file=self.workspace_meta_file,
        )


@dataclass(frozen=True, slots=True)
class UserWorkspaceView:
    user_id: str
    principal: Any | None
    workspace_id: str
    workspace_path: Path
    codepilot_home: Path
    user_home_dir: Path
    shared_runtime_dir: Path
    user_runtime_dir: Path
    # 兼容现有 Tool：用户私有运行文件默认落入 user_runtime_dir。
    workspace_dir: Path
    sessions_dir: Path
    logs_dir: Path
    workspace_meta_file: Path
