"""个人 Hook 的不可变定义与脚本版本。"""

import base64
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from codepilot.hooks.base import HookErrorPolicy, HookType
from codepilot.hooks.protocol import PREPARATION_STAGES
from codepilot.session.agent_config import AgentConfigError
from codepilot.skills.store import SkillStore


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "integer", "boolean"] = "string"
    required: bool = False
    choices: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(default_factory=list, max_length=100)


class HookDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    plugin_type: Literal["prompt", "command", "http"]
    hook_type: HookType
    order: int = Field(default=100, ge=0, le=10000)
    applies_to_tools: list[str] = Field(default_factory=list, max_length=100)
    parameters: dict[str, Parameter] = Field(default_factory=dict, max_length=50)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    on_error: HookErrorPolicy = HookErrorPolicy.FAIL_SESSION
    content: str = Field(default="", max_length=16000)
    argv: list[str] = Field(default_factory=list, max_length=100)
    url: str = Field(default="", max_length=2000)
    credential_headers: dict[str, str] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def validate_definition(self):
        if self.plugin_type == "prompt" and (self.hook_type.value not in PREPARATION_STAGES or not self.content.strip()):
            raise ValueError("Prompt Hook 仅允许前置节点，并须填写正文")
        if self.plugin_type == "command" and (not self.argv or any(not value or len(value) > 2000 or "\x00" in value for value in self.argv)):
            raise ValueError("命令及参数无效")
        if self.plugin_type == "command" and self.argv and "{{" in self.argv[0]:
            raise ValueError("命令名称不能使用参数替换")
        if self.plugin_type == "http":
            url = urlsplit(self.url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment or "{{" in self.url:
                raise ValueError("HTTP 地址无效，凭证须通过连接保存")
        for header, field in self.credential_headers.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", header) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field):
                raise ValueError("凭证字段与 Header 名称无效")
            if header.lower() in {"host", "content-length", "transfer-encoding", "content-type"}:
                raise ValueError("不能覆盖协议 Header")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key) for key in self.parameters):
            raise ValueError("参数名称无效")
        return self


class HookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: HookDefinition
    files: dict[Annotated[str, StringConstraints(max_length=500)], Annotated[str, StringConstraints(max_length=1400000)]] = Field(default_factory=dict, max_length=100)
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class HookStore:
    def __init__(self, home: Path, *, publication_id: str | None = None):
        self.home = home.resolve()
        self.files = SkillStore(home, publication_id=publication_id)

    def root(self, user_id: str) -> Path:
        root = self.files._root(user_id).parent / "hooks"
        if root.is_symlink():
            raise AgentConfigError("Hook 目录不可使用符号链接")
        return root

    def directory(self, user_id: str, identity: str) -> Path:
        if not re.fullmatch(r"personal:[0-9a-f-]{36}", identity):
            raise AgentConfigError("Hook 不存在", status=404)
        try:
            name = str(uuid.UUID(identity[9:]))
        except ValueError as exc:
            raise AgentConfigError("Hook 身份无效", status=404) from exc
        return self.files.resource_path(self.root(user_id), name)

    def get(self, user_id: str, identity: str) -> dict:
        try:
            path = self.files.resource_path(self.directory(user_id, identity), "current.json")
            if path.stat().st_size > 16384:
                raise ValueError
            record = json.loads(path.read_text())
            if record["hook_id"] != identity:
                raise ValueError
            # 旧记录从当前内容记为第 1 版，不推测过去的保存次数。
            record.setdefault("version_number", 1)
            record.setdefault("updated_at", None)
            return record
        except (OSError, ValueError, KeyError) as exc:
            raise AgentConfigError("Hook 不存在或记录损坏", code="hook_not_found", status=404) from exc

    def list(self, user_id: str) -> list[dict]:
        root = self.root(user_id)
        if not root.exists():
            return []
        return [self.get(user_id, f"personal:{item.name}") for item in sorted(root.iterdir())
                if re.fullmatch(r"[0-9a-f-]{36}", item.name) and item.is_dir() and not item.is_symlink()]

    def save(self, user_id: str, identity: str | None, payload: HookPayload, *, creating: bool = False) -> dict:
        creating = creating or identity is None
        identity = identity or f"personal:{uuid.uuid4()}"
        root = self.directory(user_id, identity)
        decoded = {}
        for name, value in payload.files.items():
            self.files.resource_path(root, name)
            if name.startswith("."):
                raise AgentConfigError("脚本文件名不能以点开头")
            try:
                data = base64.b64decode(value, validate=True)
                data.decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise AgentConfigError("脚本必须是 UTF-8 文本") from exc
            if len(data) > 1024 * 1024:
                raise AgentConfigError("单文件最多 1 MiB")
            decoded[name] = data
        if sum(map(len, decoded.values())) > 4 * 1024 * 1024:
            raise AgentConfigError("脚本总量最多 4 MiB")
        definition = payload.definition.model_dump(mode="json")
        # 旧客户端可能携带其他类型的草稿字段，只有命令类型消费脚本引用。
        for arg in definition["argv"] if definition["plugin_type"] == "command" else []:
            if arg.startswith("@file:") and arg[6:] not in decoded:
                raise AgentConfigError("命令引用的脚本文件不存在")
        manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(decoded.items())}
        bundle = {"format_version": 1, "definition": definition, "files": manifest}
        raw = json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()
        if len(raw) > 262144:
            raise AgentConfigError("Hook 定义最多 256 KiB")
        version = hashlib.sha256(raw).hexdigest()
        with self.files._lock(root):
            current = None if creating else self.get(user_id, identity)
            if current and (current["archived"] or current["revision"] != payload.expected_revision):
                raise AgentConfigError("Hook 已更新或归档，请重新加载", code="revision_conflict", status=409)
            if current and current["version"] == version:
                return current
            target = self.files.resource_path(root, f"versions/{version}")
            if not target.exists():
                staging = root / f".staging-{uuid.uuid4().hex}"
                try:
                    self.files._write(staging / "definition.json", raw)
                    for name, data in decoded.items():
                        self.files._write(self.files.resource_path(staging / "files", name), data)
                    target.parent.mkdir(exist_ok=True)
                    os.replace(staging, target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            from codepilot.utils import utc_now_iso
            record = {"hook_id": identity, "revision": uuid.uuid4().hex + uuid.uuid4().hex,
                      "version_number": current["version_number"] + 1 if current else 1,
                      "updated_at": utc_now_iso(),
                      "version": version, "name": definition["name"], "description": definition["description"],
                      "plugin_type": definition["plugin_type"], "hook_type": definition["hook_type"],
                      "archived": False, "visibility": "private"}
            self.files._publish(root, record)
            return record

    def archive(self, user_id: str, identity: str, revision: str) -> dict:
        root = self.directory(user_id, identity)
        with self.files._lock(root):
            record = self.get(user_id, identity)
            if record["revision"] != revision:
                raise AgentConfigError("Hook 已更新，请重新加载", code="revision_conflict", status=409)
            record.update(archived=True, revision=uuid.uuid4().hex + uuid.uuid4().hex)
            self.files._publish(root, record)
            return record

    def load(self, user_id: str, identity: str, version: str | None = None) -> dict:
        current = self.get(user_id, identity)
        if version is None and current["archived"]:
            raise AgentConfigError("Hook 已归档", code="hook_archived", status=409)
        version = version or current["version"]
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise AgentConfigError("Hook 版本无效")
        root = self.files.resource_path(self.directory(user_id, identity), f"versions/{version}")
        try:
            path = self.files.resource_path(root, "definition.json")
            if path.stat().st_size > 262144:
                raise ValueError
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != version:
                raise ValueError
            bundle = json.loads(raw)
            HookDefinition.model_validate(bundle["definition"])
            for name, digest in bundle["files"].items():
                self.files.resource_path(root, "files")
                path = self.files.resource_path(root / "files", name)
                if path.stat().st_size > 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError
            result = {**current, "version": version, **bundle}
            if version != current["version"]:
                # 展示序号属于保存修订，不能将当前编号误标在历史摘要上。
                result.pop("version_number", None)
                result.pop("updated_at", None)
            return result
        except (OSError, ValueError, KeyError) as exc:
            raise AgentConfigError("Hook 版本缺失或损坏", code="hook_version_invalid", status=409) from exc

    def read(self, user_id: str, identity: str, version: str, name: str) -> bytes:
        bundle = self.load(user_id, identity, version)
        if name not in bundle["files"]:
            raise AgentConfigError("文件不存在", status=404)
        return self.files.resource_path(self.directory(user_id, identity), f"versions/{version}/files/{name}").read_bytes()

    def instantiate(self, user_id: str, identity: str, version: str):
        from codepilot.hooks.plugins import CommandPluginHook, HttpPluginHook, PromptPluginHook
        value = self.load(user_id, identity, version)["definition"]
        common = dict(hook_id=identity, hook_type=value["hook_type"], name=value["name"], resource_version=version,
                      selectable=True, parameter_definitions=value["parameters"], order=value["order"],
                      on_error=value["on_error"], timeout_seconds=value["timeout_seconds"],
                      applies_to_tools=value["applies_to_tools"] or None)
        if value["plugin_type"] == "prompt":
            return PromptPluginHook(**common, content=f"[Hook: {identity}]\n{value['content']}")
        if value["plugin_type"] == "command":
            argv = [str(self.files.resource_path(self.directory(user_id, identity), f"versions/{version}/files/{arg[6:]}"))
                    if arg.startswith("@file:") else arg for arg in value["argv"]]
            return CommandPluginHook(**common, protocol_version=1, config={"argv": argv})
        return HttpPluginHook(**common, protocol_version=1,
                              config={"url": value["url"], "headers_from_env": value["credential_headers"]})

    @staticmethod
    def check_command(definition: dict, directory: Path | None = None) -> None:
        if definition["plugin_type"] != "command":
            return
        executable = definition["argv"][0]
        if executable.startswith("@file:"):
            raise AgentConfigError("版本脚本需要指定解释器作为命令", code="hook_dependency_missing", status=409)
        if "/" in executable:
            if directory is None and not Path(executable).is_absolute():
                return
            candidate = Path(executable) if Path(executable).is_absolute() else directory / executable
            available = candidate.is_file() and os.access(candidate, os.X_OK)
        else:
            available = shutil.which(executable) is not None
        if not available:
            raise AgentConfigError("Hook 命令或解释器不可用，请先完成服务器配置", code="hook_dependency_missing", status=409)

    def connection_target(self, user_id: str, identity: str) -> dict:
        record = self.get(user_id, identity)
        if record["archived"]:
            raise AgentConfigError("Hook 已归档", code="hook_archived", status=409)
        # 调用时只检查当前指针；不可变版本的目标映射不反复扫描脚本资源。
        value = _connection_definition(str(self.home), user_id, identity, record["version"])
        signature = hashlib.sha256(json.dumps([value["url"], value["credential_headers"]], sort_keys=True).encode()).hexdigest()
        return {"target": f"hook:{identity}", "kind": "hook", "fields": sorted(set(value["credential_headers"].values())),
                "signature": signature, "mapping": value["credential_headers"], "url": value["url"], "personal": True}


@lru_cache(maxsize=128)
def _connection_definition(home: str, user_id: str, identity: str, version: str) -> dict:
    return HookStore(Path(home)).load(user_id, identity, version)["definition"]
