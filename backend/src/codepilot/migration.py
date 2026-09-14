from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class MigrationError(RuntimeError):
    pass


class MultiUserMigration:
    """把旧单用户运行态离线复制到指定管理员的私有分区。"""

    def __init__(self, *, codepilot_home: Path, workspace_dir: Path) -> None:
        self.home = codepilot_home.expanduser().resolve()
        self.workspace = workspace_dir.resolve()
        self.sentinel = self.home / "migration-v2.json"
        self.journal = self.home / "migration-v2.journal.json"

    def required(self) -> bool:
        return not self.sentinel.exists() and bool(self._legacy_sources())

    def preview(self, admin_user_id: str) -> dict[str, Any]:
        sources = self._legacy_sources()
        return {
            "schema_version": 2,
            "migration_required": bool(sources) and not self.sentinel.exists(),
            "owner_user_id": admin_user_id,
            "sources": [self._relative(path) for path in sources],
            "file_count": sum(1 for source in sources for _ in self._files(source)),
        }

    def apply(
        self,
        admin_user_id: str,
        *,
        profile_by_name: Callable[[str], Any | None] | None = None,
    ) -> dict[str, Any]:
        if self.sentinel.exists():
            return json.loads(self.sentinel.read_text(encoding="utf-8"))
        sources = self._legacy_sources()
        if not sources:
            result = {"schema_version": 2, "status": "complete", "owner_user_id": admin_user_id, "migrated_files": 0}
            self._atomic_json(self.sentinel, result)
            return result

        backup = self.home / "backups" / f"multi-user-v2-{int(time.time())}"
        backup.mkdir(parents=True, mode=0o700)
        self._atomic_json(self.journal, {"schema_version": 1, "status": "copying", "owner_user_id": admin_user_id, "backup": self._relative(backup)})
        for source in sources:
            target = backup / self._relative(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

        migrated = 0
        migrated += self._copy_tree(self.home / "agents", self.home / "users" / admin_user_id / "agents", exclude={"shared"})
        legacy_memory = self.home / "instructions" / "memory.instruction.md"
        if legacy_memory.is_file():
            self._copy_file(legacy_memory, self.home / "users" / admin_user_id / "memory" / "_global.md")
            migrated += 1

        user_runtime = self.workspace / "users" / admin_user_id
        for directory in ("attachments", "plans", "todos", "bash", "schedule_prompts"):
            migrated += self._copy_tree(self.workspace / directory, user_runtime / directory)
        migrated += self._migrate_sessions(self.workspace / "sessions", user_runtime / "sessions", admin_user_id)

        for source_name, target_name in (
            ("agent-runtimes.json", "agent-runtimes.json"),
            ("agent-runs.jsonl", "agent-runs.jsonl"),
            ("agent-runtime-events.jsonl", "agent-runtime-events.jsonl"),
            ("schedule_runs.jsonl", "schedule-runs.jsonl"),
            ("schedule-runs.jsonl", "schedule-runs.jsonl"),
        ):
            source = self.workspace / source_name
            if source.is_file():
                self._migrate_json_file(source, user_runtime / target_name, admin_user_id)
                migrated += 1
        schedules = self.workspace / "schedules.json"
        if schedules.is_file():
            payload = self._read_json(schedules)
            if not isinstance(payload, list):
                raise MigrationError("schedules.json 必须是数组")
            converted = []
            for item in payload:
                if not isinstance(item, dict):
                    raise MigrationError("schedules.json 包含无效任务")
                profile = profile_by_name(str(item.get("agent_name") or "")) if profile_by_name else None
                mapped = profile is not None and bool(getattr(profile, "agent_id", "")) and bool(getattr(profile, "revision_id", ""))
                metadata = dict(item.get("metadata") or {})
                if not mapped:
                    metadata["migration_status"] = "disabled_agent_unresolved"
                converted.append({
                    **item,
                    "schema_version": 2,
                    "user_id": admin_user_id,
                    "agent_id": getattr(profile, "agent_id", ""),
                    "revision_id": getattr(profile, "revision_id", ""),
                    "enabled": bool(item.get("enabled", True)) if mapped else False,
                    "next_run_at": item.get("next_run_at") if mapped else None,
                    "metadata": metadata,
                })
            self._atomic_json(user_runtime / "schedules.json", converted)
            migrated += 1

        result = {
            "schema_version": 2,
            "status": "complete",
            "owner_user_id": admin_user_id,
            "migrated_files": migrated,
            "backup": self._relative(backup),
        }
        self._atomic_json(self.sentinel, result)
        self._atomic_json(self.journal, {**result, "status": "complete"})
        return result

    def _legacy_sources(self) -> list[Path]:
        candidates = [
            self.home / "instructions" / "memory.instruction.md",
            self.workspace / "sessions",
            self.workspace / "attachments",
            self.workspace / "plans",
            self.workspace / "todos",
            self.workspace / "bash",
            self.workspace / "schedule_prompts",
            self.workspace / "schedules.json",
            self.workspace / "schedule_runs.jsonl",
            self.workspace / "schedule-runs.jsonl",
            self.workspace / "agent-runtimes.json",
            self.workspace / "agent-runs.jsonl",
            self.workspace / "agent-runtime-events.jsonl",
        ]
        result = [path for path in candidates if path.is_file() or path.is_dir() and any(path.iterdir())]
        legacy_agents = self.home / "agents"
        if legacy_agents.is_dir() and any(
            path.is_file() and "shared" not in path.relative_to(legacy_agents).parts
            for path in legacy_agents.rglob("*")
        ):
            result.insert(0, legacy_agents)
        return result

    def _migrate_sessions(self, source: Path, target: Path, user_id: str) -> int:
        if not source.is_dir():
            return 0
        count = 0
        for path in source.glob("*.jsonl"):
            self._migrate_json_file(path, target / path.name, user_id)
            count += 1
        return count

    def _migrate_json_file(self, source: Path, target: Path, user_id: str) -> None:
        if source.suffix == ".jsonl":
            records = self._read_jsonl(source)
            converted = [self._add_owner(record, user_id) for record in records]
            self._atomic_text(target, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in converted))
            return
        payload = self._read_json(source)
        self._atomic_json(target, self._add_owner(payload, user_id))

    def _add_owner(self, value: Any, user_id: str) -> Any:
        if isinstance(value, list):
            return [self._add_owner(item, user_id) for item in value]
        if not isinstance(value, dict):
            return value
        result = dict(value)
        result.setdefault("schema_version", 2)
        result.setdefault("user_id", user_id)
        if result.get("record_type") == "session_meta" and isinstance(result.get("data"), dict):
            result["data"] = {**result["data"], "user_id": user_id}
        for key in ("run", "event"):
            if isinstance(result.get(key), dict):
                nested = {**result[key], "user_id": user_id}
                if isinstance(nested.get("ref"), dict):
                    nested["ref"] = {**nested["ref"], "user_id": user_id}
                result[key] = nested
        return result

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        raw = path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        result = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1 and not raw.endswith("\n"):
                    break
                raise MigrationError(f"JSONL 中间记录损坏：{self._relative(path)}:{index + 1}") from exc
            if not isinstance(item, dict):
                raise MigrationError(f"JSONL 记录不是对象：{self._relative(path)}:{index + 1}")
            result.append(item)
        return result

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MigrationError(f"JSON 文件损坏：{self._relative(path)}") from exc

    def _copy_tree(self, source: Path, target: Path, *, exclude: set[str] | None = None) -> int:
        if not source.is_dir():
            return 0
        count = 0
        for path in self._files(source):
            relative = path.relative_to(source)
            if relative.parts and relative.parts[0] in (exclude or set()):
                continue
            self._copy_file(path, target / relative)
            count += 1
        return count

    def _copy_file(self, source: Path, target: Path) -> None:
        self._atomic_bytes(target, source.read_bytes())

    def _files(self, source: Path):
        if source.is_file():
            yield source
        elif source.is_dir():
            yield from (path for path in source.rglob("*") if path.is_file() and not path.is_symlink())

    def _relative(self, path: Path) -> str:
        if path.is_relative_to(self.home):
            return str(Path("codepilot_home") / path.relative_to(self.home))
        if path.is_relative_to(self.workspace):
            return str(Path("workspace") / path.relative_to(self.workspace))
        return path.name

    def _atomic_json(self, path: Path, payload: Any) -> None:
        self._atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _atomic_text(self, path: Path, content: str) -> None:
        self._atomic_bytes(path, content.encode("utf-8"))

    def _atomic_bytes(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
