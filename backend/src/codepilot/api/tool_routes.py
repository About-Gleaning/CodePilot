"""代码工具管理与显式测试接口。"""

import asyncio
import inspect
from typing import Literal

from fastapi import HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from codepilot.api.hook_routes import resource_references
from codepilot.session.agent_config import AgentConfigError
from codepilot.tools.code_store import ToolPayload, ToolStore
from codepilot.tools.code_testing import ToolTestPayload, ToolTestService


class ToolRevision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: str = Field(pattern=r'^[0-9a-f]{64}$')


class PublishTool(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=1, max_length=100)


class ToolState(ToolRevision):
    archived: bool | None = None
    withdrawn: bool | None = None
    disabled: bool | None = None


class ToolRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, detail={"code": "tool_config_invalid", "message": "工具字段缺失、超限或格式无效"})
        return safe


def register_tool_routes(router, app_state):
    previous_route_class = router.route_class
    router.route_class = ToolRoute
    store = ToolStore(app_state.workspace.codepilot_home)
    tests = ToolTestService(app_state.workspace, app_state.settings)
    app_state.tool_tests = tests

    async def invoke(method, *args, **kwargs):
        try:
            if inspect.iscoroutinefunction(method):
                value = await method(*args, **kwargs)
            else:
                value = await asyncio.to_thread(method, *args, **kwargs)
            return JSONResponse(value, headers={'Cache-Control': 'no-store'})
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from exc
        except (ValueError, TypeError):
            raise HTTPException(422, detail={'code': 'tool_config_invalid', 'message': '工具配置或参数无效'})

    @router.get('/tools')
    async def listing(request: Request, scope: Literal['private', 'public', 'platform'] = 'private',
                      q: str = Query('', max_length=100), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        if scope == 'platform':
            capabilities = app_state.agent_config_service.capabilities(request.state.principal.user_id)
            rows = [{**item, 'tool_id': item['name'], 'visibility': 'platform'} for item in capabilities['tools']
                    if q.lower() in (item['name'] + item['description']).lower()]
            return JSONResponse({'tools': rows[offset:offset + limit], 'total': len(rows)}, headers={'Cache-Control': 'no-store'})
        return await invoke(store.list, request.state.principal.user_id, scope, offset, limit, q)

    @router.post('/tools')
    async def create(payload: ToolPayload, request: Request):
        # 旧版本允许缺少字段用于读取兼容；新的公开创建入口必须显式命名。
        if 'call_name' not in payload.definition.model_fields_set:
            raise HTTPException(422, detail={'code': 'tool_config_invalid', 'message': '新建工具必须填写调用名称'})
        return await invoke(store.save, request.state.principal.user_id, None, payload)

    @router.get('/tools/{identity}')
    async def detail(identity: str, request: Request):
        user = request.state.principal.user_id
        def read():
            bundle = store.load(user, identity)
            return {**bundle, 'can_manage': bundle['owner_user_id'] == user}
        return await invoke(read)

    @router.put('/tools/{identity}')
    async def update(identity: str, payload: ToolPayload, request: Request):
        return await invoke(store.save, request.state.principal.user_id, identity, payload)

    @router.post('/tools/{identity}/state')
    async def state(identity: str, payload: ToolState, request: Request):
        if payload.disabled is not None and request.state.principal.role != 'admin':
            raise HTTPException(403, detail='只有管理员可禁用工具')
        if payload.withdrawn is not None and not identity.startswith('public:') or payload.archived is not None and not identity.startswith('personal:'):
            raise HTTPException(422, detail='工具状态与作用域不匹配')
        return await invoke(store.set_state, request.state.principal.user_id, identity, payload.expected_revision,
                            archived=payload.archived, withdrawn=payload.withdrawn, disabled=payload.disabled,
                            admin=request.state.principal.role == 'admin')

    @router.post('/tools/{identity}/publication-preview')
    async def preview(identity: str, payload: ToolRevision, request: Request):
        return await invoke(store.preview, request.state.principal.user_id, identity, payload.expected_revision)

    @router.post('/tools/{identity}/publish')
    async def publish(identity: str, payload: PublishTool, request: Request):
        return await invoke(store.publish, request.state.principal.user_id, identity, payload.token)

    @router.get('/tools/{identity}/references')
    async def references(identity: str, request: Request):
        await invoke(store.get, request.state.principal.user_id, identity)
        def read():
            user = request.state.principal.user_id
            service = app_state.agent_config_service
            agents = resource_references(service, user, 'tool_ids', identity)
            # 公共工具独立于 Agent 发布物；这里只列当前可见的配置引用。
            with service.publications.db() as db:
                rows = db.execute("SELECT id, manifest FROM publications WHERE status='published' OR owner=?", (user,)).fetchall()
            import json
            for row in rows:
                manifest = json.loads(row["manifest"])
                if any(identity in profile.get("tool_ids", []) for profile in manifest["profiles"].values()):
                    root = manifest["profiles"][row["id"]]
                    agents.append({"agent_id": row["id"], "name": root["name"]})
            return {'agents': agents}
        return await invoke(read)

    @router.post('/tools/{identity}/tests')
    async def test(identity: str, payload: ToolTestPayload, request: Request):
        return await invoke(tests.start, request.state.principal.user_id, identity, payload)

    @router.get('/tool-tests/{identity}')
    async def result(identity: str, request: Request):
        return await invoke(tests.get, request.state.principal.user_id, identity)

    @router.post('/tool-tests/{identity}/cancel')
    async def cancel(identity: str, request: Request):
        return await invoke(tests.cancel, request.state.principal.user_id, identity)

    router.route_class = previous_route_class
