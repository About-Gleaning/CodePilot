from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


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
