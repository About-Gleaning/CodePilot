from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import yaml

from codepilot.session.agent_config import AgentConfigError
from codepilot.skills.runtime import Skill, SkillRegistry, parse_skill_markdown


class SkillStore:
    """原子发布当前文件集；读取与清理共用锁，不保留内容历史。"""

    def __init__(self, home: Path, *, publication_id: str | None = None):
        self.home = home.resolve()
        self.publication_id = str(uuid.UUID(publication_id)) if publication_id else None

    def _root(self, user_id: str) -> Path:
        if self.publication_id:
            root = self.home / "publications" / self.publication_id / "skills"
            cursor = root
            while cursor != self.home:
                if cursor.is_symlink():
                    raise AgentConfigError("公共资源路径不允许符号链接")
                cursor = cursor.parent
            return root
        try:
            owner = str(uuid.UUID(user_id))
        except ValueError as exc:
            raise AgentConfigError("用户身份无效", status=403) from exc
        root = self.home / "users" / owner / "skills"
        cursor = root
        while cursor != self.home:
            if cursor.is_symlink():
                raise AgentConfigError("Skill 存储路径不允许符号链接")
            cursor = cursor.parent
        if not root.resolve().is_relative_to(self.home):
            raise AgentConfigError("Skill 存储路径无效")
        return root

    def _directory(self, user_id: str, skill_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{36}", skill_id):
            raise AgentConfigError("Skill 不存在", status=404)
        path = self._root(user_id) / skill_id
        if path.is_symlink() or not path.resolve().is_relative_to(self._root(user_id).resolve()):
            raise AgentConfigError("Skill 路径无效")
        return path

    @staticmethod
    def resource_path(root: Path, name: str) -> Path:
        relative = PurePosixPath(name)
        if not name or "\\" in name or "\x00" in name or relative.is_absolute() or any(part in {".", ".."} for part in name.split("/")):
            raise AgentConfigError("Skill 文件路径无效")
        path = root.joinpath(*relative.parts)
        cursor = path
        while cursor != root:
            if cursor.is_symlink():
                raise AgentConfigError("Skill 文件不允许符号链接")
            cursor = cursor.parent
        if not path.resolve().is_relative_to(root.resolve()):
            raise AgentConfigError("Skill 文件路径越界")
        return path

    @contextmanager
    def _lock(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _get(self, user_id: str, skill_id: str) -> dict:
        root = self._directory(user_id, skill_id)
        try:
            pointer = self.resource_path(root, "current.json")
            if pointer.stat().st_size > 16384:
                raise ValueError
            record = json.loads(pointer.read_text())
            if record["skill_id"] != skill_id:
                raise ValueError
            return record
        except (OSError, ValueError, KeyError) as exc:
            raise AgentConfigError("Skill 不存在或记录损坏", status=404) from exc

    def get(self, user_id: str, skill_id: str) -> dict:
        with self._lock(self._directory(user_id, skill_id)):
            record = self._current(user_id, skill_id)
            return {key: value for key, value in record.items() if key != "content_dir"}

    def _current(self, user_id: str, skill_id: str) -> dict:
        record = self._get(user_id, skill_id)
        if "content_dir" in record:
            return record
        # 旧格式只迁移当前有效内容，校验完成并发布后才删除旧历史。
        root = self._directory(user_id, skill_id)
        revision = record["revision"]
        if not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise AgentConfigError("旧 Skill 内容标识无效")
        old = self.resource_path(root, f"versions/{revision}")
        manifest = json.loads(self.read_shared_path(old, ".manifest.json"))
        if hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != revision:
            raise AgentConfigError("旧 Skill 内容损坏")
        if not 1 <= len(manifest) <= 100 or "SKILL.md" not in manifest:
            raise AgentConfigError("旧 Skill 文件清单无效")
        files = {}
        for name, digest in manifest.items():
            data = self.read_shared_path(old, name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise AgentConfigError("旧 Skill 文件损坏")
            files[name] = data
        if sum(map(len, files.values())) > 4 * 1024 * 1024:
            raise AgentConfigError("Skill 内容最多 4 MiB")
        return self._commit(root, record, files)

    def _commit(self, root: Path, record: dict, files: dict[str, bytes]) -> dict:
        directory = "content-" + uuid.uuid4().hex
        target = root / directory
        published = False
        try:
            for name, data in files.items():
                self._write(self.resource_path(target, name), data)
            record = {**record, "format_version": 2, "content_dir": directory}
            self._publish(root, record)
            published = True
        finally:
            if not published and target.exists():
                shutil.rmtree(target)
        # 读取与清理共用锁；这里只保留当前内容，不维护历史版本。
        for path in root.iterdir():
            if path.name != directory and (path.name.startswith("content-") or path.name == "versions"):
                if path.is_symlink():
                    raise AgentConfigError("Skill 内容目录不允许符号链接")
                if path.is_dir():
                    shutil.rmtree(path)
        return record

    def list(self, user_id: str, offset: int = 0, limit: int = 50) -> list[dict]:
        root = self._root(user_id)
        if not root.exists():
            return []
        ids = sorted(item.name for item in root.iterdir() if item.is_dir() and not item.is_symlink())
        return [self.get(user_id, identity) for identity in ids[offset:offset + limit]]

    def save(self, user_id: str, skill_id: str | None, payload: dict, *, creating: bool = False) -> dict:
        creating = creating or skill_id is None
        skill_id = skill_id or str(uuid.uuid4())
        root = self._directory(user_id, skill_id)
        files = payload.get("files", {})
        if not isinstance(files, dict) or not 1 <= len(files) <= 100 or "SKILL.md" not in files:
            raise AgentConfigError("Skill 必须包含 SKILL.md，且最多 100 个文件")
        decoded = {}
        total_bytes = 0
        for name, encoded in files.items():
            self.resource_path(root, name)
            if name == ".manifest.json":
                raise AgentConfigError("Skill 文件名为系统保留名称")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise AgentConfigError("Skill 文件编码无效") from exc
            if len(data) > 1024 * 1024:
                raise AgentConfigError("单个 Skill 文件最多 1 MiB")
            total_bytes += len(data)
            if total_bytes > 4 * 1024 * 1024:
                raise AgentConfigError("Skill 内容最多 4 MiB")
            decoded[name] = data
        name, description = self.validate_document(decoded["SKILL.md"])
        with self._lock(root):
            current = self._current(user_id, skill_id) if (root / "current.json").exists() else None
            if not creating and current is None:
                raise AgentConfigError("Skill 不存在", status=404)
            if current and current["archived"]:
                raise AgentConfigError("Skill 已归档", code="skill_archived", status=409)
            if current and payload.get("expected_revision") != current["revision"]:
                raise AgentConfigError("Skill 已更新，请重新加载", code="revision_conflict", status=409)
            record = {"skill_id": skill_id, "revision": uuid.uuid4().hex + uuid.uuid4().hex,
                      "name": name, "description": description, "archived": False,
                      "visibility": "private", "files": sorted(decoded)}
            self._commit(root, record, decoded)
            return record

    @staticmethod
    def validate_document(content: bytes) -> tuple[str, str]:
        """发布复用正文校验，不能通过保存资源来试探内容是否合法。"""
        try:
            raw = content.decode("utf-8")
            if not raw.startswith("---\n"):
                raise ValueError
            header, body = raw[4:].split("\n---\n", 1)
            metadata = yaml.safe_load(header)
            if not isinstance(metadata, dict) or not body.strip():
                raise ValueError
            name, description = metadata.get("name"), metadata.get("description")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not isinstance(description, str) or not 1 <= len(description) <= 500:
                raise ValueError
        except (ValueError, UnicodeError, yaml.YAMLError) as exc:
            raise AgentConfigError("SKILL.md 必须包含有效名称、描述和正文") from exc
        return name, description

    def _publish(self, root: Path, record: dict) -> None:
        temporary = root / (".current-" + uuid.uuid4().hex)
        try:
            self._write(temporary, json.dumps(record, ensure_ascii=False).encode())
            os.replace(temporary, root / "current.json")
        finally:
            temporary.unlink(missing_ok=True)

    def archive(self, user_id: str, skill_id: str, revision: str) -> dict:
        root = self._directory(user_id, skill_id)
        with self._lock(root):
            record = self._current(user_id, skill_id)
            if record["revision"] != revision:
                raise AgentConfigError("Skill 已更新，请重新加载", code="revision_conflict", status=409)
            record["archived"] = True
            self._publish(root, record)
            return record

    def read(self, user_id: str, skill_id: str, filename: str, *,
             expected_revision: str | None = None, active: bool = False) -> bytes:
        return self.read_resource(user_id, skill_id, filename, expected_revision=expected_revision, active=active)[0]

    def read_resource(self, user_id: str, skill_id: str, filename: str, *,
                      expected_revision: str | None = None, active: bool = False) -> tuple[bytes, Path]:
        root = self._directory(user_id, skill_id)
        with self._lock(root):
            record = self._current(user_id, skill_id)
            if active and record["archived"]:
                raise AgentConfigError("Skill 已归档", code="skill_dependency_missing", status=409)
            if expected_revision and expected_revision != record["revision"]:
                raise AgentConfigError("Skill 已更新，请重新加载", code="revision_conflict", status=409)
            if filename not in record["files"]:
                raise AgentConfigError("Skill 文件不存在", status=404)
            directory = self.resource_path(root, record["content_dir"])
            return self.read_shared_path(directory, filename), directory

    @staticmethod
    def read_shared_path(root: Path, filename: str) -> bytes:
        path = SkillStore.resource_path(root, filename)
        if root.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise AgentConfigError("Skill 文件不存在或过大", status=404)
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise AgentConfigError("Skill 文件过大")
        return data

    def shared_files(self, skill: Skill) -> list[str]:
        # 只有管理页枚举文件，执行只检查请求的资源路径。
        result = []
        entries = 0
        for parent, dirs, names in os.walk(skill.path, followlinks=False):
            dirs[:] = [name for name in dirs if name not in {".git", ".build", ".venv", "node_modules", "__pycache__"}
                       and not (Path(parent) / name).is_symlink()]
            entries += len(dirs) + len(names)
            if entries > 1000:
                raise AgentConfigError(f"Skill {skill.name} 的可管理目录条目超过 1000 个")
            for name in names:
                path = Path(parent) / name
                if name == ".DS_Store" or path.is_symlink():
                    continue
                result.append(path.relative_to(skill.path).as_posix())
                if len(result) > 100:
                    raise AgentConfigError(f"Skill {skill.name} 的可管理文件超过 100 个")
        return sorted(result)

    def resolve(self, user_id: str, ids: list[str] | None, legacy: SkillRegistry) -> SkillRegistry:
        registry = SkillRegistry(legacy.skills_root)
        shared = {f"shared:{skill.name}": skill for skill in legacy.skills}
        for skill_id in list(shared) if ids is None else ids:
            if skill_id.startswith("public:"):
                from codepilot.session.publications import split_identity
                publication, resource = split_identity(skill_id)
                store = SkillStore(self.home, publication_id=publication)
                record = store.get(user_id, resource)
                root = store._directory(user_id, resource)
                registry.skills.append(Skill(record["name"], record["description"], root, root / "SKILL.md", metadata={"skill_id": skill_id}))
                continue
            if skill_id.startswith("shared:"):
                skill = shared.get(skill_id)
                if skill is None:
                    raise AgentConfigError("共享 Skill 不可用", code="skill_dependency_missing", status=409)
                if skill.path.is_symlink() or not skill.path.resolve().is_relative_to((self.home / "skills").resolve()):
                    raise AgentConfigError("共享 Skill 路径无效")
                content = self.read_shared_path(skill.path, "SKILL.md").decode("utf-8")
                metadata, _ = parse_skill_markdown(content)
                registry.skills.append(Skill(skill.name, str(metadata.get("description") or skill.description),
                                             skill.path, skill.skill_md_path, metadata={"skill_id": skill_id}))
                continue
            record = self.get(user_id, skill_id)
            if record["archived"]:
                raise AgentConfigError("Skill 已归档", code="skill_dependency_missing", status=409)
            root = self._directory(user_id, skill_id)
            registry.skills.append(Skill(record["name"], record["description"], root, root / "SKILL.md",
                                         metadata={"skill_id": skill_id, "owner": user_id}))
        if len({skill.name for skill in registry.skills}) != len(registry.skills):
            raise AgentConfigError("所选 Skill 名称重复", code="skill_name_conflict", status=409)
        return registry
