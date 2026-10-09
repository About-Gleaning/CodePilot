import { afterEach, describe, expect, it, vi } from 'vitest';

import { AUTH_EXPIRED_EVENT, ApiError, apiJson, apiRequest } from './client';

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = 'codepilot_dev_csrf=; Max-Age=0; Path=/';
});

describe('认证 API Client', () => {
  it('保留配置问题和修复建议', async () => {
    const issues = [{ code: 'tool_unavailable', field: 'tool_names', message: '工具不可用：old_tool', suggestion: '请移除旧工具。' }];
    vi.spyOn(window, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'agent_dependencies_missing', message: '依赖不可用', issues } }), { status: 409 }));
    await expect(apiRequest('/api/agents/test/start')).rejects.toMatchObject({ code: 'agent_dependencies_missing', issues });
  });
  it('写请求自动携带 Cookie 与 CSRF Header', async () => {
    document.cookie = 'codepilot_dev_csrf=csrf-token; Path=/';
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    await apiJson('/api/test', 'POST', { value: 1 });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe('same-origin');
    expect(new Headers(init.headers).get('X-CodePilot-CSRF')).toBe('csrf-token');
  });

  it('401 广播认证失效事件', async () => {
    vi.spyOn(window, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      detail: { code: 'authentication_required', message: '登录已失效' },
    }), { status: 401, headers: { 'Content-Type': 'application/json' } }));
    const listener = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, listener);

    await expect(apiRequest('/api/agents')).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener(AUTH_EXPIRED_EVENT, listener);
  });
});

it('发布失败保留逐项结果与可重试标记', async () => {
  const progress = [{ resource_id: 'tool', resource_name: '查询工具', action: 'restore', status: 'completed' }];
  const issues = [{ code: 'publication_changed', message: '资源已变化', resource_kind: 'tool', resource_name: '查询工具', referenced_by: [{ agent_id: 'a', name: '助手' }] }];
  vi.spyOn(window, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'publication_storage_error', message: '保存失败', progress, issues, retryable: true } }), { status: 503 }));
  await expect(apiJson('/api/agent-publications/confirm', 'POST', {})).rejects.toMatchObject({ progress, issues, retryable: true });
});
