from __future__ import annotations

import fcntl
import json
import os
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from codepilot.session.agent_config import AgentConfigError


class ConnectionStore:
    """只持久化加密凭证；密钥由独立环境变量提供，公开视图始终移除密文。"""

    def __init__(self, home: Path, settings):
        self.home, self.settings = home.resolve(), settings

    def catalog(self, user_id: str | None = None) -> dict[str, dict]:
        result = {}
        for name, server in self.settings.mcp.servers.items():
            if server.enabled and server.assignable_to_private_agents:
                result[f"mcp:{name}"] = {"target": f"mcp:{name}", "fields": server.credential_fields, "kind": "mcp"}
        for hook in self.settings.hooks.plugins:
            if hook.enabled and hook.plugin_type == "http":
                result[f"hook:{hook.hook_id}"] = {"target": f"hook:{hook.hook_id}", "fields": hook.credential_fields, "kind": "hook"}
        if user_id:
            from codepilot.hooks.store import HookStore
            store = HookStore(self.home)
            for hook in store.list(user_id):
                if not hook["archived"] and hook["plugin_type"] == "http":
                    entry = store.connection_target(user_id, hook["hook_id"])
                    result[entry["target"]] = entry
        return result

    def _directory(self, user_id: str) -> Path:
        try:
            owner = str(uuid.UUID(user_id))
        except ValueError as exc:
            raise AgentConfigError("用户身份无效", status=403) from exc
        directory = self.home / "users" / owner / "connections"
        cursor = directory
        while cursor != self.home:
            if cursor.is_symlink():
                raise AgentConfigError("连接存储路径不允许符号链接")
            cursor = cursor.parent
        if not directory.resolve().is_relative_to(self.home):
            raise AgentConfigError("连接存储路径无效")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def entry(self, user_id: str, target: str) -> dict | None:
        if target.startswith("tool:"):
            from codepilot.tools.code_store import ToolStore, digest
            identity = target[5:]
            bundle = ToolStore(self.home).load(user_id, identity)
            fields = bundle["definition"]["credential_fields"]
            return {"target": target, "kind": "tool", "fields": fields, "personal": True,
                    "mapping": {name: name for name in fields}, "signature": digest([identity, fields])}
        if target.startswith("hook:public:"):
            from codepilot.session.publications import PublicationStore, split_identity
            from codepilot.hooks.store import HookStore
            publication, resource = split_identity(target[5:])
            PublicationStore(self.home).get(publication)
            store = HookStore(self.home, publication_id=publication)
            hook = store.load(user_id, "personal:" + resource)
            value = hook["definition"]
            if value["plugin_type"] != "http":
                return None
            import hashlib
            return {"target": target, "kind": "hook", "fields": sorted(set(value["credential_headers"].values())),
                    "signature": hashlib.sha256(json.dumps([value["url"], value["credential_headers"]], sort_keys=True).encode()).hexdigest(),
                    "mapping": value["credential_headers"], "url": value["url"], "personal": True}
        if target.startswith("hook:personal:"):
            from codepilot.hooks.store import HookStore
            return HookStore(self.home).connection_target(user_id, target[5:])
        return self.catalog().get(target)

    def _path(self, user_id: str, connection_id: str) -> Path:
        try:
            identity = str(uuid.UUID(connection_id))
        except ValueError as exc:
            raise AgentConfigError("连接不存在", status=404) from exc
        path = self._directory(user_id) / f"{identity}.json"
        if path.is_symlink():
            raise AgentConfigError("连接文件不可用")
        return path

    @staticmethod
    def _cipher() -> Fernet:
        key = os.environ.get("CODEPILOT_CONNECTION_KEY")
        if not key:
            raise AgentConfigError("个人连接加密密钥待管理员配置", code="connection_key_missing", status=409)
        try:
            return Fernet(key.encode())
        except ValueError as exc:
            raise AgentConfigError("个人连接加密密钥无效", code="connection_key_invalid", status=409) from exc

    def _read(self, user_id: str, connection_id: str) -> dict:
        path = self._path(user_id, connection_id)
        try:
            if path.stat().st_size > 65536:
                raise ValueError
            value = json.loads(path.read_text())
            if value["connection_id"] != connection_id or value["owner"] != user_id:
                raise ValueError
            return value
        except (OSError, ValueError, KeyError) as exc:
            raise AgentConfigError("连接不存在或记录无效", code="connection_not_found", status=404) from exc

    @staticmethod
    def public(record: dict) -> dict:
        return {key: record[key] for key in ("connection_id", "target", "revision", "revoked")}

    def list(self, user_id: str, offset: int = 0, limit: int = 50) -> list[dict]:
        paths = sorted(self._directory(user_id).glob("*.json"))
        return [self.public(self._read(user_id, path.stem)) for path in paths[offset:offset + limit]]

    def save(self, user_id: str, connection_id: str | None, target: str, credentials: dict[str, str], expected: str | None) -> dict:
        if target.startswith("hook:public:"):
            from codepilot.session.publications import PublicationStore, split_identity
            publication, _ = split_identity(target[5:])
            PublicationStore(self.home).authorize(publication, user_id)
        entry = self.entry(user_id, target)
        if entry is None:
            raise AgentConfigError("管理员未开放此连接目标", status=403)
        if set(credentials) != set(entry["fields"]) or any(not value or len(value) > 8192 for value in credentials.values()):
            raise AgentConfigError("连接凭证字段与管理员声明不一致")
        if len(json.dumps(credentials).encode()) > 32768:
            raise AgentConfigError("连接凭证总大小最多 32 KiB")
        encrypted = self._cipher().encrypt(json.dumps(credentials).encode()).decode()
        identity = connection_id or str(uuid.uuid4())
        return self._update(user_id, identity, expected, {"target": target, "ciphertext": encrypted, "revoked": False,
                             "target_signature": entry.get("signature")}, existing=connection_id is not None)

    def revoke(self, user_id: str, connection_id: str, expected: str) -> dict:
        return self._update(user_id, connection_id, expected, {"revoked": True}, existing=True)

    def _update(self, user_id: str, identity: str, expected: str | None, patch: dict, *, existing: bool) -> dict:
        path = self._path(user_id, identity)
        with os.fdopen(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = self._read(user_id, identity) if existing else {"connection_id": identity, "owner": user_id}
            if existing and record["revision"] != expected:
                raise AgentConfigError("连接已更新，请重新加载", code="revision_conflict", status=409)
            record.update(patch)
            record["revision"] = uuid.uuid4().hex
            temporary = path.with_name(f".{identity}-{uuid.uuid4().hex}.tmp")
            try:
                with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.public(record)

    def resolve(self, user_id: str, connection_id: str, expected: str | None = None) -> tuple[dict, dict[str, str]]:
        if connection_id.startswith("team:"):
            target = connection_id[5:]
            if target not in self.catalog():
                raise AgentConfigError("团队连接不可用", code="connection_unavailable", status=409)
            self._validate_credentials(target, os.environ)
            return {"target": target, "connection_id": connection_id, "revision": "team"}, {}
        record = self._read(user_id, connection_id)
        entry = self.entry(user_id, record["target"])
        if record["revoked"] or (expected and expected != record["revision"]) or entry is None or record.get("target_signature") != entry.get("signature"):
            raise AgentConfigError("连接已撤销、变更或不可用，请重新选择", code="connection_unavailable", status=409)
        try:
            credentials = json.loads(self._cipher().decrypt(record["ciphertext"].encode()))
            if not isinstance(credentials, dict) or set(credentials) != set(entry["fields"]) or any(not isinstance(value, str) for value in credentials.values()):
                raise ValueError
        except (InvalidToken, ValueError, KeyError) as exc:
            raise AgentConfigError("连接凭证无法解密", code="connection_unavailable", status=409) from exc
        if entry.get("personal"):
            if any(not credentials.get(source) for source in entry["mapping"].values()):
                raise AgentConfigError("Hook 凭证待配置", code="connection_credentials_missing", status=409)
        else:
            self._validate_credentials(record["target"], credentials)
        return self.public(record), credentials

    def _validate_credentials(self, target: str, credentials) -> None:
        if target.startswith("mcp:"):
            server = self.settings.mcp.servers[target[4:]]
            mapping = getattr(server, "env_from_process", {}) | getattr(server, "headers_from_env", {})
        else:
            hook = next(item for item in self.settings.hooks.plugins if item.hook_id == target[5:])
            mapping = hook.config.get("headers_from_env", {})
        if any(not credentials.get(source) for source in mapping.values()):
            raise AgentConfigError("连接凭证字段待配置，请联系管理员或更换凭证", code="connection_credentials_missing", status=409)
