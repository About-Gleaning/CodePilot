import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codepilot.session.agent_config import AgentConfigError
from codepilot.skills import SkillRegistry
from codepilot.skills.store import SkillStore


def payload(text="第一版", **extra):
    files = {"SKILL.md": base64.b64encode(f"---\nname: review\ndescription: 审查\n---\n{text}".encode()).decode(),
             "references/check.txt": base64.b64encode(b"check").decode()}
    return {"files": files, **extra}


def test_skill_current_content_is_atomic_and_owner_scoped(tmp_path):
    store = SkillStore(tmp_path)
    owner, other = str(uuid4()), str(uuid4())
    first = store.save(owner, None, payload())
    second = store.save(owner, first["skill_id"], payload("第二版", expected_revision=first["revision"]))
    assert first["revision"] != second["revision"]
    assert "第二版" in store.read(owner, first["skill_id"], "SKILL.md").decode()
    root = store._directory(owner, first["skill_id"])
    assert len(list(root.glob("content-*"))) == 1
    assert not (root / "versions").exists()
    with pytest.raises(AgentConfigError):
        store.read(other, first["skill_id"], "SKILL.md")
    with pytest.raises(AgentConfigError) as conflict:
        store.save(owner, first["skill_id"], payload("第三版", expected_revision=first["revision"]))
    assert conflict.value.status == 409
    with pytest.raises(AgentConfigError):
        store.read(owner, first["skill_id"], "SKILL.md", expected_revision=first["revision"])


def test_skill_failed_publish_preserves_current_version(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    owner = str(uuid4())
    first = store.save(owner, None, payload())
    original = store._write

    def fail_attachment(path, content):
        if path.name == "check.txt":
            raise OSError("模拟附件落盘失败")
        original(path, content)

    monkeypatch.setattr(store, "_write", fail_attachment)
    with pytest.raises(OSError):
        store.save(owner, first["skill_id"], payload("第二版", expected_revision=first["revision"]))
    assert store.get(owner, first["skill_id"])["revision"] == first["revision"]


@pytest.mark.parametrize("path", ["../outside", "/outside", "a/../../outside", "a\\b", "a//../b"])
def test_skill_rejects_traversal(tmp_path, path):
    data = payload()
    data["files"][path] = "YQ=="
    with pytest.raises(AgentConfigError):
        SkillStore(tmp_path).save(str(uuid4()), None, data)


def test_skill_archive_blocks_execution_but_allows_management_read(tmp_path):
    store = SkillStore(tmp_path)
    owner = str(uuid4())
    first = store.save(owner, None, payload())
    store.archive(owner, first["skill_id"], first["revision"])
    with pytest.raises(AgentConfigError):
        store.resolve(owner, [first["skill_id"]], SkillRegistry(tmp_path / "shared"))
    assert store.read(owner, first["skill_id"], "references/check.txt") == b"check"
    with pytest.raises(AgentConfigError):
        store.read(owner, first["skill_id"], "SKILL.md", active=True)
    with pytest.raises(AgentConfigError):
        store.save(owner, first["skill_id"], payload(expected_revision=first["revision"]))


def test_shared_skill_reads_latest_without_scanning_build_directory(tmp_path):
    root = tmp_path / "skills" / "review"
    root.mkdir(parents=True)
    root.joinpath("SKILL.md").write_bytes(base64.b64decode(payload()["files"]["SKILL.md"]))
    root.joinpath("data.txt").write_text("第一版")
    build = root / ".build"
    build.mkdir()
    (build / "release").symlink_to(tmp_path)
    legacy = SkillRegistry(tmp_path / "skills")
    legacy.discover()
    store = SkillStore(tmp_path)
    first = store.resolve(str(uuid4()), None, legacy).skills[0]
    root.joinpath("data.txt").write_text("第二版")
    assert store.read_shared_path(first.path, "data.txt").decode() == "第二版"
    assert not (tmp_path / "skill-snapshots").exists()
    with pytest.raises(AgentConfigError):
        store.read_shared_path(first.path, ".build/release/outside")


def test_skill_update_requires_existing_record(tmp_path):
    with pytest.raises(AgentConfigError) as error:
        SkillStore(tmp_path).save(str(uuid4()), str(uuid4()), payload())
    assert error.value.status == 404


def test_legacy_skill_migrates_only_current_and_removes_history(tmp_path):
    store, owner, identity = SkillStore(tmp_path), str(uuid4()), str(uuid4())
    root = store._directory(owner, identity)
    files = {key: base64.b64decode(value) for key, value in payload()["files"].items()}
    manifest = {key: hashlib.sha256(value).hexdigest() for key, value in sorted(files.items())}
    revision = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    for name, data in files.items():
        store._write(root / "versions" / revision / name, data)
    store._write(root / "versions" / revision / ".manifest.json", json.dumps(manifest).encode())
    store._write(root / "versions" / "unused" / "old.txt", b"old")
    store._publish(root, {"skill_id": identity, "revision": revision, "name": "review", "description": "审查",
                          "archived": False, "visibility": "private", "files": sorted(files)})
    assert store.read(owner, identity, "SKILL.md") == files["SKILL.md"]
    assert store.get(owner, identity)["revision"] == revision
    assert not (root / "versions").exists()
    assert len(list(root.glob("content-*"))) == 1


@pytest.mark.asyncio
async def test_load_skill_reads_new_content_in_same_context_and_checks_owner(tmp_path):
    from codepilot.tools.load_skill_tool import LoadSkillTool
    store, owner = SkillStore(tmp_path), str(uuid4())
    first = store.save(owner, None, payload())
    registry = store.resolve(owner, [first["skill_id"]], SkillRegistry(tmp_path / "skills"))
    context = SimpleNamespace(workspace=SimpleNamespace(codepilot_home=tmp_path, user_id=owner),
                              runtime=SimpleNamespace(skill_registry=registry))
    tool = LoadSkillTool(registry, 1)
    assert "第一版" in (await tool.execute({"name": "review"}, context))["output"]
    store.save(owner, first["skill_id"], payload("第二版", expected_revision=first["revision"]))
    assert "第二版" in (await tool.execute({"name": "review"}, context))["output"]
    context.workspace.user_id = str(uuid4())
    assert (await tool.execute({"name": "review"}, context))["status"] != "ok"


def test_failed_pointer_publish_keeps_old_content(tmp_path, monkeypatch):
    store, owner = SkillStore(tmp_path), str(uuid4())
    first = store.save(owner, None, payload())
    def fail(*args):
        raise OSError("模拟指针发布失败")
    monkeypatch.setattr(store, "_publish", fail)
    with pytest.raises(OSError):
        store.save(owner, first["skill_id"], payload("第二版", expected_revision=first["revision"]))
    assert "第一版" in store.read(owner, first["skill_id"], "SKILL.md").decode()
    assert len(list(store._directory(owner, first["skill_id"]).glob("content-*"))) == 1


def test_skill_api_current_reads_and_stale_editor_conflicts(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from codepilot.api.skill_routes import register_skill_routes
    app, owner = FastAPI(), str(uuid4())
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.principal = SimpleNamespace(user_id=request.headers.get("test-owner", owner))
        return await call_next(request)
    register_skill_routes(app, SimpleNamespace(workspace=SimpleNamespace(codepilot_home=tmp_path),
                                              skill_registry=SkillRegistry(tmp_path / "skills")))
    with TestClient(app) as client:
        first = client.post("/skills", json=payload()).json()
        path = f"/skills/{first['skill_id']}/files"
        assert client.get(path).status_code == 200
        client.put(f"/skills/{first['skill_id']}", json=payload("新正文", expected_revision=first["revision"]))
        assert "新正文" in client.get(path).text
        assert client.get(path, params={"revision": first["revision"]}).status_code == 409
        assert client.get(path, headers={"test-owner": str(uuid4())}).status_code == 404
        shared = tmp_path / "skills" / "new"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("---\nname: new\ndescription: 新技能\n---\n正文")
        assert client.get("/skills").json()["shared"][0]["name"] == "new"
        assert client.get("/skills/shared:new/files").status_code == 200


def test_worker_ignores_retired_skill_pins(tmp_path):
    from codepilot.scheduler.worker import _read_execution_bundle
    from codepilot.session.agents import AgentProfile
    profile = AgentProfile(name="测试", system_prompt="测试").model_dump()
    profile.update(resolved_skill_versions={"shared:old": "a" * 64}, resource_snapshot_version=1)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema_version": 2, "profile": profile, "prompt": "测试"}))
    _, loaded = _read_execution_bundle(bundle)
    assert "resolved_skill_versions" not in loaded.model_dump()
    assert "resource_snapshot_version" not in loaded.model_dump()
