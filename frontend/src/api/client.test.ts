import { afterEach, describe, expect, it, vi } from 'vitest';

import { AUTH_EXPIRED_EVENT, ApiError, apiJson, apiRequest } from './client';

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = 'codepilot_dev_csrf=; Max-Age=0; Path=/';
});

describe('认证 API Client', () => {
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
