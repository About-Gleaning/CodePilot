"""公共 Agent 的发布索引与附属资源；私人数据不进入发布清单。"""

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from codepilot.session.agent_config import AgentConfigError
from codepilot.session.agents import AgentProfile
from codepilot.skills.store import SkillStore
from codepilot.hooks.store import HookStore, HookPayload


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def public_identity(publication_id, resource_id):
    return f"public:{publication_id}:{resource_id}"


def split_identity(identity):
    try:
        prefix, publication_id, resource_id = identity.split(":")
        if prefix != "public":
            raise ValueError
        return str(uuid.UUID(publication_id)), str(uuid.UUID(resource_id))
    except (ValueError, AttributeError) as exc:
        raise AgentConfigError("公共资源身份无效", status=404) from exc


def check_public_content(value, field="content"):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if re.search(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)|-----BEGIN .*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{20,}|(?:api[_-]?key|password|secret|access_token)\s*[=:]\s*[\"']?[A-Za-z0-9_/-]{12,}", text, re.I):
        raise AgentConfigError(f"{field} 包含疑似私人路径或秘密，请修正后发布", code="publication_sensitive_content")


class PublicationStore:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.root = self.home / "publications"
        if self.root.is_symlink():
            raise AgentConfigError("公共存储目录不可用")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS publications (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, author TEXT NOT NULL,
                  source TEXT NOT NULL, status TEXT NOT NULL, revision TEXT NOT NULL,
                  number INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
                  manifest TEXT NOT NULL, updated REAL NOT NULL, UNIQUE(owner, source));
                CREATE TABLE IF NOT EXISTS releases (
                  publication TEXT NOT NULL, revision TEXT NOT NULL, manifest TEXT NOT NULL,
                  PRIMARY KEY(publication, revision));
                CREATE TABLE IF NOT EXISTS candidates (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS submissions (
                  owner TEXT NOT NULL, request TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
                  PRIMARY KEY(owner, request));
                CREATE TABLE IF NOT EXISTS candidate_attempts (
                  candidate TEXT PRIMARY KEY, owner TEXT NOT NULL, request TEXT NOT NULL, progress TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS candidate_request ON candidate_attempts(owner, request);
                CREATE TABLE IF NOT EXISTS settings (
                  owner TEXT NOT NULL, publication TEXT NOT NULL, revision TEXT NOT NULL, data TEXT NOT NULL,
                  PRIMARY KEY(owner, publication));
                CREATE INDEX IF NOT EXISTS publication_status ON publications(status, updated);
            """)

    @contextmanager
    def db(self):
        path = self.root / "index.sqlite3"
        if path.is_symlink():
            raise AgentConfigError("公共索引不可用")
        db = sqlite3.connect(path, timeout=10)
        os.chmod(path, 0o600)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, identity):
        try:
            identity = str(uuid.UUID(identity))
        except (ValueError, AttributeError) as exc:
            raise AgentConfigError("公共 Agent 不存在", code="agent_not_found", status=404) from exc
        with self.db() as db:
            row = db.execute("SELECT * FROM publications WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise AgentConfigError("公共 Agent 不存在", code="agent_not_found", status=404)
        result = dict(row)
        result["manifest"] = json.loads(result["manifest"])
        return result

    def find_source(self, owner, source):
        with self.db() as db:
            row = db.execute("SELECT id FROM publications WHERE owner=? AND source=?", (owner, source)).fetchone()
        return self.get(row[0]) if row else None

    def list(self, owner, q="", offset=0, limit=50):
        with self.db() as db:
            rows = db.execute("SELECT id FROM publications WHERE (status='published' OR owner=?) AND (instr(lower(name),lower(?))>0 OR instr(lower(description),lower(?))>0) ORDER BY updated DESC LIMIT ? OFFSET ?", (owner, q, q, limit, offset)).fetchall()
        return [self.view(self.get(row[0]), owner) for row in rows]

    def view(self, record, user, detail=False):
        result = {key: record[key] for key in ("id", "author", "status", "revision", "number", "name", "description", "updated")}
        result.update(agent_id=record["id"], owned=record["owner"] == user)
        if record["owner"] == user:
            result["source_agent_id"] = record["source"]
        if detail:
            manifest = record["manifest"]
            result["profiles"] = manifest["profiles"]
            result["resources"] = [{key: resource[key] for key in ("id", "kind", "name")} for resource in manifest["resources"]]
        return result

    def authorize(self, identity, user, *, write=False):
        record = self.get(identity)
        if write and record["owner"] != user:
            raise AgentConfigError("只有作者可以修改公共资源", status=403)
        if record["status"] != "published" and record["owner"] != user:
            raise AgentConfigError("公共 Agent 已撤回或下架", code="publication_unavailable", status=404)
        if write and record["status"] == "blocked":
            raise AgentConfigError("公共 Agent 已被管理员下架", status=409)
        return record

    @staticmethod
    def issue(error, kind, identity, name, profile, action=None):
        # 原始异常、敏感正文和文件路径不进入面向用户的诊断。
        message = str(error) if isinstance(error, AgentConfigError) else "资源无法读取或内容无效"
        code = getattr(error, "code", "publication_resource_invalid")
        suggestion = "查看并一并发布依赖。" if action else "请检查对应资源或配置，修正后重新预览。"
        if code == "tool_disabled":
            suggestion = "请联系管理员恢复工具权限；快捷发布不能解除管理员禁用。"
        elif code == "tool_withdrawn":
            suggestion = "请联系工具作者恢复发布，或在 Agent 配置中移除该工具。"
        elif "归档" in message:
            suggestion = "请先在资源管理页面手动恢复资源，或从 Agent 配置中移除，再重新预览。"
        return {"code": code, "field": kind + "_ids",
                "message": message, "suggestion": suggestion,
                "resource_kind": kind, "resource_id": identity, "resource_name": name,
                "referenced_by": [{"agent_id": profile.agent_id, "name": profile.name}] if profile else [], "action": action}

    @staticmethod
    def reject_issues(issues):
        if not issues:
            return
        grouped = {}
        for issue in issues:
            key = (issue["resource_kind"], issue["resource_id"], issue["code"], issue["message"])
            if key in grouped:
                for agent in issue["referenced_by"]:
                    if agent not in grouped[key]["referenced_by"]:
                        grouped[key]["referenced_by"].append(agent)
            else:
                grouped[key] = issue
        error = AgentConfigError("发布检查未通过：" + "；".join(dict.fromkeys(i["message"] for i in grouped.values())),
                                 code="publication_dependencies_missing", status=409)
        error.issues = list(grouped.values())
        raise error

    def preview(self, service, user, author, source, include_dependencies=False):
        try:
            private = service._private_service(user).get_record_snapshot(source)
        except AgentConfigError as exc:
            exc.issues = [self.issue(exc, "agent", source, None, None)]
            raise
        if private["archived"] or private["profile"] is None or not private["profile"].supports_direct:
            self.reject_issues([self.issue(AgentConfigError("请选择有效、未归档且启用 direct 模式的私人主 Agent 发布"), "agent", source, private["name"], private["profile"])])
        previous = self.find_source(user, source)
        if previous and previous["status"] == "blocked":
            self.reject_issues([self.issue(AgentConfigError("公共 Agent 已被管理员下架，请联系管理员", code="publication_blocked"), "agent", source, private["name"], private["profile"])])
        identity = previous["id"] if previous else str(uuid.uuid4())
        manifest, checks, plans = self.collect(service, user, identity, private["profile"], previous,
                                                include_dependencies=include_dependencies)
        candidate = {"publication_id": identity, "source": source, "author": author,
                     "expected": previous["revision"] if previous else None,
                     "expected_status": previous["status"] if previous else None, "manifest": manifest, "checks": checks,
                     "tools": plans, "include_dependencies": include_dependencies}
        candidate_id = str(uuid.uuid4())
        with self.db() as db:
            db.execute("DELETE FROM candidates WHERE expires<?", (time.time(),))
            # 已确认但暂时失败的候选不因新建预览而被提前挤掉。
            db.execute("DELETE FROM candidates WHERE owner=? AND id NOT IN (SELECT candidate FROM candidate_attempts) AND id NOT IN (SELECT id FROM candidates WHERE owner=? ORDER BY expires DESC LIMIT 4)", (user, user))
            if db.execute("SELECT COUNT(*) FROM candidates WHERE owner=?", (user,)).fetchone()[0] >= 5:
                raise AgentConfigError("发布预览过多，请稍后重试", status=409)
            db.execute("DELETE FROM candidate_attempts WHERE candidate NOT IN (SELECT id FROM candidates)")
            db.execute("INSERT INTO candidates VALUES(?,?,?,?)", (candidate_id, user, time.time() + 900, json.dumps(candidate)))
        return {"candidate_id": candidate_id, "digest": digest(candidate), "expected_revision": candidate["expected"],
                "profiles": manifest["profiles"], "resources": [{k: r[k] for k in ("id", "kind", "name", "payload")} for r in manifest["resources"]],
                "dependencies": list({plan["tool_id"]: plan for plan in plans.values() if plan["action"] != "use"}.values()),
                "excluded": ["私人工作目录", "连接及凭证", "记忆正文", "会话及任务数据"]}

    def collect(self, service, user, identity, root_profile, previous, *, include_dependencies=False, tool_plans=None):
        from codepilot.skills import SkillRegistry
        from codepilot.tools.code_store import ToolStore
        from codepilot.hooks.manager import validate_parameters
        skills, hooks, tool_store = SkillStore(self.home), HookStore(self.home), ToolStore(self.home)
        registry = SkillRegistry(self.home / "skills")
        registry.discover()
        old = {r["source"]: r for r in previous["manifest"]["resources"]} if previous else {}
        resources, profiles, checks, plans, issues = {}, {}, {}, {}, []
        total_bytes = 0
        tool_bytes, action_ids, counted_tools = 0, set(), set()
        children = root_profile.delegate_agent_ids
        if children is None:
            children = [p.agent_id for p in service.list_active_delegated_profile_snapshots(user)] if root_profile.can_delegate else []
        children = list(dict.fromkeys(children))
        source_profiles = [root_profile]
        for child_id in children:
            try:
                record = service.get_record_snapshot(user, child_id)
                child = record["profile"]
                if record["archived"] or child is None or not child.supports_delegated or child_id == root_profile.agent_id:
                    raise AgentConfigError("委派 Agent 无效、已归档或未启用 delegated 模式")
                source_profiles.append(child)
            except AgentConfigError as exc:
                issues.append(self.issue(exc, "agent", child_id, None, root_profile))
        if len(source_profiles) > 100:
            raise AgentConfigError("发布配置超过 100 个资源")
        ids = {p.agent_id: identity if p.agent_id == root_profile.agent_id else str(uuid.uuid5(uuid.UUID(identity), p.agent_id)) for p in source_profiles}
        tool_errors = {}
        for profile in source_profiles:
            checks[profile.agent_id] = profile.revision_id
            data = profile.model_dump(exclude={"resolved_connection_versions", "resolved_hook_versions", "resolved_platform_hook_versions", "resolved_tool_versions"})
            data.update(agent_id=ids[profile.agent_id], revision_id="", visibility="shared", source="custom", working_directory=None, connection_ids=[],
                        delegate_agent_ids=[ids[c] for c in children if c in ids] if profile is root_profile else [])
            for problem in service.shared._dependency_issues(profile):
                issues.append(self.issue(AgentConfigError(problem.message, code=problem.code), "agent", profile.agent_id, profile.name, profile))
            data["tool_ids"] = []
            names = set(service.tool_registry._tools)
            for tool_id in dict.fromkeys(profile.tool_ids):
                name = None
                try:
                    if tool_id in tool_errors:
                        raise tool_errors[tool_id]
                    if tool_id not in plans:
                        if tool_plans is not None:
                            if tool_id not in tool_plans:
                                raise AgentConfigError("工具依赖已变化，请重新预览", code="publication_changed", status=409)
                            plans[tool_id] = tool_plans[tool_id]
                        else:
                            plans[tool_id] = tool_store.prepare_dependency(user, tool_id)
                        plan = plans[tool_id]
                        # 边读取边计数，不能等所有依赖进入内存后才检查整体上限。
                        if plan["tool_id"] not in counted_tools:
                            counted_tools.add(plan["tool_id"])
                            tool_bytes += len(json.dumps(plan, ensure_ascii=False).encode())
                            if plan["action"] != "use":
                                action_ids.add(plan["tool_id"])
                                total_bytes += len(json.dumps({"definition": plan["definition"], "files": plan["files"]}, ensure_ascii=False).encode())
                            if len(action_ids) > 100 or total_bytes > 32 * 1024 * 1024 or tool_bytes > 44 * 1024 * 1024:
                                raise AgentConfigError("发布内容超过资源数量或 32 MiB 限制，已停止读取剩余内容", code="publication_limit", status=409)
                    plan = plans[tool_id]
                    name = plan["name"]
                    if not include_dependencies and plan["action"] != "use":
                        issues.append(self.issue(AgentConfigError("请先发布或恢复代码工具：" + name, code="tool_publication_required"), "tool", tool_id, name, profile, plan["action"]))
                    if plan["tool_id"] in data["tool_ids"]:
                        continue
                    if plan["call_name"] in names:
                        raise AgentConfigError("公共工具调用名称冲突：" + plan["call_name"], code="tool_name_conflict", status=409)
                    names.add(plan["call_name"])
                    if profile.readonly:
                        raise AgentConfigError("只读 Agent 不允许执行代码工具", code="tool_readonly_forbidden", status=409)
                    data["tool_ids"].append(plan["tool_id"])
                except AgentConfigError as exc:
                    if exc.code == "publication_limit":
                        self.reject_issues(issues + [self.issue(exc, "agent", root_profile.agent_id, root_profile.name, root_profile)])
                    if tool_id not in plans:
                        tool_errors[tool_id] = exc
                        try:
                            name = tool_store.get(user, tool_id)["name"]
                        except AgentConfigError:
                            pass
                    issues.append(self.issue(exc, "tool", tool_id, name, profile))
            selected_skills = profile.skill_ids if profile.skill_ids is not None else [f"shared:{s.name}" for s in registry.skills]
            selected_hooks = profile.hook_ids if profile.hook_ids is not None else [h.hook_id for h in service.settings.hooks.plugins if h.enabled]
            data["skill_ids"], data["hook_ids"], data["hook_parameters"] = [], [], {}
            for kind, selected in (("skill", selected_skills), ("hook", selected_hooks)):
                for source_id in dict.fromkeys(selected):
                    if kind == "hook" and not source_id.startswith("personal:"):
                        data["hook_ids"].append(source_id)
                        data["hook_parameters"][source_id] = profile.hook_parameters.get(source_id, {})
                        continue
                    key = kind + ":" + source_id
                    resource_id = old[key]["id"] if key in old else str(uuid.uuid5(uuid.UUID(identity), key))
                    target = public_identity(identity, resource_id)
                    data[kind + "_ids"].append(target)
                    if kind == "hook":
                        data["hook_parameters"][target] = profile.hook_parameters.get(source_id, {})
                    name = None
                    try:
                        if key in resources:
                            if kind == "hook":
                                validate_parameters(profile.hook_parameters.get(source_id, {}), resources[key]["parameters"])
                            continue
                        # 已交付的资源以公共当前内容为准，不检查或覆盖私人源。
                        selected_skills_store = SkillStore(self.home, publication_id=identity) if key in old else skills
                        selected_hooks_store = HookStore(self.home, publication_id=identity) if key in old else hooks
                        selected_id = resource_id if kind == "skill" and key in old else "personal:" + resource_id if key in old else source_id
                        if kind == "skill":
                            if source_id.startswith("shared:") and key not in old:
                                resolved = skills.resolve(user, [source_id], registry).skills[0]
                                raw = {n: skills.read_shared_path(resolved.path, n) for n in skills.shared_files(resolved)}
                                name = resolved.name
                            else:
                                with selected_skills_store._lock(selected_skills_store._directory(user, selected_id)):
                                    record = selected_skills_store._current(user, selected_id)
                                    name = record["name"]
                                    if record["archived"]:
                                        raise AgentConfigError("Skill 已归档", code="skill_archived")
                                    raw = {n: skills.read_shared_path(selected_skills_store._directory(user, selected_id) / record["content_dir"], n) for n in record["files"]}
                                    checks[key] = record["revision"]
                            payload = {"files": {n: base64.b64encode(b).decode() for n, b in raw.items()}}
                            if "SKILL.md" not in raw:
                                raise AgentConfigError("Skill 缺少 SKILL.md", code="skill_invalid")
                            skills.validate_document(raw["SKILL.md"])
                            parameters = None
                        else:
                            hook = selected_hooks_store.load(user, selected_id)
                            name = hook["name"]
                            raw = {n: selected_hooks_store.read(user, selected_id, hook["version"], n) for n in hook["files"]}
                            payload = {"definition": hook["definition"], "files": {n: base64.b64encode(b).decode() for n, b in raw.items()}}
                            parameters = hook["definition"]["parameters"]
                            validate_parameters(profile.hook_parameters.get(source_id, {}), parameters)
                            selected_hooks_store.check_command(hook["definition"])
                            checks[key] = hook["revision"]
                            check_public_content(hook["definition"], "Hook 定义")
                        for content in raw.values():
                            check_public_content(content.decode("utf-8", errors="replace"), "资源文件")
                        resource_bytes = sum(map(len, raw.values()))
                        if len(raw) > 100 or any(len(b) > 1024 * 1024 for b in raw.values()) or resource_bytes > 4 * 1024 * 1024:
                            raise AgentConfigError("资源超过文件数量或大小限制")
                        if key not in old:
                            total_bytes += resource_bytes
                            if total_bytes > 32 * 1024 * 1024:
                                raise AgentConfigError("发布内容超过 32 MiB 限制，已停止读取剩余内容", code="publication_limit", status=409)
                        resources[key] = {"id": resource_id, "source": key, "kind": kind, "name": old[key]["name"] if key in old else name,
                                          "payload": None if key in old else payload, "parameters": parameters}
                    except (AgentConfigError, OSError, ValueError) as exc:
                        if isinstance(exc, AgentConfigError) and exc.code == "publication_limit":
                            self.reject_issues(issues + [self.issue(exc, "agent", root_profile.agent_id, root_profile.name, root_profile)])
                        issues.append(self.issue(exc, kind, source_id, name, profile))
            try:
                check_public_content(data, "Agent 配置")
            except AgentConfigError as exc:
                issues.append(self.issue(exc, "agent", profile.agent_id, profile.name, profile))
            profiles[data["agent_id"]] = data
        # 依赖检查不把私人运行凭证或目录混入发布校验。
        actions = list({p["tool_id"]: p for p in plans.values() if p["action"] != "use"}.values())
        if len(actions) > 100 or total_bytes > 32 * 1024 * 1024 or len(profiles) + len(resources) > 100:
            issues.append(self.issue(AgentConfigError("发布内容超过资源数量或 32 MiB 限制"), "agent", root_profile.agent_id, root_profile.name, root_profile))
        self.reject_issues(issues)
        manifest = {"schema_version": 4, "profiles": profiles, "resources": [{k: v for k, v in r.items() if k != "parameters"} for r in resources.values()]}
        if len(json.dumps(manifest).encode()) > 44 * 1024 * 1024:
            raise AgentConfigError("发布内容超过整体大小限制")
        return manifest, checks, plans

    def confirm(self, service, user, candidate_id, candidate_digest, expected, request_id):
        from codepilot.tools.code_store import ToolStore
        fingerprint = digest([candidate_id, candidate_digest, expected])
        tool_store = ToolStore(self.home)
        with self.db() as db:
            # 所有批量确认按公共发布库→工具库的顺序加锁，串行化同候选重试。
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT * FROM submissions WHERE owner=? AND request=?", (user, request_id)).fetchone()
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise AgentConfigError("请求 ID 已用于其他发布", status=409)
                return json.loads(saved["result"])
            row = db.execute("SELECT * FROM candidates WHERE id=? AND owner=?", (candidate_id, user)).fetchone()
            if row is None or row["expires"] < time.time():
                error = AgentConfigError("发布预览已过期，请重新预览", code="publication_expired", status=409)
                attempt = db.execute("SELECT progress FROM candidate_attempts WHERE candidate=? AND owner=?", (candidate_id, user)).fetchone()
                error.progress = json.loads(attempt["progress"]) if attempt else []
                raise error
            candidate = json.loads(row["data"])
            if digest(candidate) != candidate_digest or expected != candidate["expected"]:
                raise AgentConfigError("发布预览不匹配", status=409)
            if "tools" not in candidate:
                raise AgentConfigError("旧发布预览需要重新生成", code="publication_changed", status=409)
            attempt = db.execute("SELECT * FROM candidate_attempts WHERE candidate=?", (candidate_id,)).fetchone()
            reserved = db.execute("SELECT candidate FROM candidate_attempts WHERE owner=? AND request=?", (user, request_id)).fetchone()
            if reserved and reserved["candidate"] != candidate_id:
                raise AgentConfigError("请求 ID 已用于其他发布候选", status=409)
            if attempt and (attempt["owner"] != user or attempt["request"] != request_id):
                raise AgentConfigError("该候选已绑定其他请求 ID，请使用原请求重试", status=409)
            if not attempt:
                db.execute("INSERT INTO candidate_attempts VALUES(?,?,?,?)", (candidate_id, user, request_id, "[]"))
                # 在任何依赖副作用前持久化请求归属，崩溃后也不可换请求 ID。
                db.commit()
                db.execute("BEGIN IMMEDIATE")
                # 释放请求归属事务后，另一个相同请求可能先完成全部发布。
                saved = db.execute("SELECT * FROM submissions WHERE owner=? AND request=?", (user, request_id)).fetchone()
                if saved:
                    if saved["fingerprint"] != fingerprint:
                        raise AgentConfigError("请求 ID 已用于其他发布", status=409)
                    return json.loads(saved["result"])
            plans = candidate.get("tools", {})
            unique_plans = {plan["tool_id"]: plan for plan in plans.values()}
            actions = [(key, plan) for key, plan in unique_plans.items() if plan["action"] != "use"]
            progress = [{"resource_id": plan["tool_id"], "resource_name": plan["name"], "action": plan["action"], "status": "pending"} for _, plan in actions]
            agent_step = {"resource_id": candidate["publication_id"], "resource_name": candidate["manifest"]["profiles"][candidate["publication_id"]]["name"], "action": "publish_agent", "status": "pending"}
            progress.append(agent_step)
            progress_by_id = {item["resource_id"]: item for item in progress}
            stage = agent_step
            final_savepoint = False
            try:
                previous, profile = self._check_candidate(db, service, user, candidate)
                # 先核对所有步骤回执，不能依靠“已经发布成功”绕过后来的撤权。
                with tool_store.db() as tools_db:
                    for key, plan in unique_plans.items():
                        stage = progress_by_id.get(plan["tool_id"], agent_step)
                        receipt = tool_store.dependency_receipt(tools_db, user, candidate_id + ":" + plan["tool_id"], plan)
                        if receipt:
                            progress_by_id[plan["tool_id"]]["status"] = "completed"
                        tool_store.check_dependency(tools_db, user, plan, receipt)
                stage = agent_step
                self._check_contents(service, user, candidate, profile, previous)
                for index, (key, plan) in enumerate(actions):
                    stage = progress[index]
                    tool_store.apply_dependency(user, plan, candidate_id + ":" + key)
                    stage["status"] = "completed"
                stage = agent_step
                previous, profile = self._check_candidate(db, service, user, candidate)
                manifest = self._check_contents(service, user, candidate, profile, previous)
                with tool_store.db() as tools_db:
                    tools_db.execute("BEGIN IMMEDIATE")
                    for key, plan in unique_plans.items():
                        receipt = tool_store.dependency_receipt(tools_db, user, candidate_id + ":" + plan["tool_id"], plan)
                        if plan["action"] != "use" and receipt is None:
                            raise AgentConfigError("工具发布尚未完成", status=409)
                        tool_store.check_dependency(tools_db, user, plan, receipt)
                    # 保留工具进度时不能同时提交尚未完成的 Agent 索引写入。
                    db.execute("SAVEPOINT final_publication")
                    final_savepoint = True
                    result = self._publish_manifest(db, user, candidate, manifest, previous, request_id, fingerprint)
                    db.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
                    db.execute("DELETE FROM candidate_attempts WHERE candidate=?", (candidate_id,))
                    stage["status"] = "completed"
                    if actions:
                        result["progress"] = progress
                        db.execute("UPDATE submissions SET result=? WHERE owner=? AND request=?", (json.dumps(result), user, request_id))
                    # 工具写锁保持到公共入口提交结束，撤回和禁用无法插入最终检查窗口。
                    db.commit()
                    return result
            except (AgentConfigError, OSError, sqlite3.Error) as exc:
                if not actions:
                    raise
                if final_savepoint and db.in_transaction:
                    db.execute("ROLLBACK TO final_publication")
                    agent_step["status"] = "failed"
                # 回执是跨存储事实来源；即使步骤返回前异常也重新核对真实成功情况。
                with tool_store.db() as tools_db:
                    for key, plan in actions:
                        receipt = tool_store.dependency_receipt(tools_db, user, candidate_id + ":" + key, plan)
                        if receipt:
                            progress_by_id[plan["tool_id"]]["status"] = "completed"
                if stage and stage["status"] != "completed":
                    stage["status"] = "failed"
                retryable = isinstance(exc, (OSError, sqlite3.Error)) or isinstance(exc, AgentConfigError) and exc.code in {"publication_storage_error"}
                error = exc if isinstance(exc, AgentConfigError) else AgentConfigError("发布保存失败，请重试；已成功的依赖仍然公开", code="publication_storage_error", status=503)
                if not error.issues:
                    referrers = [p for p in candidate["manifest"]["profiles"].values() if stage["resource_id"] in p.get("tool_ids", [])]
                    source_ids = {candidate["publication_id"]: candidate["source"]}
                    source_ids.update({str(uuid.uuid5(uuid.UUID(candidate["publication_id"]), key)): key for key in candidate["checks"] if not key.startswith(("skill:", "hook:")) and key != candidate["source"]})
                    error.issues = [{"code": error.code, "field": "tool_ids" if referrers else "agent_ids", "message": str(error),
                        "suggestion": "使用原候选重试。" if retryable else "请重新检查并生成发布预览。",
                        "resource_kind": "tool" if referrers else "agent", "resource_id": stage["resource_id"],
                        "resource_name": stage["resource_name"], "referenced_by": [{"agent_id": source_ids[p["agent_id"]], "name": p["name"]} for p in referrers]}]
                error.progress, error.retryable = progress, retryable
                db.execute("UPDATE candidate_attempts SET progress=? WHERE candidate=?", (json.dumps(progress), candidate_id))
                db.commit()
                raise error from exc

    def _check_candidate(self, db, service, user, candidate):
        row = db.execute("SELECT * FROM publications WHERE id=?", (candidate["publication_id"],)).fetchone()
        source_row = db.execute("SELECT id FROM publications WHERE owner=? AND source=?", (user, candidate["source"])).fetchone()
        if source_row and source_row[0] != candidate["publication_id"]:
            raise AgentConfigError("该 Agent 已从其他预览发布，请重新预览", code="publication_changed", status=409)
        previous = dict(row) if row else None
        if previous:
            previous["manifest"] = json.loads(previous["manifest"])
        if previous and (previous["revision"] != candidate["expected"] or previous["status"] != candidate["expected_status"] or previous["status"] == "blocked"):
            raise AgentConfigError("公共配置已变化，请重新预览", code="publication_changed", status=409)
        source = service._private_service(user).get_record_snapshot(candidate["source"])
        if source["archived"] or source["profile"] is None:
            raise AgentConfigError("源配置已归档或无效，请重新预览", code="publication_changed", status=409)
        return previous, source["profile"]

    def _check_contents(self, service, user, candidate, profile, previous):
        manifest, checks, plans = self.collect(service, user, candidate["publication_id"], profile, previous,
                                               include_dependencies=True, tool_plans=candidate.get("tools"))
        if checks != candidate["checks"] or manifest != candidate["manifest"] or plans != candidate.get("tools", {}):
            changed = []
            resources_by_source = {r["source"]: r for r in candidate["manifest"]["resources"] + manifest["resources"]}
            for key in dict.fromkeys([*candidate["checks"], *checks]):
                if checks.get(key) == candidate["checks"].get(key):
                    continue
                if key.startswith(("skill:", "hook:")):
                    kind, source_id = key.split(":", 1)
                    resource = resources_by_source.get(key, {})
                    name = resource.get("name")
                else:
                    kind, source_id = "agent", key
                    public_id = candidate["publication_id"] if key == candidate["source"] else str(uuid.uuid5(uuid.UUID(candidate["publication_id"]), key))
                    data = manifest["profiles"].get(public_id) or candidate["manifest"]["profiles"].get(public_id, {})
                    name = data.get("name")
                changed.append(self.issue(AgentConfigError("源配置或资源已变化，请重新预览", code="publication_changed"), kind, source_id, name, profile))
            if not changed:
                changed.append(self.issue(AgentConfigError("源配置或资源内容已变化，请重新预览", code="publication_changed"), "agent", profile.agent_id, profile.name, profile))
            self.reject_issues(changed)
        return manifest

    def _publish_manifest(self, db, user, candidate, manifest, previous, request_id, fingerprint):
        identity = candidate["publication_id"]
        for resource in manifest["resources"]:
            payload = resource.pop("payload")
            if payload is None:
                continue
            if resource["kind"] == "skill":
                store = SkillStore(self.home, publication_id=identity)
                root = store._directory(user, resource["id"])
                exists = (root / "current.json").exists()
                store.save(user, resource["id"], {**payload, "expected_revision": store.get(user, resource["id"])["revision"] if exists else None}, creating=not exists)
            else:
                store = HookStore(self.home, publication_id=identity)
                target = "personal:" + resource["id"]
                root = store.directory(user, target)
                exists = (root / "current.json").exists()
                store.save(user, target, HookPayload.model_validate({**payload, "expected_revision": store.get(user, target)["revision"] if exists else None}), creating=not exists)
        revision = digest(manifest)
        number = previous["number"] + (previous["revision"] != revision) if previous else 1
        main = manifest["profiles"][identity]
        raw = json.dumps(manifest)
        db.execute("INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, user, candidate["author"], candidate["source"], "published", revision, number, main["name"], main["description"], raw, time.time()))
        db.execute("INSERT OR IGNORE INTO releases VALUES(?,?,?)", (identity, revision, raw))
        result = {"id": identity, "agent_id": identity, "revision": revision, "number": number}
        db.execute("INSERT INTO submissions VALUES(?,?,?,?)", (user, request_id, fingerprint, json.dumps(result)))
        return result

    def change_status(self, identity, user, expected, *, blocked=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.get(identity)
            if not blocked and record["owner"] != user:
                raise AgentConfigError("只有作者可以撤回", status=403)
            if record["revision"] != expected:
                raise AgentConfigError("公共配置已变化", status=409)
            if record["status"] == "blocked" and not blocked:
                raise AgentConfigError("管理员下架不能由作者修改", status=403)
            db.execute("UPDATE publications SET status=?, updated=? WHERE id=?", ("blocked" if blocked else "withdrawn", time.time(), identity))

    def settings(self, user, identity):
        with self.db() as db:
            row = db.execute("SELECT * FROM settings WHERE owner=? AND publication=?", (user, identity)).fetchone()
        return {"revision": row["revision"], **json.loads(row["data"])} if row else {"revision": None, "working_directory": None, "connections": {}}

    def profile(self, user, identity, revision=None, *, active=True):
        record = self.get(identity)
        if active and record["status"] != "published":
            raise AgentConfigError("公共 Agent 已撤回或下架", code="publication_unavailable", status=409)
        manifest = record["manifest"]
        revision = revision or record["revision"]
        if revision != record["revision"]:
            with self.db() as db:
                row = db.execute("SELECT manifest FROM releases WHERE publication=? AND revision=?", (identity, revision)).fetchone()
            if row is None:
                raise AgentConfigError("公共配置发布记录不存在", status=404)
            manifest = json.loads(row[0])
        settings = self.settings(user, identity)
        profiles = {}
        for key, value in manifest["profiles"].items():
            profiles[key] = AgentProfile.model_validate({**value, "publication_id": identity, "revision_id": revision,
                "working_directory": settings["working_directory"] if key == identity else None,
                "connection_ids": settings["connections"].get(key, [])})
        root = profiles.pop(identity)
        root.publication_children = {key: value.model_dump() for key, value in profiles.items()}
        return root

    def agent_view(self, user, identity, detail=False):
        record = self.get(identity)
        self.check_history_access(record, user)
        profile = self.profile(user, identity, active=False)
        value = {"agent_id": identity, "revision_id": record["revision"], "name": profile.name,
                 "source": "custom", "visibility": "shared", "publication_id": identity,
                 "owner_user_id": record["owner"], "archived": record["status"] != "published",
                 "validation_status": "valid", "validation_issues": [], "kind": "agent",
                 "description": profile.description, "readonly": profile.readonly,
                 "default_provider": profile.default_provider, "default_model": profile.default_model}
        if detail:
            value.update(profile.model_dump())
            value["tool_names"] = [t for t in profile.allowed_tools if not t.startswith("mcp:")]
            value["mcp_server_names"] = [t[4:] for t in profile.allowed_tools if t.startswith("mcp:")]
        return value

    def check_history_access(self, record, user):
        if record["status"] != "published" and record["owner"] != user and self.settings(user, record["id"])["revision"] is None:
            raise AgentConfigError("公共 Agent 已撤回或下架", code="agent_not_found", status=404)

    def save_settings(self, user, identity, data, expected):
        self.authorize(identity, user)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.settings(user, identity)["revision"] != expected:
                raise AgentConfigError("使用设置已变化，请重新加载", status=409)
            revision = uuid.uuid4().hex
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?,?)", (user, identity, revision, json.dumps(data)))
        return {"revision": revision, **data}
