import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';

import { readE2eState } from './global-setup';

const root = path.resolve(import.meta.dirname, '../..');

async function login(page: Page, username: string, password: string) {
  await page.goto('/');
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.locator('.identity-bar')).toContainText(username);
}

test('HTTPS 同源认证、过期、注销与双用户 SSE 隔离', async ({ browser }) => {
  const state = readE2eState();
  const contextA = await browser.newContext({ ignoreHTTPSErrors: true });
  const contextB = await browser.newContext({ ignoreHTTPSErrors: true });
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  await login(pageA, 'alice', state.password);
  await login(pageB, 'bob', state.password);

  const cookies = await contextA.cookies();
  const sessionCookie = cookies.find((item) => item.name === '__Host-codepilot_session');
  const csrfCookie = cookies.find((item) => item.name === '__Host-codepilot_csrf');
  expect(sessionCookie).toMatchObject({ secure: true, httpOnly: true, sameSite: 'Strict' });
  expect(csrfCookie).toMatchObject({ secure: true, httpOnly: false, sameSite: 'Strict' });

  for (const page of [pageA, pageB]) {
    await page.evaluate(() => {
      const target = window as typeof window & { runtimeEvents?: unknown[]; runtimeSource?: EventSource };
      target.runtimeEvents = [];
      target.runtimeSource = new EventSource('/api/agent-runtimes/stream');
      target.runtimeSource.onmessage = (event) => target.runtimeEvents?.push(JSON.parse(event.data));
      target.runtimeSource.addEventListener('agent_running', (event) => target.runtimeEvents?.push(JSON.parse((event as MessageEvent).data)));
    });
  }
  const agentId = await pageA.evaluate(async () => {
    const response = await fetch('/api/agents');
    const payload = await response.json();
    return payload.agents[0].agent_id as string;
  });
  await pageA.evaluate(async (id) => {
    const csrf = document.cookie.split('; ').find((item) => item.startsWith('__Host-codepilot_csrf='))?.split('=')[1] || '';
    const response = await fetch(`/api/agents/${id}/start`, { method: 'POST', headers: { 'X-CodePilot-CSRF': decodeURIComponent(csrf) } });
    if (!response.ok) throw new Error(`start failed: ${response.status}`);
  }, agentId);
  await expect.poll(() => pageA.evaluate(() => (window as typeof window & { runtimeEvents?: unknown[] }).runtimeEvents?.length || 0)).toBeGreaterThan(0);
  await pageB.waitForTimeout(300);
  expect(await pageB.evaluate(() => (window as typeof window & { runtimeEvents?: unknown[] }).runtimeEvents?.length || 0)).toBe(0);

  await pageB.getByRole('button', { name: '退出登录' }).click();
  await expect(pageB.getByRole('button', { name: '登录' })).toBeVisible();

  const python = path.join(root, 'backend/.venv/bin/python');
  execFileSync(python, ['-c', `import sqlite3; c=sqlite3.connect(${JSON.stringify(path.join(state.home, 'auth.sqlite3'))}); c.execute("UPDATE auth_sessions SET idle_expires_at=0 WHERE user_id=(SELECT user_id FROM users WHERE username_normalized='alice')"); c.commit()`]);
  await pageA.reload();
  await expect(pageA.getByRole('button', { name: '登录' })).toBeVisible();
  await contextA.close();
  await contextB.close();
});
